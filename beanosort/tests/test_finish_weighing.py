"""Finish/Stop opens weighing without asserting an unconfirmed drain."""
import pytest
from sqlalchemy import func, select

from beanosort.models import Audit, Ledger, Operation, Reconciliation
from test_web import client_for, token
from test_weighing import form_for, ready


def state(x,op):
    with x.db.read() as s:
        return (s.get(Operation,op).status,
                s.scalar(select(func.count()).select_from(Reconciliation)),
                s.scalar(select(func.count()).select_from(Ledger)))


@pytest.mark.parametrize('initial',['running','paused','failed','finishing'])
def test_finish_opens_weighing_directly_and_get_does_not_advance_boundary(system,tmp_path,initial):
    x=system;op,_,_=ready(x,tmp_path,count=25,finish=False)
    if initial!='running':x.workflow.transition(x.uid,op,initial,reason='Test boundary')
    client=client_for(x);page=client.get('/jobs/'+op)
    if initial=='finishing':
        assert 'Await reconciliation' not in page.text
        assert 'Continue to container weighing' in page.text
    response=client.post('/jobs/'+op+'/transition',data={'csrf':token(page),'target':'finishing'},follow_redirects=False)
    assert response.status_code==303 and response.headers['location']=='/jobs/'+op+'/weigh'
    page=client.get(response.headers['location'])
    assert page.status_code==200 and 'Weigh containers & reconcile' in page.text
    assert 'all in-flight beans are accounted for' in page.text
    assert state(x,op)==('finishing',0,1)
    assert client.get('/jobs/'+op+'/weigh').status_code==200
    assert state(x,op)==('finishing',0,1)


@pytest.mark.parametrize('change',[{'weighed_selected':''},{'gross_selected':'0.1'},{'revision_selected':'stale'}])
def test_invalid_weights_cannot_confirm_finishing_boundary(system,tmp_path,change):
    x=system;op,_,_=ready(x,tmp_path,count=25,finish=False)
    client=client_for(x);page=client.get('/jobs/'+op)
    page=client.post('/jobs/'+op+'/transition',data={'csrf':token(page),'target':'finishing'})
    response=client.post('/jobs/'+op+'/weigh/review',data={**form_for(x,op,**change),'csrf':token(page),'key':token(page,'key')})
    assert response.status_code==409
    assert state(x,op)==('finishing',0,1)


def test_final_confirmed_weights_advance_boundary_once_but_do_not_post_until_accepted(system,tmp_path):
    x=system;op,_,_=ready(x,tmp_path,count=25,finish=False)
    client=client_for(x);page=client.get('/jobs/'+op)
    page=client.post('/jobs/'+op+'/transition',data={'csrf':token(page),'target':'finishing'})
    data={**form_for(x,op),'csrf':token(page),'key':token(page,'key')}
    review=client.post('/jobs/'+op+'/weigh/review',data=data)
    assert review.status_code==200,review.text
    assert state(x,op)==('awaiting_reconciliation',0,1)
    assert client.post('/jobs/'+op+'/weigh/review',data=data).status_code==200
    with x.db.read() as s:
        transitions=[a for a in s.scalars(select(Audit).filter_by(operation_id=op,action='execution_transition'))
                     if a.data['to']=='awaiting_reconciliation']
        assert len(transitions)==1
        assert transitions[0].data['in_flight_accounted_for'] is True
        assert transitions[0].data['confirmed_with']=='final_container_weighings'
    accept={'csrf':token(review),'review_token':token(review,'review_token'),'accept_review':'on','reason':'Reviewed final weights'}
    assert client.post('/jobs/'+op+'/weigh/accept',data=accept).status_code==200
    final=state(x,op)
    assert final[0]=='completed' and final[1]==1
    assert client.post('/jobs/'+op+'/weigh/accept',data=accept).status_code==200
    assert state(x,op)==final


def test_running_job_cannot_weigh_or_use_legacy_drain_shortcut(system,tmp_path):
    x=system;op,_,_=ready(x,tmp_path,count=25,finish=False)
    client=client_for(x);page=client.get('/jobs/'+op)
    assert client.get('/jobs/'+op+'/weigh').status_code==409
    response=client.post('/jobs/'+op+'/transition',data={'csrf':token(page),'target':'awaiting_reconciliation','drained':'on'})
    assert response.status_code==409 and state(x,op)==('running',0,1)
    client.post('/jobs/'+op+'/transition',data={'csrf':token(page),'target':'finishing'})
    response=client.post('/jobs/'+op+'/transition',data={'csrf':token(page),'target':'awaiting_reconciliation'})
    assert response.status_code==409 and state(x,op)==('finishing',0,1)


@pytest.mark.parametrize('environment',['simulation','real'])
def test_nonreference_sorting_jobs_also_go_directly_to_weighing(factory,environment):
    x=factory(environment);source,*_=x.receive('1');op,_=x.prepare(source,'1');x.start(op,25)
    client=client_for(x);page=client.get('/jobs/'+op)
    page=client.post('/jobs/'+op+'/transition',data={'csrf':token(page),'target':'finishing'})
    assert page.url.path=='/jobs/'+op+'/weigh'
    assert page.status_code==200 and 'gross_selected' in page.text
    assert state(x,op)==('finishing',0,1)
