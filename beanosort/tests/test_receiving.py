from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError

from beanosort.cli import initialize, migrate
from beanosort.db import Database
from beanosort.models import (Assignment, Audit, Container, Ledger, Location, Measurement, Operation,
                             Output, Portion, Profile, Receipt, SimulationAssumption, Station, now)
from beanosort.quantities import DomainError, receiving_capacity_limit_mg
from beanosort.service import Workflow, balance
from test_handling import execute as execute_handling, prepare as prepare_handling
from test_web import client_for, token


def setup_loads(x, masses=("30.125", "29.875")):
    place = x.workflow.create_location(x.uid, "new-beans-"+uuid4().hex, "Shelf --- NewBeans")
    rows = [{"container_id": x.workflow.create_container(x.uid, label="Receiving " + uuid4().hex,
        location="Floor", tare_mass="0.500", capacity="40.000"), "net_mass": amount,
        "location_id": place, "revision": 1, "tared": True} for amount in masses]
    return place, rows


def receive(x, rows, key=None, **extra):
    return x.workflow.receive_loads(x.uid, key or "decant-"+uuid4().hex,
        {"supplier":"Coffee supplier", "supplier_lot":"SUPPLIER-LOT-60", "bean_type":"Green coffee"}, rows,
        station_id=x.stations["Receiving desk"], ready=extra.get("ready",True), placed=extra.get("placed",True))


def test_one_source_multiple_containers_actual_net_mass_only_and_retry(system):
    x = system
    place, rows = setup_loads(x)
    rid = receive(x, rows, key="same-decant-command")
    assert receive(x, rows, key="same-decant-command") == rid
    with x.db.read() as s:
        receipt = s.get(Receipt,rid)
        outputs = list(s.scalars(select(Output).filter_by(operation_id=receipt.operation_id)))
        assert len(outputs) == 2
        assert s.get(Operation,receipt.operation_id).boundary_mg == 60000000
        assert sum(o.mass_mg for o in outputs) == 60000000
        assert {s.get(Portion,o.portion_id).receipt_id for o in outputs} == {rid}
        assert sorted(balance(s,o.portion_id) for o in outputs) == [29875000,30125000]
        assert s.scalar(select(func.count()).select_from(Receipt)) == 1
        assert s.scalar(select(func.count()).select_from(Ledger)) == 2
        assert s.scalar(select(func.count()).select_from(Profile)) == 0
        assert s.scalar(select(func.count()).select_from(SimulationAssumption)) == 0
        for c in s.scalars(select(Container)):
            assert c.location_id == place
        for m in s.scalars(select(Measurement)):
            assert m.provenance["registered_tare_mg"] == 500000
            assert m.provenance["scale_zeroed_confirmation"] is True
            assert m.provenance["entry_decimal_places"] == 3
            assert not m.provenance.get("precision_mg") and not m.provenance.get("device")


def test_invalid_loads_are_atomic_and_require_empty_containers_scale_and_placement(system):
    x=system
    _, rows=setup_loads(x)
    for changed in ([rows[0],rows[0]], [rows[0],{**rows[1],"net_mass":"1.2345"}],
                    [rows[0],{**rows[1],"net_mass":"48.001"}], [rows[0],{**rows[1],"tared":False}],
                    [rows[0],{**rows[1],"location_id":"missing"}], [rows[0],{**rows[1],"revision":0}]):
        with pytest.raises(DomainError):
            receive(x,changed)
    for confirmation in ({"ready":False},{"placed":False}):
        with pytest.raises(DomainError):
            receive(x,rows,**confirmation)
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Receipt)) == 0
        assert s.scalar(select(func.count()).select_from(Assignment)) == 0
        assert all(c.location == "Floor" for c in s.scalars(select(Container)))
    receive(x,rows)
    with pytest.raises(DomainError,match="already has contents"):
        receive(x,rows)


def test_two_operators_cannot_receive_into_the_same_container(system):
    x=system
    _, rows=setup_loads(x,("12.345",))
    def record(i):
        try:
            return receive(x,rows,key="competing-receipt-"+str(i))
        except DomainError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(record,(0,1)))
    assert results.count("conflict") == 1
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Receipt)) == 1
        assert s.scalar(select(func.sum(Ledger.delta_mg))) == 12345000


@pytest.mark.parametrize('environment', ['real', 'simulation'])
def test_receiving_allows_twenty_percent_without_changing_nominal_capacity(factory, environment):
    x=factory(environment)
    _,rows=setup_loads(x,('40.001','48.000'))
    rid=receive(x,rows,key='overfilled-receiving')
    assert receive(x,rows,key='overfilled-receiving')==rid
    with x.db.read() as s:
        receipt=s.get(Receipt,rid)
        assert s.get(Operation,receipt.operation_id).boundary_mg==88001000
        assert sorted(balance(s,a.portion_id) for a in s.scalars(select(Assignment)))==[40001000,48000000]
        assert all(c.capacity_mg==40000000 for c in s.scalars(select(Container)))
        measurements=list(s.scalars(select(Measurement)))
        assert sorted(m.mass_mg for m in measurements)==[40001000,48000000]
        for m in measurements:
            assert m.provenance['kind']==('direct_measurement' if environment=='real' else 'simulation')
            assert m.provenance['receiving_capacity']=={
                'policy':'nominal-capacity-120-percent/v1','nominal_capacity_mg':40000000,
                'receiving_limit_mg':48000000,'above_nominal_mg':m.mass_mg-40000000}
        assert s.scalar(select(func.count()).select_from(Ledger))==2


def test_overfilled_container_can_move_and_be_edited_without_reducing_capacity(system):
    x=system
    place,rows=setup_loads(x,('48.000',))
    receive(x,rows)
    cid=rows[0]['container_id']
    with x.db.read() as s:
        c=s.get(Container,cid)
        revision=c.revision
        floor=s.scalar(select(Location.id).filter_by(name='Floor'))
        original_measurements=[(m.id,m.mass_mg,m.provenance) for m in s.scalars(select(Measurement))]
    x.workflow.move_container(x.uid,'move-overfilled-container',cid,location_id=floor,revision=revision)
    x.workflow.update_container(x.uid,'rename-overfilled-container',cid,dict(label='Overfilled and renamed',
        location_id=place,kind='bin',capacity='40',tare_mass='0.500',revision=revision+1))
    x.workflow.update_location(x.uid,'rename-overfilled-place',place,name='Renamed overfilled storage',revision=1)
    with pytest.raises(DBAPIError):
        with x.db.engine.begin() as connection:
            connection.execute(text('UPDATE containers SET capacity_mg=39999000 WHERE id=:id'),{'id':cid})
    with x.db.read() as s:
        c=s.get(Container,cid)
        assert c.location=='Renamed overfilled storage' and c.label=='Overfilled and renamed'
        assert c.capacity_mg==40000000
        assert balance(s,s.scalar(select(Assignment).filter_by(container_id=cid,active=True)).portion_id)==48000000
        assert [(m.id,m.mass_mg,m.provenance) for m in s.scalars(select(Measurement))]==original_measurements
        assert s.scalar(select(func.count()).select_from(Ledger))==1


def test_receiving_limit_rounds_down_without_float_rounding():
    assert receiving_capacity_limit_mg(None) is None
    assert receiving_capacity_limit_mg(25000000)==30000000
    assert receiving_capacity_limit_mg(1000001)==1200001
    assert receiving_capacity_limit_mg(10**15)==1200000000000000


@pytest.mark.parametrize('kind',['sorting','split'])
def test_partial_processing_keeps_existing_overfill_in_source_container(system,kind):
    x=system
    _,rows=setup_loads(x,('48.000',))
    receive(x,rows)
    cid=rows[0]['container_id']
    with x.db.read() as s:
        pid=s.scalar(select(Assignment).filter_by(container_id=cid,active=True)).portion_id
    if kind=='sorting':
        op,_=x.prepare(pid,'1')
        run=x.start(op,count=10)
        x.workflow.simulate_next(x.uid,run)
        x.finish(op)
        x.reconcile(op,selected='0.2',pass_through='0.8',remainder='47')
    else:
        op,handler,_=prepare_handling(x,'split',[pid],withdrawn_mass='1')
        execute_handling(x,handler,op,output_mass='1',remainder='47')
    x.workflow.release(x.uid,op,accept_estimates=True,reason='Reviewed partial processing test')
    with x.db.read() as s:
        remainder=s.scalar(select(Assignment).filter_by(container_id=cid,active=True)).portion_id
        assert remainder!=pid and balance(s,pid)==0
        assert balance(s,remainder)==47000000 and s.get(Container,cid).capacity_mg==40000000
        assert sum(balance(s,p.id) for p in s.scalars(select(Portion)))==48000000


def test_real_receiving_is_net_scale_measurement_without_inferred_device_precision(factory):
    x=factory("real")
    _, rows=setup_loads(x,("9.876",))
    rid=receive(x,rows)
    with x.db.read() as s:
        m=s.scalar(select(Measurement).filter_by(operation_id=s.get(Receipt,rid).operation_id))
        assert m.mass_mg == 9876000
        assert m.provenance["kind"] == "direct_measurement"
        assert m.provenance["registered_tare_mg"] == 500000
        assert m.provenance["tare_basis"].startswith("empty receiving container zeroed")
        assert m.provenance.get("precision_mg") is None


def test_receiving_web_four_sections_and_clean_evidence_contract(system):
    x=system
    place,rows=setup_loads(x)
    client=client_for(x)
    page=client.get('/receive')
    assert [page.text.index(title) for title in ('1. Source details','2. Receiving container details','3. Weigh the beans','4. Storage location')] == sorted(page.text.index(title) for title in ('1. Source details','2. Receiving container details','3. Weigh the beans','4. Storage location'))
    for field in ('evidence_kind','simulation_settings_id','estimate_particles','device','precision_mg','gross_mass'):
        assert 'name="'+field+'"' not in page.text
    payload=dict(csrf=token(page),key=token(page,'key'),station_id=x.stations['Receiving desk'],
        receiving_load_id=['0','1'],receiving_container_id=[r['container_id'] for r in rows],
        receiving_container_revision=['1','1'],receiving_net_mass=['30.125','29.875'],
        receiving_location_id=[place,place],tared=['0','1'],ready='on',placed='on',supplier='One supplier')
    assert client.post('/receive',data={**payload,'csrf':'bad'}).status_code == 403
    result=client.post('/receive',data=payload)
    assert result.status_code == 200,result.text
    assert '60.000 kg' in result.text and '30.125 kg' in result.text and '29.875 kg' in result.text
    assert 'remain unknown' in result.text
    assert client.post('/receive',data=payload).url.path == result.url.path
    after=client.get('/receive')
    for row in rows:
        assert 'value="'+row['container_id']+'"' not in after.text
    # Historical receipts can retain finer precision than the new default.
    legacy_pid, *_ = x.receive("1.234567")
    with x.db.read() as s:
        legacy_rid = s.get(Portion, legacy_pid).receipt_id
    assert '1.234567 kg' in client.get('/receipts/'+legacy_rid).text
    client.close()


def test_receiving_web_rejects_above_allowance_then_accepts_exact_limit(system):
    x=system
    place,rows=setup_loads(x,('48.000',))
    client=client_for(x)
    page=client.get('/receive')
    assert 'data-receiving-limit="48"' in page.text
    payload=dict(csrf=token(page),key=token(page,'key'),station_id=x.stations['Receiving desk'],
        receiving_load_id=['0'],receiving_container_id=[rows[0]['container_id']],
        receiving_container_revision=['1'],receiving_net_mass=['48.001'],
        receiving_location_id=[place],tared=['0'],ready='on',placed='on')
    response=client.post('/receive',data=payload)
    assert response.status_code==409 and '120%' in response.text and '48 kg' in response.text
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Receipt))==0
    response=client.post('/receive',data={**payload,'receiving_net_mass':['48.000']})
    assert response.status_code==200 and '48.000 kg' in response.text
    with x.db.read() as s:
        assert s.get(Container,rows[0]['container_id']).capacity_mg==40000000
    client.close()


def test_legacy_missing_weights_are_listed_and_completed_atomically(tmp_path):
    url=f"sqlite:///{tmp_path/'unknown-tare.db'}"
    config=Config(str(Path(__file__).parents[1]/'alembic.ini'));config.attributes['database_url']=url
    command.upgrade(config,'0004')
    engine=create_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO containers (id,created_at,label,kind,location,capacity_mg) VALUES ('unknown',:at,'Unknown tare vessel','bin','Floor',25000000)"),{'at':now()})
        c.execute(text("INSERT INTO containers (id,created_at,label,kind,location) VALUES ('unknown-both',:at,'Unknown both weights','bin','Floor')"),{'at':now()})
    engine.dispose();migrate(url)
    db=Database(url,'simulation');uid=initialize(db,'admin','test-password-123')
    with db.read() as s:
        station=s.scalar(select(Station.id).filter_by(name='Receiving desk'))
        place=s.scalar(select(Location.id).filter_by(name='Floor'))
    with pytest.raises(DomainError,match='empty-container tare'):
        Workflow(db).receive_loads(uid,'unknown-tare-receive',{},[dict(container_id='unknown',net_mass='1.000',location_id=place,tared=True)],station_id=station,ready=True,placed=True)
    client=client_for(type('LegacySystem',(),{'db':db})())
    page=client.get('/receive')
    assert 'value="unknown"' in page.text and 'value="unknown-both"' in page.text
    payload=dict(csrf=token(page),key=token(page,'key'),station_id=station,receiving_load_id=['0'],
        receiving_container_id=['unknown'],receiving_container_revision=['1'],receiving_net_mass=['1.000'],
        receiving_location_id=[place],tared=['0'],ready='on',placed='on')
    assert client.post('/receive',data=payload).status_code==409
    payload.update(receiving_tare_mass=['0.500'],receiving_capacity=[''])
    response=client.post('/receive',data=payload)
    assert response.status_code==200 and '1.000 kg' in response.text
    assert client.post('/receive',data=payload).url.path==response.url.path
    with db.read() as s:
        container=s.get(Container,'unknown')
        assert container.tare_mg==500000 and container.capacity_mg==25000000 and container.revision==2
        assert s.scalar(select(Measurement)).mass_mg==1000000
        assert s.scalar(select(Measurement)).provenance['registered_tare_mg']==500000
        change=s.scalar(select(Audit).filter_by(action='container_updated'))
        assert change.data['before']['tare_mg'] is None and change.data['after']['tare_mg']==500000
    page=client.get('/receive')
    assert 'value="unknown"' not in page.text and 'Already contains material' in page.text
    second={**payload,'key':token(page,'key'),'receiving_container_id':['unknown-both'],
            'receiving_tare_mass':['0.750'],'receiving_capacity':['20'],'receiving_net_mass':['24.001']}
    assert client.post('/receive',data=second).status_code==409
    with db.read() as s:
        container=s.get(Container,'unknown-both')
        assert container.tare_mg is None and container.capacity_mg is None and container.revision==1
    response=client.post('/receive',data={**second,'receiving_net_mass':['24.000']})
    assert response.status_code==200 and '24.000 kg' in response.text
    with db.read() as s:
        container=s.get(Container,'unknown-both')
        assert container.tare_mg==750000 and container.capacity_mg==20000000
        assert s.scalar(select(func.count()).select_from(Receipt))==2
    client.close()
    db.engine.dispose()


def test_receiving_cannot_override_registered_container_weights(system):
    x=system;_,rows=setup_loads(x,('1.000',))
    for changed in ({'tare_mass':'0.6'},{'capacity':'41'}):
        with pytest.raises(DomainError,match='already registered'):
            receive(x,[{**rows[0],**changed}])
    with x.db.read() as s:
        c=s.get(Container,rows[0]['container_id'])
        assert c.tare_mg==500000 and c.capacity_mg==40000000 and c.revision==1
        assert s.scalar(select(func.count()).select_from(Receipt))==0


def test_missing_weight_completion_rolls_back_and_commits_across_backends(system):
    x=system
    # Reproduce historical unknown-weight records only inside the disposable test
    # database, restoring registration guards before exercising the application.
    with x.db.write() as s:
        place=s.scalar(select(Location.id).filter_by(name='Floor'))
        if x.db.engine.dialect.name=='sqlite':
            guard=s.execute(text("SELECT sql FROM sqlite_master WHERE name='container_weights_required'")).scalar_one()
            s.execute(text('DROP TRIGGER container_weights_required'))
        else:
            s.execute(text('ALTER TABLE containers DISABLE TRIGGER container_weights_required'))
        for key,capacity in [('legacy-one',40000000),('legacy-two',None)]:
            s.execute(text('''INSERT INTO containers(id,created_at,label,kind,location,location_id,capacity_mg)
                VALUES (:id,:at,:id,'bin','Floor',:place,:capacity)'''),{'id':key,'at':now(),'place':place,'capacity':capacity})
        if x.db.engine.dialect.name=='sqlite':s.execute(text(guard))
        else:s.execute(text('ALTER TABLE containers ENABLE TRIGGER container_weights_required'))
    rows=[dict(container_id='legacy-one',net_mass='2.000',tare_mass='0.500',location_id=place,tared=True,revision=1),
          dict(container_id='legacy-two',net_mass='7.201',tare_mass='0.300',capacity='6',location_id=place,tared=True,revision=1)]
    with pytest.raises(DomainError,match='120%'):receive(x,rows)
    with x.db.read() as s:
        assert s.get(Container,'legacy-one').tare_mg is None
        assert s.get(Container,'legacy-two').tare_mg is None and s.get(Container,'legacy-two').capacity_mg is None
        assert s.scalar(select(func.count()).select_from(Receipt))==0
    rows[1]['net_mass']='7.200'
    rid=receive(x,rows,key='complete-metadata-once')
    assert receive(x,rows,key='complete-metadata-once')==rid
    with x.db.read() as s:
        assert s.get(Container,'legacy-one').tare_mg==500000
        assert s.get(Container,'legacy-two').capacity_mg==6000000
        assert s.scalar(select(func.count()).select_from(Ledger))==2
        assert s.scalar(select(func.sum(Ledger.delta_mg)))==9200000
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=='container_updated'))==2
