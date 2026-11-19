"""Combining stock into an ingredient vessel or other occupied vessel."""
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from beanosort.handling import MaterialHandling, new_operation
from beanosort.models import (Assignment, Container, Destination, HandlingPlan, Input, Ledger,
                             Lineage, Operation, Portion, Reservation, User, identity)
from beanosort.quantities import DomainError, kg
from beanosort.service import active_assignment, available, balance
from test_handling import execute, release
from test_web import client_for, token


def plan(x, portions, destination, **extra):
    return dict(portion_ids=portions, container_id=destination,
                station_id=x.stations['Receiving desk'], policy_id=x.versions['reconciliation_policy'],
                ready=True, **extra)


@pytest.mark.parametrize('destination_case', ['first_source', 'second_source', 'another_occupied', 'one_source_plus_destination'])
def test_combining_counts_every_ingredient_once_and_preserves_mass_and_ancestry(system, destination_case):
    x=system
    a,ca,*_=x.receive('0.010001')
    b,cb,*_=x.receive('0.020002')
    amounts={a:10001,b:20002}
    sources=[a,b]
    if destination_case=='another_occupied':
        c,destination,*_=x.receive('0.030003')
        amounts[c]=30003
    else:
        destination=ca if destination_case=='first_source' else cb
    if destination_case=='one_source_plus_destination':sources=[a]
    data=plan(x,sources,destination)
    original_sources=list(sources)
    h=MaterialHandling(x.db)
    key='blend-'+uuid4().hex
    op=h.prepare(x.uid,key,'blend',data)
    assert h.prepare(x.uid,key,'blend',data)==op
    assert sources==original_sources  # Auto-inclusion cannot mutate the command used for replay.
    with x.db.read() as s:
        assert s.get(Operation,op).boundary_mg==sum(amounts.values())
        inputs=list(s.scalars(select(Input).filter_by(operation_id=op)))
        reservations=list(s.scalars(select(Reservation).filter_by(operation_id=op,active=True)))
        assert {i.portion_id:i.mass_mg for i in inputs}==amounts
        assert {i.portion_id:i.mass_mg for i in reservations}==amounts
        assert len(reservations)==len(amounts)
        assert all(available(s,s.get(Portion,pid))==0 for pid in amounts)
        pinned=s.scalar(select(Destination).filter_by(operation_id=op))
        assert pinned.readiness['existing_mass_mg']==amounts[pinned.readiness['existing_portion_id']]
    actual={pid:kg(value) for pid,value in amounts.items()}
    execute(x,h,op,output_mass=kg(sum(amounts.values())),actual_inputs=actual)
    release(x,op)
    with x.db.read() as s:
        assignment=active_assignment(s,container_id=destination)
        assert assignment.portion_id not in amounts
        assert available(s,s.get(Portion,assignment.portion_id))==sum(amounts.values())
        assert all(balance(s,pid)==0 and s.get(Portion,pid).status=='consumed' for pid in amounts)
        assert set(s.scalars(select(Lineage.parent_id).filter_by(child_id=assignment.portion_id)))==set(amounts)
        assert s.scalar(select(func.count()).select_from(Assignment).filter_by(active=True))==1
        assert sum(balance(s,p.id) for p in s.scalars(select(Portion)))==sum(amounts.values())
        assert not list(s.scalars(select(Reservation).filter_by(operation_id=op,active=True)))
        assert not list(s.scalars(select(Destination).filter_by(operation_id=op,active=True)))


def test_blend_cancel_releases_existing_destination_and_other_sources(system):
    x=system;a,ca,*_=x.receive('1');b,cb,*_=x.receive('2')
    h=MaterialHandling(x.db);data=plan(x,[a],cb)
    op=h.prepare(x.uid,'first-blend','blend',data)
    with pytest.raises(DomainError):h.prepare(x.uid,'competing-blend','blend',data)
    with pytest.raises(DomainError):x.prepare(b,'2')
    x.workflow.transition(x.uid,op,'cancelled',reason='Not combined')
    with x.db.read() as s:
        assert available(s,s.get(Portion,a))==1000000
        assert available(s,s.get(Portion,b))==2000000
        assert active_assignment(s,container_id=ca).portion_id==a
        assert active_assignment(s,container_id=cb).portion_id==b
    assert h.prepare(x.uid,'replacement-blend','blend',data)!=op


def test_combined_capacity_and_stale_contents_fail_before_reserving(system):
    x=system;a,ca,*_=x.receive('1');b,cb,*_=x.receive('2')
    h=MaterialHandling(x.db)
    with pytest.raises(DomainError,match='contents changed'):
        h.prepare(x.uid,'stale-contents','blend',plan(x,[a],cb,destination_portion_id=a))
    with x.db.write() as s:s.get(Container,cb).capacity_mg=2500000
    with pytest.raises(DomainError,match='capacity'):
        h.prepare(x.uid,'over-capacity','blend',plan(x,[a,b],cb))
    with x.db.read() as s:
        assert not list(s.scalars(select(Reservation).filter_by(active=True)))
        assert available(s,s.get(Portion,a))==1000000
        assert available(s,s.get(Portion,b))==2000000


@pytest.mark.parametrize('change', ['hold_before_prepare','hold_before_start','assignment_before_start','assignment_before_execute'])
def test_occupied_blend_destination_is_rechecked_without_consuming_stock(system,change):
    x=system;a,ca,*_=x.receive('1');b,cb,*_=x.receive('2')
    h=MaterialHandling(x.db)
    if change=='hold_before_prepare':
        x.workflow.set_hold(x.uid,b,'Inspect stock')
        with pytest.raises(DomainError,match='destination'):
            h.prepare(x.uid,'held-prepare','blend',plan(x,[a],cb))
        return
    op=h.prepare(x.uid,'prepared','blend',plan(x,[a],cb))
    if change=='assignment_before_execute':h.start(x.uid,op)
    if change=='hold_before_start':x.workflow.set_hold(x.uid,b,'Inspect stock')
    else:
        with x.db.write() as s:active_assignment(s,container_id=cb).active=False
    with pytest.raises(DomainError,match='contents|hold'):
        if change=='assignment_before_execute':
            h.execute(x.uid,'changed-execute',op,dict(output_mass='3',loss='0',actual_inputs={a:'1',b:'2'},
                evidence=x.evidence,reason='Test',physical_confirmation=True))
        else:h.start(x.uid,op)
    with x.db.read() as s:
        assert balance(s,a)==1000000 and balance(s,b)==2000000
        assert not list(s.scalars(select(Ledger).filter_by(operation_id=op)))


def test_actual_contribution_of_existing_destination_is_required(system):
    x=system;a,ca,*_=x.receive('1');b,cb,*_=x.receive('2')
    h=MaterialHandling(x.db);op=h.prepare(x.uid,'prepare-blend','blend',plan(x,[a],cb))
    h.start(x.uid,op)
    with pytest.raises(DomainError,match='actual contributions'):
        h.execute(x.uid,'execute-blend',op,dict(output_mass='3',loss='0',actual_inputs={a:'1'},
            evidence=x.evidence,reason='Actual work',physical_confirmation=True))
    with x.db.read() as s:assert balance(s,a)==1000000 and balance(s,b)==2000000


@pytest.mark.parametrize('kind,include_input,right_assignment,allowed',[
    ('blend',True,True,True),('blend',False,True,False),
    ('blend',True,False,False),('transfer',True,True,False)])
def test_database_guard_requires_the_pinned_blend_input(system,kind,include_input,right_assignment,allowed):
    x=system;pid,cid,*_=x.receive('1')
    def insert():
        with x.db.write() as s:
            user=s.get(User,x.uid);op=new_operation(s,user,kind,x.stations['Receiving desk'],1000000,'simulation')
            assigned=active_assignment(s,container_id=cid)
            s.add(Reservation(id=identity('reservation'),operation_id=op.id,portion_id=pid,mass_mg=1000000))
            if include_input:s.add(Input(operation_id=op.id,portion_id=pid,mass_mg=1000000))
            s.flush()
            s.add(Destination(id=identity('destination'),operation_id=op.id,container_id=cid,stream='output',
                readiness=dict(existing_portion_id=pid,existing_mass_mg=1000000,
                               existing_assignment_id=assigned.id if right_assignment else 'stale')))
    if allowed:insert()
    else:
        with pytest.raises(DBAPIError):insert()


@pytest.mark.parametrize('whole', [True,False])
def test_web_move_contents_accepts_occupied_destination(system,whole):
    from test_material_web import values
    from test_material_actions import state
    x=system;a,ca,*_=x.receive('1',label='Small source');b,cb,*_=x.receive('2',label='Destination with beans')
    client=client_for(x);page=client.get('/handling/new?kind=blend')
    assert page.url.path=='/handling/contents'
    assert '2.000 kg contents · nominal capacity 1000.000 kg' in page.text
    data={**values(page),'source_id':ca,'destination_id':cb,'moved_mass':'1' if whole else '0.5'}
    if whole:data['empty_source']='on'
    response=client.post('/handling/contents',data=data)
    assert response.status_code==200,response.text
    assert 'Movement recorded' in response.text
    assert state(x,cb)['mass_mg']==(3000000 if whole else 2500000)
    assert client.post('/handling/contents',data=data).url==response.url


def test_web_rejects_a_destination_that_changed_since_review(system):
    from beanosort.material import MaterialActions
    from test_material_web import values
    from test_material_actions import adjust_data
    x=system;a,ca,*_=x.receive('1');b,cb,*_=x.receive('2')
    client=client_for(x);page=client.get('/handling/contents')
    data={**values(page),'source_id':ca,'destination_id':cb,'moved_mass':'1','empty_source':'on'}
    MaterialActions(x.db).reconcile(x.uid,'new-destination-weight',adjust_data(x,cb,weight='1.9'))
    response=client.post('/handling/contents',data=data)
    assert response.status_code==409 and 'changed' in response.text
