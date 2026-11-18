"""All-pass hopper emptying: one inventory input, unique vessels and no actions."""
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from beanosort.collections import starting_mass
from beanosort.models import Assignment, Destination, Ledger, Lineage, Operation, Output, Particle, Portion, Profile, Reservation, Version
from beanosort.quantities import DomainError
from beanosort.references import create_recipe, matches
from beanosort.service import available, balance
from beanosort.sorting_modes import HOPPER_RECIPE_NAME
from beanosort.weighing import build_review, weighing_context
from test_occupied_destinations import empty
from test_references import reference
from test_web import client_for, token


def hopper(x, tmp_path, *, same=True, planned='10'):
    dataset, normal, _ = reference(x, tmp_path)
    recipe = create_recipe(x.db, x.uid, 'hopper-recipe', HOPPER_RECIPE_NAME, dataset, feature='area_mm2', relation='pass_all')
    source, container, *_ = x.receive('10')
    data = dict(portion_id=source, planned_mass=planned, station_id=x.stations['Training sorter'],
        pass_through_id=container if same else empty(x), recipe_id=recipe,
        policy_id=x.versions['reconciliation_policy'], ready=True)
    op = x.workflow.prepare(x.uid, 'hopper-job', data)
    return op, source, container, data, normal


def simulate(x, op, *, interrupted=False):
    run = x.workflow.start(x.uid, op, count=300, seed='hopper-emptying')
    x.workflow.simulate_next(x.uid, run)
    if interrupted:
        x.workflow.transition(x.uid, op, 'failed', reason='Feeding interrupted; hopper emptied back to source')
    else:
        while x.workflow.simulate_next(x.uid, run): pass
    x.finish(op)
    return run


def review(x, op, **gross):
    with x.db.read() as s:
        context = weighing_context(s, s.get(Operation, op), 'simulation')
        form = {}
        for row in context['rows']:
            stream = row['stream']
            form.update({'gross_'+stream:gross.get(stream,row['gross_value']),
                'revision_'+stream:str(row['container'].revision), 'weighed_'+stream:'on'})
        return build_review(s, s.get(Operation, op), 'simulation', form), form


@pytest.mark.parametrize('planned,interrupted', [('10',False),('2',False),('2',True)])
def test_shared_vessel_is_weighed_once_and_never_double_counted(system,tmp_path,planned,interrupted):
    x=system;op,source,container,data,_=hopper(x,tmp_path,planned=planned)
    with x.db.read() as s:
        assert starting_mass(s,s.get(Operation,op))==0
        assert s.scalar(select(func.count()).select_from(Reservation).where(Reservation.operation_id==op,Reservation.active==True))==1
        assert s.scalar(select(Destination).filter_by(operation_id=op)).readiness['source_return'] is True
        assert available(s,s.get(Portion,source))==0
    run=simulate(x,op,interrupted=interrupted)
    proof,form=review(x,op)
    assert set(proof['data']['container_weighing']['containers'])=={'pass_through'}
    assert proof['data']['selected']==proof['data']['remainder']=='0'
    assert proof['input_total_mg']==10000000 and form['gross_pass_through']=='10.500'
    with x.db.read() as s:
        particles=list(s.scalars(select(Particle).filter_by(run_id=run)))
        assert all(p.payload['decision']=='pass' and p.payload['action']=='not_issued' and p.simulated_destination=='pass_through' for p in particles)
        profile=x.workflow.profile(s,s.get(Operation,op));assert profile['expected']['selected']=='0'
        assert profile['counts']['action_attempted']==0
        assert matches(None,s.get(Version,data['recipe_id']).data) is False
    for invalid in ({'selected':'1'},{'remainder':'1'}):
        with pytest.raises(DomainError):x.workflow.reconcile(x.uid,'invalid-'+str(uuid4()),op,{**proof['data'],**invalid})
    rid=x.workflow.reconcile(x.uid,'hopper-weights',op,proof['data'])
    assert x.workflow.reconcile(x.uid,'hopper-weights',op,proof['data'])==rid
    x.workflow.release(x.uid,op,accept_estimates=True,reason='Reviewed single shared vessel')
    assert x.workflow.simulate_next(x.uid,run,replay_first=True)==0
    with x.db.read() as s:
        outputs=list(s.scalars(select(Output).filter_by(operation_id=op)))
        assert len(outputs)==1 and outputs[0].stream=='pass_through' and outputs[0].mass_mg==10000000
        assert s.scalar(select(func.sum(Ledger.delta_mg)))==10000000
        assignment=s.scalar(select(Assignment).filter_by(container_id=container,active=True))
        assert assignment.portion_id!=source and balance(s,assignment.portion_id)==10000000
        assert s.scalar(select(Lineage).filter_by(parent_id=source,child_id=assignment.portion_id))
        assert s.scalar(select(Profile).filter_by(portion_id=assignment.portion_id)).data['source_return'] is True
    # A second passage inherits the whole source description, not only the
    # observed hopper sample, and gets a new independently namespaced run.
    second=x.workflow.prepare(x.uid,'second-hopper-job',{**data,'portion_id':assignment.portion_id})
    assert x.workflow.start(x.uid,second,count=20,seed='next-pass')!=run


def test_separate_pass_through_requires_two_weights_and_no_selected(system,tmp_path):
    x=system;op,source,container,data,_=hopper(x,tmp_path,same=False,planned='2')
    simulate(x,op)
    proof,form=review(x,op,pass_through='2.500',remainder='8.500')
    assert set(proof['data']['container_weighing']['containers'])=={'pass_through','remainder'}
    assert proof['discrepancy_mg']==0
    x.workflow.reconcile(x.uid,'two-vessel-weights',op,proof['data'])
    x.workflow.release(x.uid,op,accept_estimates=True,reason='Reviewed two vessels')
    with x.db.read() as s:
        assert {o.stream:o.mass_mg for o in s.scalars(select(Output).filter_by(operation_id=op))}=={'pass_through':2000000,'remainder':8000000}


def test_unstarted_shared_job_can_be_deleted_and_normal_recipe_cannot_reuse_source(system,tmp_path):
    x=system;op,source,container,data,normal=hopper(x,tmp_path)
    x.workflow.delete_prepared_sorting(x.uid,'delete-hopper-job',op,confirm_unstarted=True)
    with x.db.read() as s:assert available(s,s.get(Portion,source))==10000000
    with pytest.raises(DomainError):x.workflow.prepare(x.uid,'wrong-source-return',{**data,'recipe_id':normal,'selected_id':empty(x)})
    with pytest.raises(DomainError):x.workflow.prepare(x.uid,'unexpected-selected',{**data,'selected_id':empty(x)})
    op=x.workflow.prepare(x.uid,'new-hopper-job',data)
    with pytest.raises(DomainError):x.workflow.prepare(x.uid,'competing-job',data)


def test_all_pass_can_add_to_a_different_occupied_container(system,tmp_path):
    x=system;unused,source,_,data,_=hopper(x,tmp_path,same=False,planned='2')
    x.workflow.delete_prepared_sorting(x.uid,'replace-occupied-hopper',unused,confirm_unstarted=True)
    old,destination,*_=x.receive('3')
    op=x.workflow.prepare(x.uid,'occupied-hopper-job',{**data,'pass_through_id':destination})
    simulate(x,op)
    proof,_=review(x,op,pass_through='5.500',remainder='8.500')
    assert proof['input_total_mg']==13000000 and proof['discrepancy_mg']==0
    x.workflow.reconcile(x.uid,'occupied-hopper-weights',op,proof['data'])
    x.workflow.release(x.uid,op,accept_estimates=True,reason='Reviewed combined all-pass output')
    with x.db.read() as s:
        child=s.scalar(select(Output.portion_id).filter_by(operation_id=op,stream='pass_through'))
        assert {edge.parent_id for edge in s.scalars(select(Lineage).where(Lineage.child_id==child))}=={source,old}
        assert balance(s,child)==5000000 and s.scalar(select(func.sum(Ledger.delta_mg)))==13000000


def test_single_container_web_review_acceptance_and_recipe_preview(system,tmp_path):
    x=system;op,_,_,data,_=hopper(x,tmp_path);simulate(x,op)
    client=client_for(x)
    assert client.get('/recipes/'+data['recipe_id']).status_code==200
    page=client.get('/jobs/'+op+'/weigh')
    assert 'name="gross_selected"' not in page.text and 'name="gross_remainder"' not in page.text
    _,form=review(x,op)
    page=client.post('/jobs/'+op+'/weigh/review',data={**form,'csrf':token(page),'key':token(page,'key')})
    assert page.status_code==200,page.text
    accept={'csrf':token(page),'review_token':token(page,'review_token'),'accept_review':'on','reason':'Reviewed one shared container'}
    result=client.post('/jobs/'+op+'/weigh/accept',data=accept)
    assert result.status_code==200 and 'Completed' in result.text
    assert 'Source / Pass-through' in result.text and '(10 kg)' in result.text
    assert client.post('/jobs/'+op+'/weigh/accept',data=accept).status_code==200
    client.close()
