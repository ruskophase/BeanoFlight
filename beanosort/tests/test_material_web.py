import re
from html.parser import HTMLParser

from sqlalchemy import select

from beanosort.handling import MaterialHandling
from beanosort.material import MaterialActions
from beanosort.models import Assignment, Container, HandlingPlan
from test_material_actions import adjust_data, empty, state
from test_web import client_for, token


def values(page):
    result=dict(re.findall(r'<input type="hidden" name="(state_[^"]+)" value="([^"]+)"',page.text))
    return {**result,'csrf':token(page),'key':token(page,'key'),'representative':'on'}


def test_page_has_four_functions_and_product_packaging_uses_remaining_weighing(system):
    client=client_for(system);page=client.get('/handling')
    for path in ('move','contents','reconcile','package'):
        assert f'href="/handling/{path}"' in page.text
    assert '<h1>Material handling</h1>' in page.text
    assert '/handling/new?kind=' not in page.text
    package=client.get('/handling/package')
    assert '<h1>Package as product</h1>' in package.text and 'name="pack_masses"' not in package.text
    assert 'Packaging desk' in package.text and 'name="weight"' in package.text
    for kind in ('transfer','split','blend','packaging'):
        response=client.get('/handling/new?kind='+kind,follow_redirects=False)
        assert response.status_code==303
        assert response.headers['location']==('/handling/package' if kind=='packaging' else '/handling/contents')
    assert client.post('/handling',data={'csrf':token(page),'kind':'packaging'}).status_code==409


def test_one_post_moves_and_combines_contents_and_replay_is_idempotent(system):
    x=system;pid,cs,*_=x.receive('2',label='Source beans');_,cd,*_=x.receive('1',label='Destination beans')
    client=client_for(x);page=client.get('/handling/contents')
    assert page.status_code==200 and page.headers['cache-control']=='no-store'
    assert '2.000 kg contents' in page.text and 'nominal capacity 1000.000 kg' in page.text
    data={**values(page),'source_id':cs,'destination_id':cd,'moved_mass':'0.75'}
    response=client.post('/handling/contents',data=data)
    assert response.status_code==200,response.text
    assert '<h1>Movement recorded</h1>' in response.text
    assert '1.25 kg calculated remainder' in response.text and '1.75 kg calculated total' in response.text
    assert 'Record work started' not in response.text and 'Release permitted outputs' not in response.text
    assert client.post('/handling/contents',data=data).url==response.url
    assert state(x,cs)['mass_mg']==1250000 and state(x,cd)['mass_mg']==1750000
    detail=client.get('/operations/'+response.url.path.rsplit('/',1)[-1])
    assert detail.url==response.url


def test_complete_move_needs_no_mass_or_reason_and_preserves_id(system):
    x=system;pid,cs,*_=x.receive('2');cd=empty(x);client=client_for(x)
    page=client.get('/handling/contents')
    data={**values(page),'source_id':cs,'destination_id':cd,'empty_source':'on'}
    response=client.post('/handling/contents',data=data)
    assert response.status_code==200,response.text
    assert '2 kg' in response.text and 'recorded source balance; no new weighing' in response.text
    assert state(x,cd)['portion_id']==pid and state(x,cd)['mass_mg']==2000000
    assert state(x,cs)['portion_id'] is None


def test_stale_signed_state_csrf_and_tampering_cannot_change_contents(system):
    x=system;_,cs,*_=x.receive('2');cd=empty(x);client=client_for(x)
    page=client.get('/handling/contents');data={**values(page),'source_id':cs,'destination_id':cd,'moved_mass':'0.5'}
    assert client.post('/handling/contents',data={**data,'csrf':'wrong'}).status_code==403
    assert client.post('/handling/contents',data={**data,'state_'+cd:'invalid'}).status_code==409
    MaterialActions(x.db).reconcile(x.uid,'other-operator-weighing',adjust_data(x,cs,weight='1.9'))
    response=client.post('/handling/contents',data=data)
    assert response.status_code==409 and 'changed' in response.text
    assert state(x,cd)['portion_id'] is None


def test_gross_weighing_updates_same_portion_and_zero_closes_it_without_extra_steps(system):
    x=system;pid,cid,*_=x.receive('2');client=client_for(x)
    page=client.get('/handling/reconcile?container_id='+cid)
    data={**values(page),'container_id':cid,'weight':'2.4','weighing_basis':'gross','reason':'Samples removed'}
    response=client.post('/handling/reconcile',data=data)
    assert response.status_code==200,response.text
    assert '<h1>Weighing recorded</h1>' in response.text and '-0.1 kg' in response.text
    assert state(x,cid)['portion_id']==pid and state(x,cid)['mass_mg']==1900000
    page=client.get('/handling/reconcile')
    response=client.post('/handling/reconcile',data={**values(page),'container_id':cid,'weight':'0.5','weighing_basis':'gross','reason':'Source now empty'})
    assert response.status_code==200 and 'Container recorded empty' in response.text
    assert state(x,cid)['portion_id'] is None


def test_historical_prepared_jobs_can_still_be_completed(system):
    x=system;pid,*_=x.receive('2');dest=empty(x)
    old=MaterialHandling(x.db).prepare(x.uid,'old-plan-created-before-upgrade','transfer',{
        'portion_ids':[pid],'container_id':dest,'station_id':x.stations['Receiving desk'],
        'policy_id':x.versions['reconciliation_policy'],'ready':True})
    client=client_for(x);page=client.get('/handling/'+old)
    assert 'Begin manual work' in page.text
    page=client.post('/handling/'+old+'/start',data={'csrf':token(page)})
    response=client.post('/handling/'+old+'/execute',data={**values(page),'reason':'Complete existing transfer','physical_confirmation':'on'})
    assert response.status_code==200 and state(x,dest)['portion_id']==pid
