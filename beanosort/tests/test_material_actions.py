from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from beanosort.material import MaterialActions, container_state
from beanosort.models import (Assignment, Audit, Container, HandlingPlan, Input, Ledger,
                             Lineage, Measurement, Operation, Output, Portion, Profile, Reconciliation)
from beanosort.quantities import DomainError
from beanosort.service import active_assignment, balance, trace


def empty(x, capacity='1000'):
    return x.workflow.create_container(x.uid, label='Empty '+uuid4().hex, location='Test shelf', capacity=capacity, tare_mass='0.5')


def state(x, cid):
    with x.db.read() as s:
        return container_state(s, s.get(Container, cid))


def move_data(x, source, destination, **changes):
    return {**dict(source_id=source, destination_id=destination, source_state=state(x, source),
                destination_state=state(x, destination), moved_mass='0.5', empty_source=False,
                representative=True, confirmed=True, reason=''), **changes}


def adjust_data(x, cid, **changes):
    return {**dict(container_id=cid, container_state=state(x, cid), weight='1.25', weighing_basis='net',
                   representative=True, confirmed=True, reason='Current scale reading'), **changes}


def record(x, op):
    with x.db.read() as s:
        return s.scalar(select(HandlingPlan).filter_by(operation_id=op)).data


@pytest.mark.parametrize('whole',[False,True])
@pytest.mark.parametrize('occupied',[False,True])
def test_four_movements_preserve_mass_identity_and_ancestry(system, whole, occupied):
    x=system;source,cs,*_=x.receive('2')
    target,cd,*_=x.receive('1') if occupied else (None,empty(x),None,None)
    data=move_data(x,cs,cd);data.update(empty_source=whole,moved_mass='2' if whole else '0.5')
    action=MaterialActions(x.db);op=action.move_contents(x.uid,'movement-'+uuid4().hex,data)
    with x.db.read() as s:
        operation=s.get(Operation,op)
        assert (operation.status,operation.release_status)==('completed','released')
        assert s.scalar(select(func.sum(Ledger.delta_mg)))==(3000000 if occupied else 2000000)
        assert s.scalar(select(func.count()).select_from(Reconciliation))==0
        out=active_assignment(s,container_id=cd).portion_id
        assert balance(s,out)==(1000000 if occupied else 0)+(2000000 if whole else 500000)
        if whole and not occupied:
            assert out==source and s.get(Portion,source).status=='stock'
        else:
            assert out!=source and s.get(Portion,source).status=='consumed'
            parents=set(s.scalars(select(Lineage.parent_id).filter_by(child_id=out)))
            assert parents==({source,target} if occupied else {source})
        remainder=active_assignment(s,container_id=cs)
        if whole:
            assert remainder is None
        else:
            assert remainder.portion_id!=source and balance(s,remainder.portion_id)==1500000
            assert list(s.scalars(select(Lineage.parent_id).filter_by(child_id=remainder.portion_id)))==[source]
        assert s.get(Portion,out).quality=='released'


@pytest.mark.parametrize('supplied', [None,'','1.98','2.02','NaN'])
def test_complete_movement_uses_recorded_balance_without_weighing(system,supplied):
    x=system;pid,cs,*_=x.receive('2');cd=empty(x)
    data=move_data(x,cs,cd);data.update(empty_source=True,representative=False,reason='')
    if supplied is None:data.pop('moved_mass')
    else:data['moved_mass']=supplied
    action=MaterialActions(x.db);key='whole-'+uuid4().hex;op=action.move_contents(x.uid,key,data)
    assert action.move_contents(x.uid,key,data)==op
    contents=record(x,op)
    assert contents['weight_adjustment_mg']==0 and contents['moved_mg']==2000000
    assert contents['transfer_basis']=='recorded_source_balance'
    with x.db.read() as s:
        assert active_assignment(s,container_id=cd).portion_id==pid
        assert balance(s,pid)==2000000 and active_assignment(s,container_id=cs) is None
        assert list(s.scalars(select(Ledger).filter_by(operation_id=op)))==[]
        assert s.scalar(select(func.count()).select_from(Measurement).filter_by(operation_id=op))==0
    with pytest.raises(DomainError,match='different content'):
        action.move_contents(x.uid,key,{**data,'reason':'Different submission'})


@pytest.mark.parametrize('change',[
    {'moved_mass':'0'}, {'moved_mass':'3'}, {'moved_mass':'2'},
    {'confirmed':False}, {'moved_mass':'NaN'},
])
def test_invalid_movement_rolls_back_without_changing_stock(system,change):
    x=system;_,cs,*_=x.receive('2');cd=empty(x);before=state(x,cs)
    data=move_data(x,cs,cd);data.update(change)
    with pytest.raises(DomainError):MaterialActions(x.db).move_contents(x.uid,'invalid-'+uuid4().hex,data)
    assert state(x,cs)==before and state(x,cd)['portion_id'] is None
    with x.db.read() as s:assert s.scalar(select(func.count()).select_from(HandlingPlan))==0


def test_capacity_and_active_job_locks_are_checked_before_any_posting(system):
    x=system;pid,cs,*_=x.receive('2');cd=empty(x,'0.4');action=MaterialActions(x.db)
    with pytest.raises(DomainError,match='capacity'):action.move_contents(x.uid,'capacity-check',move_data(x,cs,cd))
    op,_=x.prepare(pid,'1')
    with pytest.raises(DomainError,match='active jobs'):action.move_contents(x.uid,'reserved-source',move_data(x,cs,empty(x)))
    with pytest.raises(DomainError,match='active jobs'):action.reconcile(x.uid,'reserved-weighing',adjust_data(x,cs))
    x.workflow.delete_prepared_sorting(x.uid,'remove-plan',op,confirm_unstarted=True)
    assert state(x,cs)['mass_mg']==2000000


def test_same_container_and_stale_weighings_are_rejected(system):
    x=system;_,cs,*_=x.receive('2');cd=empty(x);action=MaterialActions(x.db)
    with pytest.raises(DomainError,match='different source'):
        action.move_contents(x.uid,'same-container',move_data(x,cs,cs))
    old=move_data(x,cs,cd)
    action.reconcile(x.uid,'adjust-before-transfer',adjust_data(x,cs,weight='1.9'))
    with pytest.raises(DomainError,match='changed'):
        action.move_contents(x.uid,'stale-movement',old)
    assert state(x,cs)['mass_mg']==1900000 and state(x,cd)['portion_id'] is None


def test_hold_is_inherited_by_both_remainder_and_combined_contents(system):
    x=system;pid,cs,*_=x.receive('2');_,cd,*_=x.receive('1')
    x.workflow.set_hold(x.uid,pid,'Keep for inspection')
    op=MaterialActions(x.db).move_contents(x.uid,'held-material-movement',move_data(x,cs,cd))
    with x.db.read() as s:
        for cid in (cs,cd):
            portion=s.get(Portion,active_assignment(s,container_id=cid).portion_id)
            assert portion.quality=='held' and portion.hold_reason=='Keep for inspection'
        assert s.get(Operation,op).status=='completed'


@pytest.mark.parametrize('environment',['simulation','real'])
def test_reconciliation_retains_identity_records_measured_difference_and_frees_zero(factory,environment):
    x=factory(environment);pid,cid,*_=x.receive('2');action=MaterialActions(x.db)
    data=adjust_data(x,cid,weight='2.0',weighing_basis='gross')
    key='reweigh-'+uuid4().hex;op=action.reconcile(x.uid,key,data)
    assert action.reconcile(x.uid,key,data)==op
    with x.db.read() as s:
        assert active_assignment(s,container_id=cid).portion_id==pid and balance(s,pid)==1500000
        reading=s.scalar(select(Measurement).filter_by(operation_id=op,role='net'))
        assert reading.mass_mg==1500000 and reading.provenance['tare_mg']==500000
        assert reading.provenance['kind']==('simulation' if environment=='simulation' else 'direct_measurement')
        assert s.scalar(select(func.count()).select_from(Measurement))==3
    assert record(x,op)['weight_adjustment_mg']==-500000
    action.reconcile(x.uid,'empty-by-weighing',adjust_data(x,cid,weight='0.5',weighing_basis='gross'))
    assert state(x,cid)['portion_id'] is None
    with x.db.read() as s:assert s.get(Portion,pid).status=='consumed' and balance(s,pid)==0


def test_weighing_above_nominal_is_truthfully_recorded_and_new_found_contents_are_unknown(system):
    x=system;cid=empty(x,'1');action=MaterialActions(x.db)
    op=action.reconcile(x.uid,'found-unrecorded',adjust_data(x,cid,weight='1.1'))
    data=record(x,op)
    assert data['above_nominal_mg']==100000 and data['before_mg']==0
    with x.db.read() as s:
        pid=active_assignment(s,container_id=cid).portion_id
        assert balance(s,pid)==1100000
        assert s.scalar(select(Profile).filter_by(operation_id=op)).data['material_population_unknown']
        assert list(s.scalars(select(Lineage).filter_by(child_id=pid)))==[]


def test_two_concurrent_moves_cannot_spend_the_same_snapshot_twice(system):
    x=system;_,cs,*_=x.receive('2');destinations=[empty(x),empty(x)]
    plans=[move_data(x,cs,cid) for cid in destinations]
    def attempt(index):
        try:return MaterialActions(x.db).move_contents(x.uid,'concurrent-'+str(index),plans[index])
        except DomainError:return None
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(attempt,range(2)))
    assert sum(bool(value) for value in results)==1
    assert state(x,cs)['mass_mg']==1500000


def test_partial_movement_from_legacy_sorting_without_reference_population(system):
    x=system;pid,*_=x.receive('2');op,plan=x.prepare(pid,'2');run=x.start(op,10)
    while x.workflow.simulate_next(x.uid,run):pass
    x.finish(op);x.reconcile(op,selected='0.5',pass_through='1.5')
    x.workflow.release(x.uid,op,accept_estimates=True,reason='Release legacy fixture')
    destination=empty(x)
    moved=MaterialActions(x.db).move_contents(x.uid,'legacy-no-reference',move_data(
        x,plan['selected_id'],destination,moved_mass='0.1'))
    assert state(x,plan['selected_id'])['mass_mg']==400000
    assert state(x,destination)['mass_mg']==100000
    with x.db.read() as s:
        profile=s.scalar(select(Profile).filter_by(operation_id=moved,stream='moved'))
        assert not profile.data.get('retained_population')
