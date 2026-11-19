from pathlib import Path
import os
import sqlite3

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from beanosort.cli import seed_material_demo
from beanosort.db import Database
from beanosort.maintenance import CONFIG_TABLES, MATERIAL_TABLES, stage_training_inventory_reset
from beanosort.models import Audit, Container, Ledger, Operation, ReferenceDataset, User, Version
from beanosort.quantities import DomainError
from beanosort.references import DEFAULT_RECIPE, create_recipe, import_bundle, retire_recipe, retired_recipe_ids
from beanosort.service import Workflow
from reference_fixture import bundle
from test_web import client_for


pytestmark = pytest.mark.skipif(bool(os.environ.get("BEANOSORT_TEST_POSTGRES_URL")),
                                reason="Offline reset utility handles SQLite training copies only")


def test_staged_training_reset_keeps_configuration_and_clears_entire_material_chain(system,tmp_path):
    x=system
    seed_material_demo(x.db,x.uid)
    from test_segmented_jobs import ready,swap
    from test_occupied_destinations import empty
    from beanosort.segments import collections,record_weight
    _,_,segmented,data,_=ready(x)
    x.workflow.transition(x.uid,segmented,'paused',reason='Reset fixture includes a removed bin')
    swap(x,segmented,data,selected_id=empty(x))
    with x.db.read() as s:closed=next(item for item in collections(s,s.get(Operation,segmented)) if item.code=='selected')
    record_weight(x.workflow,x.uid,'reset-fixture-weight',closed.id,{'gross':'3.5','revision':'1','weighed':True})
    did=import_bundle(x.db,x.uid,bundle(tmp_path/'bundle'),tmp_path/'reference',name='Kept reference')
    rid=create_recipe(x.db,x.uid,'retained-recipe',DEFAULT_RECIPE,did)
    retire_recipe(x.db,x.uid,'retained-retirement',x.versions['recipe'],'Retired fixture')
    # Existing historical vessels may have unknown weights. Reset must preserve
    # these records instead of inventing values to reinsert them as new vessels.
    with sqlite3.connect(x.db.engine.url.database) as c:
        guard=c.execute("SELECT sql FROM sqlite_master WHERE name='container_weights_required'").fetchone()[0]
        c.execute('DROP TRIGGER container_weights_required')
        c.execute("INSERT INTO containers (id,created_at,label,kind,location,location_id,archived,deleted,revision) SELECT 'historical','2000','Historical vessel','bin',name,id,0,0,1 FROM locations LIMIT 1")
        c.execute(guard)
    source=Path(x.db.engine.url.database);staged=tmp_path/'empty.db';backup=tmp_path/'archive.db'
    result=stage_training_inventory_reset(source,staged,backup)
    assert result['removed_record_counts']['portions']>0 and result['removed_record_counts']['packs']>0
    assert all(result['removed_record_counts'][table]>0 for table in ('sorting_collections','sorting_segments','collection_weighings'))
    with sqlite3.connect(source) as current,sqlite3.connect(backup) as archive,sqlite3.connect(staged) as clean:
        for table in MATERIAL_TABLES:
            assert clean.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]==0
            assert current.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()==archive.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        for table in CONFIG_TABLES:
            assert current.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()==clean.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        assert not clean.execute('PRAGMA foreign_key_check').fetchall()
        assert clean.execute('PRAGMA integrity_check').fetchone()==('ok',)
    db=Database('sqlite:///'+str(staged),'simulation');workflow=Workflow(db)
    with db.read() as s:
        assert x.versions['recipe'] in retired_recipe_ids(s)
        assert s.get(Version,rid) is not None and s.get(ReferenceDataset,did) is not None
        assert s.scalar(select(Audit).filter_by(action='training_inventory_reset')) is not None
        target=s.scalar(select(Container).where(Container.tare_mg>0,Container.deleted==False,Container.archived==False))
        cid=target.id
    fresh=workflow.receive(x.uid,'fresh-after-reset',dict(container_id=cid,station_id=x.stations['Receiving desk'],
        ready=True,net_mass='1.000',evidence=x.evidence))
    assert fresh
    with pytest.raises(DBAPIError):
        with db.engine.begin() as c:c.execute(text('DELETE FROM ledger'))
    with db.read() as s:assert s.scalar(select(Ledger.delta_mg))==1000000
    client=client_for(type('System',(),{'db':db})())
    assert client.get('/').status_code==200
    client.close();db.engine.dispose()


def test_training_reset_refuses_real_database_and_existing_destinations(factory,tmp_path):
    real=factory('real')
    source=Path(real.db.engine.url.database)
    with pytest.raises(DomainError,match='isolated simulation'):
        stage_training_inventory_reset(source,tmp_path/'clean.db',tmp_path/'backup.db')
    assert not (tmp_path/'backup.db').exists()
    x=factory();source=Path(x.db.engine.url.database)
    with pytest.raises(DomainError,match='distinct new'):
        stage_training_inventory_reset(source,source,tmp_path/'backup.db')
    with x.db.write() as s:s.get(User,x.uid).role='operator'
    with pytest.raises(DomainError,match='administrator'):
        stage_training_inventory_reset(source,tmp_path/'clean.db',tmp_path/'backup.db')
