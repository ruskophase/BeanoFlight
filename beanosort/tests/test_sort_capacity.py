from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from beanosort.models import Audit, Container, Operation, Particle
from beanosort.quantities import DomainError
from beanosort.sort_capacity import snapshot
from test_segmented_jobs import swap
from test_web import client_for, token


def ready(x, initial='0.9', capacity='1', *, start=True):
    pid, *_ = x.receive('2')
    previous, data = x.prepare(pid,'2')
    # Replace the unused prepared destinations with a deliberately small vessel.
    x.workflow.delete_prepared_sorting(x.uid,'capacity-delete-'+uuid4().hex,previous,confirm_unstarted=True)
    cid=x.workflow.create_container(x.uid,label='Small receiver '+uuid4().hex,location='Floor',capacity=capacity,tare_mass='0.1')
    if initial!='0':
        x.workflow.receive(x.uid,'capacity-receipt-'+uuid4().hex,dict(container_id=cid,station_id=x.stations['Receiving desk'],
            net_mass=initial,ready=True,evidence=x.evidence))
    data['pass_through_id']=cid
    op=x.workflow.prepare(x.uid,'capacity-plan-'+uuid4().hex,data)
    run=x.start(op,2000) if start else None
    return op,run,data


def status(x,op):
    with x.db.read() as s:return s.get(Operation,op).status


def fill(x,op):
    with x.db.read() as s:return next(row for row in snapshot(s,s.get(Operation,op),x.db.environment) if row['logical_stream']=='pass_through')


def test_warning_does_not_block_start_then_thresholds_pause_and_resume(system):
    x=system;op,run,_=ready(x)
    assert status(x,op)=='running' and fill(x,op)['level']=='orange'
    assert x.workflow.simulate_next(x.uid,run)<250
    assert status(x,op)=='paused' and Decimal(fill(x,op)['percent_exact'])==100
    with pytest.raises(DomainError,match='Resume'):x.workflow.simulate_next(x.uid,run)
    with x.db.read() as s:before=s.scalar(select(func.count()).select_from(Particle))
    assert x.workflow.simulate_next(x.uid,run,replay_first=True)==0
    x.workflow.transition(x.uid,op,'running')
    assert fill(x,op)['next_pause_percent']==105
    assert status(x,op)=='running'
    x.workflow.simulate_next(x.uid,run)
    assert status(x,op)=='paused' and Decimal(fill(x,op)['percent_exact'])==105
    assert fill(x,op)['level']=='red'
    x.workflow.transition(x.uid,op,'running')
    x.workflow.simulate_next(x.uid,run)
    assert status(x,op)=='paused' and Decimal(fill(x,op)['percent_exact'])==110
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Particle))>before
        assert len(list(s.scalars(select(Audit).filter_by(operation_id=op,action='capacity_pause'))))==3
        assert len(list(s.scalars(select(Audit).filter_by(operation_id=op,action='capacity_override'))))==2


def test_already_full_start_pauses_and_user_can_override(system):
    x=system;op,run,_=ready(x,initial='1.1')
    assert status(x,op)=='paused' and fill(x,op)['level']=='red'
    x.workflow.transition(x.uid,op,'running')
    assert status(x,op)=='running' and fill(x,op)['next_pause_percent']==115
    x.workflow.simulate_next(x.uid,run)
    assert status(x,op)=='paused' and Decimal(fill(x,op)['percent_exact'])==115


def test_replacement_resets_its_fill_and_kept_collection_does_not_reset(system):
    x=system;op,run,data=ready(x)
    x.workflow.simulate_next(x.uid,run)
    replacement=x.workflow.create_container(x.uid,label='Fresh receiver',location='Floor',capacity='10',tare_mass='0.1')
    swap(x,op,data,pass_through_id=replacement)
    assert fill(x,op)['container_id']==replacement and fill(x,op)['mass_mg']==0
    x.workflow.transition(x.uid,op,'running')
    assert x.workflow.simulate_next(x.uid,run)>0 and status(x,op)=='running'


def test_readonly_web_warning_json_and_override_button(system):
    x=system;op,run,_=ready(x)
    client=client_for(x)
    with x.db.read() as s:before=s.scalar(select(func.count()).select_from(Audit))
    page=client.get('/jobs/'+op)
    assert page.status_code==200,page.text
    assert '90.0% of nominal capacity' in page.text and 'no physical stop command' in page.text
    result=client.get('/jobs/'+op+'/capacity-status')
    assert result.status_code==200 and result.headers['cache-control']=='no-store'
    with x.db.read() as s:assert s.scalar(select(func.count()).select_from(Audit))==before
    response=client.post('/jobs/'+op+'/simulate-batch',data={'csrf':token(page)})
    assert response.status_code==200 and response.json()['paused']
    page=client.get('/jobs/'+op)
    assert 'Override capacity warning &amp; resume' in page.text or 'Override capacity warning & resume' in page.text
    response=client.post('/jobs/'+op+'/transition',data={'csrf':token(page),'target':'running','reason':'Accept full receiving container'})
    assert response.status_code==200 and fill(x,op)['next_pause_percent']==105


def test_reference_model_pauses_without_rewriting_passages(system,tmp_path):
    from test_references import reference
    x=system;_,recipe,_=reference(x,tmp_path)
    op,_,data=ready(x,initial='0',capacity='0.05',start=False)
    x.workflow.delete_prepared_sorting(x.uid,'replace-with-reference-recipe',op,confirm_unstarted=True)
    data['recipe_id']=recipe;data.pop('capture_model_id')
    op=x.workflow.prepare(x.uid,'capacity-reference-job',data)
    run=x.start(op,300)
    generated=x.workflow.simulate_next(x.uid,run)
    assert 0<generated<250 and status(x,op)=='paused'
    assert Decimal(fill(x,op)['percent_exact'])>=100
    with x.db.read() as s:before=[(p.id,p.digest) for p in s.scalars(select(Particle).filter_by(run_id=run))]
    assert x.workflow.simulate_next(x.uid,run,replay_first=True)==0
    x.workflow.transition(x.uid,op,'running')
    x.workflow.simulate_next(x.uid,run)
    assert status(x,op)=='paused' and Decimal(fill(x,op)['percent_exact'])>=105
    with x.db.read() as s:
        assert all(s.get(Particle,key).digest==value for key,value in before)


def test_full_replacement_requires_an_override_and_resume_keeps_new_threshold(system):
    x=system;op,run,data=ready(x)
    x.workflow.simulate_next(x.uid,run)
    replacement=x.workflow.create_container(x.uid,label='Full replacement',location='Floor',capacity='1',tare_mass='0.1')
    x.workflow.receive(x.uid,'full-replacement-receipt',dict(container_id=replacement,station_id=x.stations['Receiving desk'],
        net_mass='1',ready=True,evidence=x.evidence))
    with pytest.raises(DomainError,match='capacity'):swap(x,op,data,pass_through_id=replacement)
    swap(x,op,data,pass_through_id=replacement,capacity_override=True)
    x.workflow.transition(x.uid,op,'running')
    assert fill(x,op)['next_pause_percent']==105
    x.workflow.simulate_next(x.uid,run)
    assert status(x,op)=='paused' and Decimal(fill(x,op)['percent_exact'])==105


def test_user_can_record_actual_overfill_after_capacity_override(system):
    from beanosort.service import balance
    from beanosort.models import Output
    x=system;op,run,data=ready(x)
    x.workflow.simulate_next(x.uid,run)
    x.workflow.transition(x.uid,op,'running')
    x.workflow.simulate_next(x.uid,run)
    x.finish(op)
    x.reconcile(op,selected='0.05',pass_through='1.05',remainder='1.8')
    with x.db.read() as s:
        output=s.scalar(select(Output).filter_by(operation_id=op,stream='pass_through'))
        assert balance(s,output.portion_id)==1050000
        assert s.scalar(select(Audit.id).filter_by(operation_id=op,action='capacity_final_override'))


def test_in_flight_batches_are_retained_and_override_arms_next_new_band(system):
    from beanosort.capture import fixture_particle
    x=system;op,run,_=ready(x)
    x.workflow.simulate_next(x.uid,run)
    with x.db.read() as s:first=s.scalar(select(func.max(Particle.source_sequence)).filter_by(run_id=run))+1
    arrived=[fixture_particle(i) for i in range(first,first+250)]
    assert x.workflow.ingest(x.uid,run,'in-flight-after-pause',arrived)==250
    assert status(x,op)=='paused' and Decimal(fill(x,op)['percent_exact'])>=120
    x.workflow.transition(x.uid,op,'running')
    row=fill(x,op)
    assert row['next_pause_percent']>Decimal(row['percent_exact'])
    x.workflow.simulate_next(x.uid,run)
    assert status(x,op)=='paused'


def test_unknown_manual_incoming_mass_is_not_fabricated(factory):
    from beanosort.capture import fixture_particle
    x=factory('real');op,run,_=ready(x)
    item=fixture_particle(0)
    item['evidence']['features']['kind']='image_derived'
    item['evidence']['decision']['kind']='model_derived'
    item['evidence']['action']['kind']='controller_record'
    x.workflow.ingest(x.uid,run,'archived-real-particle',[item])
    row=fill(x,op)
    assert not row['mass_known'] and row['mass_mg']==900000
    assert 'Incoming mass unavailable' in row['basis']
    assert status(x,op)=='running'
