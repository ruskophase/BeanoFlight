from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from beanosort.cli import initialize
from beanosort.models import Assignment, Audit, Container, Ledger, Location, Measurement, User
from beanosort.quantities import DomainError
from beanosort.service import balance
from test_handling import prepare, execute
from test_web import client_for, token


def add(x, name):
    return x.workflow.create_location(x.uid,"add-"+uuid4().hex,name)


def test_rename_location_updates_vessel_display_preserves_contents_and_history(system):
    x=system
    pid,cid,*_=x.receive("1")
    with x.db.read() as s:
        lid=s.get(Container,cid).location_id
        original_measurements=[(m.id,m.mass_mg,m.provenance) for m in s.scalars(select(Measurement))]
    for _ in range(2):
        assert x.workflow.update_location(x.uid,"rename-place-once",lid,name="Shelf -- Green beans",revision=1)==lid
    with x.db.read() as s:
        c=s.get(Container,cid)
        assert c.location=="Shelf -- Green beans" and c.location_id==lid and c.revision==2
        assert balance(s,pid)==1000000
        assert s.get(Location,lid).revision==2
        assert [(m.id,m.mass_mg,m.provenance) for m in s.scalars(select(Measurement))]==original_measurements
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=="location_updated"))==1
        assert s.scalar(select(func.count()).select_from(Ledger))==1
    assert "Shelf -- Green beans" in client_for(x).get('/containers/'+cid).text


def test_location_edit_validation_and_competing_edit_revisions(system):
    x=system
    lid=add(x,"Original")
    add(x,"Existing name")
    for name in ('',' ','a'*201,'EXISTING NAME'):
        with pytest.raises(DomainError):
            x.workflow.update_location(x.uid,"bad-edit-"+uuid4().hex,lid,name=name,revision=1)
    def save(index):
        try:
            return x.workflow.update_location(x.uid,"concurrent-location-"+str(index),lid,name="Edited "+str(index),revision=1)
        except DomainError:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(save,(0,1)))
    assert results.count('conflict')==1
    with pytest.raises(DomainError,match='another session'):
        x.workflow.delete_location(x.uid,'stale-location-delete',lid,revision=1,confirmed=True)


def test_delete_location_requires_no_containers_including_archived_and_keeps_old_id(system):
    x=system
    lid=add(x,'Remove me')
    cid=x.workflow.create_container(x.uid,label='Stored empty bin',location_id=lid,capacity='25',tare_mass='0.5')
    for archived in (False,True):
        if archived:
            x.workflow.retire_container(x.uid,'archive-stored-bin',cid,revision=1,action='archive')
        with pytest.raises(DomainError,match='contains containers'):
            x.workflow.delete_location(x.uid,'blocked-delete-'+str(archived),lid,revision=1,confirmed=True)
    x.workflow.retire_container(x.uid,'delete-stored-bin',cid,revision=2,action='delete',confirmed=True)
    with pytest.raises(DomainError,match='Confirm deletion'):
        x.workflow.delete_location(x.uid,'unconfirmed-location',lid,revision=1)
    for _ in range(2):
        assert x.workflow.delete_location(x.uid,'delete-place-once',lid,revision=1,confirmed=True)==lid
    replacement=add(x,'Remove me')
    assert replacement!=lid
    client=client_for(x)
    page=client.get('/locations/'+lid)
    assert page.status_code==410 and 'Deleted location' in page.text
    assert 'Remove me' in page.text and replacement not in page.text
    client.close()


def test_empty_used_vessel_delete_preserves_lineage_and_releases_storage_membership(system):
    x=system
    pid,cid,*_=x.receive('1',label='Used vessel')
    with x.db.read() as s:
        old_location=s.get(Container,cid).location_id
    with pytest.raises(DomainError,match='contents'):
        x.workflow.retire_container(x.uid,'occupied-bin-delete',cid,revision=1,action='delete',confirmed=True)
    op,handler,_=prepare(x,'transfer',[pid]);execute(x,handler,op)
    for _ in range(2):
        x.workflow.retire_container(x.uid,'empty-used-bin-delete',cid,revision=1,action='delete',confirmed=True)
    x.workflow.delete_location(x.uid,'delete-former-bin-location',old_location,revision=1,confirmed=True)
    with x.db.read() as s:
        c=s.get(Container,cid)
        assert c.deleted and c.location_id is None
        assert s.scalar(select(func.count()).select_from(Assignment).where(Assignment.container_id==cid))==1
        assert balance(s,pid)==1000000
    client=client_for(x)
    assert 'Used vessel' not in client.get('/containers?include_archived=true').text
    assert pid in client.get('/containers/'+cid).text
    assert client.get('/locations/'+old_location).status_code==410
    with pytest.raises(DomainError,match='deleted'):
        x.workflow.receive(x.uid,'deleted-bin-receipt',dict(container_id=cid,station_id=x.stations['Receiving desk'],net_mass='1',ready=True,evidence=x.evidence))
    client.close()


def test_delete_default_location_is_not_recreated_by_initialization(system):
    x=system
    with x.db.read() as s:
        lid=s.scalar(select(Location.id).filter_by(name='Floor'))
    x.workflow.delete_location(x.uid,'delete-default-floor',lid,revision=1,confirmed=True)
    initialize(x.db,'admin','test-password-123')
    with x.db.read() as s:
        assert s.scalar(select(Location.id).filter_by(name='Floor')) is None


def test_location_management_forms_and_permissions(system):
    x=system
    lid=add(x,'Editable place')
    client=client_for(x)
    page=client.get('/locations/'+lid+'/edit')
    data=dict(csrf=token(page),key=token(page,'key'),revision='1',name='Renamed place')
    assert client.post('/locations/'+lid+'/edit',data={**data,'csrf':'wrong'}).status_code==403
    page=client.post('/locations/'+lid+'/edit',data=data)
    assert page.status_code==200 and 'Renamed place' in page.text
    assert 'href="/locations/'+lid+'/delete"' in page.text
    page=client.get('/locations/'+lid+'/delete')
    assert page.status_code==200 and 'Delete location' in page.text
    with x.db.write() as s:
        s.get(User,x.uid).role='operator'
    denied=client.post('/locations/'+lid+'/delete',data=dict(csrf=token(page),key='operator-cannot-delete',revision='2',confirmed='on'))
    assert denied.status_code==409 and 'supervisor/admin' in denied.text
    assert client.get('/locations/'+lid+'/delete').status_code==409
    assert 'href="/locations/'+lid+'/delete"' not in client.get('/locations/'+lid).text
    client.close()


def test_location_delete_confirmation_is_read_only_and_rechecks_containers(system):
    x=system
    lid=add(x,'Clear before deleting')
    client=client_for(x)
    page=client.get('/locations/'+lid+'/delete')
    assert page.status_code==200 and 'name="confirmed"' in page.text
    with x.db.read() as s:
        assert s.get(Location,lid) is not None
        assert not s.scalar(select(Audit.id).filter_by(action='location_deleted'))
    data=dict(csrf=token(page),key=token(page,'key'),revision='1',confirmed='on')
    assert client.post('/locations/'+lid+'/delete',data={**data,'csrf':'wrong'}).status_code==403
    assert client.post('/locations/'+lid+'/delete',data={**data,'confirmed':''}).status_code==409
    cid=x.workflow.create_container(x.uid,label='Arrived before deletion',location_id=lid,capacity='25',tare_mass='0.5')
    page=client.get('/locations/'+lid+'/delete')
    assert 'Arrived before deletion' in page.text and 'still contains' in page.text
    assert '<form action="/locations/'+lid+'/delete"' not in page.text
    assert client.post('/locations/'+lid+'/delete',data=data).status_code==409
    with x.db.read() as s:
        floor=s.scalar(select(Location.id).filter_by(name='Floor'))
    x.workflow.move_container(x.uid,'clear-location-before-delete',cid,location_id=floor,revision=1)
    page=client.get('/locations/'+lid+'/delete')
    assert 'name="confirmed"' in page.text
    for _ in range(2):
        response=client.post('/locations/'+lid+'/delete',data=data)
        assert response.status_code==200 and response.url.path=='/locations'
    assert client.get('/locations/'+lid).status_code==410
    with x.db.read() as s:
        assert s.get(Container,cid).location_id==floor
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=='location_deleted'))==1
    client.close()
