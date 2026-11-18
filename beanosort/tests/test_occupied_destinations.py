from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from beanosort.models import Assignment, Destination, Input, Ledger, Lineage, Operation, Output, Portion, Profile, Reservation, identity
from beanosort.quantities import DomainError, kg
from beanosort.service import Workflow, available, balance
from beanosort.weighing import build_review, weighing_context
from test_resorting import first_pass, generate, prepare, reconcile


def prepare_into(x, source, selected, passed, amount='2', recipe=None):
    data=dict(portion_id=source,planned_mass=amount,recipe_id=recipe or x.versions['recipe'],
              capture_model_id=x.versions['capture_model'] if recipe is None else None,
              policy_id=x.versions['reconciliation_policy'],station_id=x.stations['Training sorter'],
              selected_id=selected,pass_through_id=passed,ready=True)
    if data['capture_model_id'] is None:data.pop('capture_model_id')
    return x.workflow.prepare(x.uid,'occupied-'+uuid4().hex,data),data


def empty(x):return x.workflow.create_container(x.uid,label='Collection '+uuid4().hex,location='Floor',capacity='100',tare_mass='0.5')


def test_reserve_existing_contents_cancel_edit_and_competing_use(system):
    x=system;source,*_=x.receive('2');baseline,selected,*_=x.receive('5');passed=empty(x)
    op,data=prepare_into(x,source,selected,passed)
    with x.db.read() as s:
        assert balance(s,baseline)==5000000 and available(s,s.get(Portion,baseline))==0
        assert s.scalar(select(func.count()).select_from(Input).where(Input.operation_id==op))==1
        pin=s.scalar(select(Destination).filter_by(operation_id=op,stream='selected')).readiness
        assert pin['existing_portion_id']==baseline and pin['existing_mass_mg']==5000000
    with pytest.raises(DomainError):prepare_into(x,baseline,empty(x),empty(x),'5')
    replacement=x.workflow.edit_prepared_sorting(x.uid,'edit-occupied-job',op,{**data,'selected_id':passed,'pass_through_id':selected},confirm_unstarted=True)
    x.workflow.delete_prepared_sorting(x.uid,'delete-occupied-job',replacement,confirm_unstarted=True)
    with x.db.read() as s:
        assert available(s,s.get(Portion,baseline))==5000000 and available(s,s.get(Portion,source))==2000000
        assert s.scalar(select(func.count()).select_from(Ledger))==2


def test_two_occupied_destinations_conserve_stock_and_keep_both_ancestries(system):
    x=system;source,*_=x.receive('2');old_s,selected,*_=x.receive('5');old_p,passed,*_=x.receive('7')
    op,_=prepare_into(x,source,selected,passed);x.start(op,250);x.workflow.simulate_next(x.uid,x.start(op,250));x.finish(op)
    quantities=dict(selected='5.6',pass_through='8.4',remainder='0',retained='0',loss='0',sample='0',reason='Final container totals',evidence=x.evidence)
    key='collect-occupied-weights';rid=x.workflow.reconcile(x.uid,key,op,quantities)
    assert x.workflow.reconcile(x.uid,key,op,quantities)==rid
    x.workflow.release(x.uid,op,accept_estimates=True,reason='Reviewed unknown combined composition')
    with x.db.read() as s:
        outputs=list(s.scalars(select(Output).filter_by(operation_id=op)))
        assert sum(o.mass_mg for o in outputs)==14000000
        assert s.scalar(select(func.count()).select_from(Input).where(Input.operation_id==op))==3
        assert all(balance(s,pid)==0 and s.get(Portion,pid).status=='consumed' for pid in (source,old_s,old_p))
        for output in outputs:
            old=old_s if output.stream=='selected' else old_p
            assert {l.parent_id for l in s.scalars(select(Lineage).filter_by(child_id=output.portion_id))}=={source,old}
            assert s.get(Portion,output.portion_id).quality=='released'
            profile=s.scalar(select(Profile).filter_by(portion_id=output.portion_id))
            assert profile.data['destination_combination']['previous_portion_id']==old
        assert s.scalar(select(func.sum(Ledger.delta_mg)))==14000000
        assert s.scalar(select(func.count()).select_from(Assignment).where(Assignment.active==True))==2


def test_zero_addition_preserves_old_population_and_final_less_than_start_is_rejected(system):
    x=system;source,*_=x.receive('2');old,selected,*_=x.receive('5');passed=empty(x)
    op,_=prepare_into(x,source,selected,passed);x.start(op,250);x.finish(op)
    with pytest.raises(DomainError,match='starting'):
        x.reconcile(op,selected='4.9',pass_through='2.1')
    x.reconcile(op,selected='5',pass_through='2')
    x.workflow.release(x.uid,op,accept_estimates=True,reason='No net addition to Selected')
    with x.db.read() as s:
        assert s.scalar(select(Assignment).filter_by(container_id=selected,active=True)).portion_id==old
        assert available(s,s.get(Portion,old))==5000000
        assert not s.scalar(select(Output).filter_by(operation_id=op,stream='selected'))


def test_combined_reference_population_counts_photos_and_further_sort(system,tmp_path):
    x=system;_,recipe,_,_,_,first=first_pass(x,tmp_path)
    old,selected,*_=x.receive('1');passed=empty(x)
    with x.db.read() as s:amount=balance(s,first['selected'])
    op,_=prepare_into(x,first['selected'],selected,passed,kg(amount),recipe);generate(x,op,count=300);x.finish(op)
    with x.db.read() as s:
        operation=s.get(Operation,op);context=weighing_context(s,operation,'simulation');form={}
        assert context['input_total_mg']==amount+1000000
        for row in context['rows']:
            if row['stream']=='selected':assert Decimal(row['gross_value'])>Decimal('1.5')
            form.update({'gross_'+row['stream']:row['gross_value'],'revision_'+row['stream']:str(row['container'].revision),'weighed_'+row['stream']:'on'})
        review=build_review(s,operation,'simulation',form)
    x.workflow.reconcile(x.uid,'reference-occupied-weights',op,review['data'])
    x.workflow.release(x.uid,op,accept_estimates=True,reason='Explicit simulation review',approve_discrepancy=True)
    with x.db.read() as s:
        child=s.scalar(select(Output).filter_by(operation_id=op,stream='selected')).portion_id
        profile=s.scalar(select(Profile).filter_by(portion_id=child)).data
        combined=profile['combined_population'];assert len(combined['weights'])>1
        from beanosort.portion_labels import describe_portion
        assert 'Selected addition · Combined with existing contents' in describe_portion(s, child)
        assert Decimal(profile['estimated_count'])==Decimal(combined['before_count'])+Decimal(combined['added_count'])
        assert combined['physical_confirmation'] is False
        assert all(weight>0 for _,weight in combined['weights'])
        final=balance(s,child)
    from test_web import client_for
    client=client_for(x)
    page=client.get('/portions/'+child);assert profile['population_analysis_url'].replace('&','&amp;') in page.text
    chart=client.get(profile['population_analysis_url']);assert chart.status_code==200 and 'Combined container population' in chart.text
    from urllib.parse import urlsplit,parse_qsl
    args=dict(parse_qsl(urlsplit(profile['population_analysis_url']).query));args.update(feature='lab_l',bucket=0)
    inspected=client.get('/jobs/'+op+'/populations/inspect',params=args)
    assert inspected.status_code==200 and inspected.json()['collection'] is True
    assert inspected.json()['count_unit']=='estimated beans'
    client.close()
    new,_=prepare(x,child,recipe,kg(final));run=generate(x,new,count=300)
    from beanosort.models import Particle
    with x.db.read() as s:
        assert {p.payload['reference_bean']['bean_sequence'] for p in s.scalars(select(Particle).filter_by(run_id=run))} <= {seq for seq,_ in combined['weights']}


def test_held_or_stale_baseline_and_database_guard(system):
    x=system;source,*_=x.receive('2');old,selected,*_=x.receive('5');passed=empty(x)
    x.workflow.set_hold(x.uid,old,'Do not mix')
    with pytest.raises(DomainError,match='released'):prepare_into(x,source,selected,passed)
    with pytest.raises(DBAPIError):
        with x.db.write() as s:
            s.add(Destination(id=identity('destination'),operation_id=s.get(Portion,source).created_by,container_id=selected,stream='invalid',readiness={},active=True))
