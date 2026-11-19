from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import DBAPIError

from beanosort.capture import fixture_particle
from beanosort.models import Audit, Container, Operation, Particle, Run, RunSummary
from beanosort.run_summaries import get_summary, rebuild
from beanosort.segments import change_containers, collections
from beanosort.sort_capacity import snapshot
from beanosort.weighing import predicted_mass
from test_references import reference
from test_web import client_for


def prepared(x, tmp_path, *, reference_recipe=False):
    pid, *_ = x.receive('2')
    op, data = x.prepare(pid,'2')
    if reference_recipe:
        _, recipe, _ = reference(x,tmp_path)
        x.workflow.delete_prepared_sorting(x.uid,'replace-summary-recipe',op,confirm_unstarted=True)
        data.update(recipe_id=recipe);data.pop('capture_model_id')
        op=x.workflow.prepare(x.uid,'summary-reference-job',data)
    return op,x.start(op,503),data


def capture(x, op):
    with x.db.read() as s:
        operation=s.get(Operation,op)
        return x.workflow.profile(s,operation),predicted_mass(s,operation,x.db.environment)


def equal_predictions(first, second):
    assert first['available']==second['available']
    for key in first['populations']:
        a,b=first['populations'][key],second['populations'][key]
        assert a['predicted_net_mg']==b['predicted_net_mg']
        for name in ('unrounded_net_mg','expected_count','mean_bean_mass_mg'):
            if a.get(name) is None:assert b.get(name) is None
            else:assert abs(Decimal(a[name])-Decimal(b[name]))<Decimal('1e-15')


@pytest.mark.parametrize('reference_recipe',[False,True])
def test_incremental_summary_equals_original_evidence_and_rebuild(system,tmp_path,reference_recipe):
    x=system;op,run,_=prepared(x,tmp_path,reference_recipe=reference_recipe)
    x.workflow.simulate_next(x.uid,run)
    before=capture(x,op)
    assert x.workflow.simulate_next(x.uid,run,replay_first=True)==0
    assert capture(x,op)==before
    while x.workflow.simulate_next(x.uid,run):pass
    cached,model=capture(x,op)
    with x.db.write() as s:
        original=[(p.id,p.digest) for p in s.scalars(select(Particle).filter_by(run_id=run))]
        s.execute(delete(RunSummary).where(RunSummary.run_id==run))
    uncached,original_model=capture(x,op)
    assert uncached==cached
    equal_predictions(model,original_model)
    with x.db.read() as s:assert get_summary(s,run) is None  # Reads never repair/write caches.
    with x.db.write() as s:rebuild(s,s.get(Run,run),x.db.environment)
    rebuilt,rebuilt_model=capture(x,op)
    assert rebuilt==cached
    equal_predictions(model,rebuilt_model)
    with x.db.read() as s:assert [(p.id,p.digest) for p in s.scalars(select(Particle).filter_by(run_id=run))]==original


def test_cached_profile_and_capacity_do_not_query_particle_payloads(system,tmp_path):
    x=system;op,run,_=prepared(x,tmp_path,reference_recipe=True)
    x.workflow.simulate_next(x.uid,run)
    statements=[]
    def record(conn,cursor,statement,params,context,many):statements.append(statement.lower())
    event.listen(x.db.engine,'before_cursor_execute',record)
    try:
        with x.db.read() as s:
            operation=s.get(Operation,op)
            assert x.workflow.profile(s,operation)['counts']['tracked']==250
            assert predicted_mass(s,operation,x.db.environment)['available']
            assert len(snapshot(s,operation,x.db.environment))==2
    finally:event.remove(x.db.engine,'before_cursor_execute',record)
    assert not any('from particles' in statement for statement in statements)


def test_summary_rolls_back_with_rejected_batch_and_handles_duplicate_particles(system,tmp_path):
    x=system;op,run,_=prepared(x,tmp_path)
    x.workflow.simulate_next(x.uid,run)
    before=capture(x,op)
    rows=[fixture_particle(250),{**fixture_particle(0),'key':'conflicting-sequence'}]
    with pytest.raises(ValueError,match='sequence'):
        x.workflow.ingest(x.uid,run,'must-rollback',rows)
    assert capture(x,op)==before
    assert x.workflow.ingest(x.uid,run,'duplicate-existing-evidence',[fixture_particle(0)])==0
    assert capture(x,op)==before


def test_missing_summary_rebuilt_before_next_batch(system,tmp_path):
    x=system;op,run,_=prepared(x,tmp_path)
    x.workflow.simulate_next(x.uid,run)
    with x.db.write() as s:s.execute(delete(RunSummary).where(RunSummary.run_id==run))
    assert x.workflow.simulate_next(x.uid,run)==250
    with x.db.read() as s:assert get_summary(s,run).particle_count==500


def test_late_evidence_updates_the_original_collection_summary(factory,tmp_path):
    x=factory('real');op,run,data=prepared(x,tmp_path)
    def real(sequence):
        row=fixture_particle(sequence)
        for aspect,kind in [('features','image_derived'),('decision','model_derived'),('action','controller_record')]:row['evidence'][aspect]['kind']=kind
        return row
    x.workflow.ingest(x.uid,run,'first-real-window',[real(0),real(2)])
    x.workflow.transition(x.uid,op,'paused',reason='Change container')
    replacement=x.workflow.create_container(x.uid,label='Replacement',location='Floor',capacity='10',tare_mass='0.1')
    change_containers(x.workflow,x.uid,'new-summary-window',op,{**data,'selected_id':replacement,
        'feeding_stopped':True,'in_flight_clear':True,'ready':True,'reason':'New selected collection','first_sequence':'3'})
    x.workflow.ingest(x.uid,run,'late-first-window',[real(1)])
    with x.db.read() as s:
        operation=s.get(Operation,op)
        items=collections(s,operation)
        first=next(item for item in items if item.code=='selected')
        second=next(item for item in items if item.code=='selected_2')
        assert get_summary(s,run,first).particle_count==3
        assert get_summary(s,run,second).particle_count==0
        assert x.workflow.profile(s,operation,collection=first)['counts']['tracked']==3
        assert x.workflow.profile(s,operation,collection=second)['counts']['tracked']==0


def test_sqlite_reader_does_not_block_writer_and_keeps_consistent_snapshot(system):
    x=system
    if x.db.engine.dialect.name!='sqlite':pytest.skip('SQLite WAL and transaction isolation check')
    ready=Event();written=Event()
    def reader():
        with x.db.read() as s:
            assert s.scalar(select(func.count()).select_from(Container))==0
            ready.set()
            assert written.wait(5),'A read-only session blocked the writer'
            assert s.scalar(select(func.count()).select_from(Container))==0
    with ThreadPoolExecutor(2) as pool:
        future=pool.submit(reader)
        assert ready.wait(5)
        try:x.workflow.create_container(x.uid,label='Concurrent vessel',location='Floor',capacity='1',tare_mass='0.1')
        finally:written.set()
        future.result(timeout=5)
    with x.db.read() as s:assert s.scalar(select(func.count()).select_from(Container))==1


def test_sqlite_read_context_is_read_only_and_connections_still_allow_later_writes(system):
    x=system
    if x.db.engine.dialect.name!='sqlite':pytest.skip('SQLite query-only session check')
    with pytest.raises(DBAPIError):
        with x.db.read() as s:
            s.add(Audit(id='audit-must-not-exist',operator_id=x.uid,action='read_must_not_write',data={}))
            s.flush()
    x.workflow.create_container(x.uid,label='Writable later',location='Floor',capacity='1',tare_mass='0.1')
    with x.db.read() as s:assert s.get(Audit,'audit-must-not-exist') is None


def test_distributions_load_separately_and_reads_preserve_inventory(system,tmp_path):
    x=system;op,run,_=prepared(x,tmp_path,reference_recipe=True)
    x.workflow.simulate_next(x.uid,run)
    client=client_for(x)
    page=client.get('/jobs/'+op)
    assert 'data-lazy-details="/jobs/'+op+'/distributions"' in page.text
    assert '<th>Category/bin</th>' not in page.text
    fragment=client.get('/jobs/'+op+'/distributions',headers={'X-BeanoSort-Fragment':'1'})
    assert fragment.status_code==200 and '<th>Category/bin</th>' in fragment.text
    assert '<!doctype' not in fragment.text
    assert client.get('/jobs/'+op+'/distributions').status_code==200
