from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from beanosort.models import Audit, Destination, Input, Ledger, Operation, Portion, Reservation, Run, SimulationAssumption, Version
from beanosort.quantities import DomainError
from beanosort.service import available, balance, empty_container
from beanosort.simulation import SimulationConfiguration, current_settings
from test_web import client_for, token


def edit(x, op, data, **extra):
    return x.workflow.edit_prepared_sorting(x.uid, "edit-job-"+uuid4().hex, op, data, confirm_unstarted=extra.get("confirmed",True))


def test_delete_prepared_releases_claims_without_consuming_material_and_replays(system):
    x=system
    pid, *_=x.receive()
    op,data=x.prepare(pid)
    with pytest.raises(DomainError,match="Confirm"):
        x.workflow.delete_prepared_sorting(x.uid,"unconfirmed-job-delete",op)
    for _ in range(2):
        assert x.workflow.delete_prepared_sorting(x.uid,"delete-job-once",op,confirm_unstarted=True)==op
    with x.db.read() as s:
        assert s.get(Operation,op).status=="cancelled"
        assert s.get(Portion,pid).status=="stock"
        assert available(s,s.get(Portion,pid))==20000000 and balance(s,pid)==20000000
        assert not s.scalar(select(Reservation.id).filter_by(operation_id=op,active=True))
        assert not s.scalar(select(Destination.id).filter_by(operation_id=op,active=True))
        assert s.scalar(select(func.count()).select_from(Ledger))==1
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=="job_deleted"))==1
        assert s.get(Input,(op,pid)).mass_mg==20000000
    client=client_for(x)
    assert op not in client.get('/').text
    assert "Prepared job deleted" in client.get('/jobs/'+op).text
    with pytest.raises(DomainError):
        x.start(op)
    new=x.workflow.prepare(x.uid,"prepare-after-job-delete",data)
    assert new!=op
    client.close()


def test_edit_wrong_destination_atomically_replaces_claims_preserving_old_configuration(system):
    x=system
    pid,*_=x.receive()
    op,data=x.prepare(pid)
    new_destination=x.workflow.create_container(x.uid,label="Correct Selected bin",location="Floor",capacity="25",tare_mass="0.5")
    edited={**data,"selected_id":new_destination}
    for _ in range(2):
        new=x.workflow.edit_prepared_sorting(x.uid,"correct-job-once",op,edited,confirm_unstarted=True)
    assert new!=op
    with x.db.read() as s:
        assert s.get(Operation,op).status=="cancelled"
        assert s.get(Operation,new).status=="prepared"
        assert s.scalar(select(Destination.container_id).filter_by(operation_id=op,stream="selected"))==data["selected_id"]
        assert s.scalar(select(Destination.container_id).filter_by(operation_id=new,stream="selected",active=True))==new_destination
        assert s.scalar(select(Reservation.operation_id).filter_by(portion_id=pid,active=True))==new
        assert available(s,s.get(Portion,pid))==0 and balance(s,pid)==20000000
        assert empty_container(s,data["selected_id"])
        assert s.scalar(select(func.count()).select_from(Ledger))==1
        assert s.scalar(select(func.count()).select_from(Run))==0
        event=s.scalar(select(Audit).filter_by(operation_id=op,action="job_replaced"))
        assert event.data["replacement_id"]==new and event.data["before"]["selected_id"]==data["selected_id"]


def test_edit_wrong_source_releases_old_material_and_allows_swapped_outputs(system):
    x=system
    first,*_=x.receive()
    second,*_=x.receive("3")
    op,data=x.prepare(first)
    new=edit(x,op,{**data,"portion_id":second,"planned_mass":"3","selected_id":data["pass_through_id"],"pass_through_id":data["selected_id"]})
    with x.db.read() as s:
        assert available(s,s.get(Portion,first))==20000000
        assert available(s,s.get(Portion,second))==0
        assert s.get(Input,(op,first)).mass_mg==20000000
        assert s.get(Input,(new,second)).mass_mg==3000000
        assert s.scalar(select(func.sum(Ledger.delta_mg)))==23000000


def test_invalid_edit_rolls_back_claim_release_and_does_not_replace_job(system):
    x=system
    pid,*_=x.receive()
    op,data=x.prepare(pid)
    held,occupied,*_=x.receive("1")
    x.workflow.set_hold(x.uid,held,"Held contents cannot be a sorting destination")
    invalid=[{**data,"selected_id":occupied},{**data,"selected_id":data["pass_through_id"]},
             {**data,"planned_mass":"21"},{**data,"recipe_id":data["capture_model_id"]},{**data,"ready":False}]
    for changed in invalid:
        with pytest.raises(DomainError):
            edit(x,op,changed)
    with pytest.raises(DomainError,match="Confirm"):
        edit(x,op,data,confirmed=False)
    with x.db.read() as s:
        assert s.get(Operation,op).status=="prepared"
        assert s.scalar(select(Reservation.operation_id).filter_by(portion_id=pid,active=True))==op
        assert len(list(s.scalars(select(Destination).filter_by(operation_id=op,active=True))))==2
        assert not s.scalar(select(Audit.id).filter_by(action="job_replaced"))


def test_edit_and_delete_unavailable_after_start_and_pause(system):
    x=system
    pid,*_=x.receive()
    op,data=x.prepare(pid)
    x.start(op,250)
    for state in ("running","paused","failed"):
        if state!="running":
            x.workflow.transition(x.uid,op,state,reason="Lifecycle guard test")
        with pytest.raises(DomainError,match="never started"):
            edit(x,op,data)
        with pytest.raises(DomainError,match="never started"):
            x.workflow.delete_prepared_sorting(x.uid,"cannot-delete-"+state,op,confirm_unstarted=True)
    with x.db.read() as s:
        assert s.get(Portion,pid).status=="wip" and available(s,s.get(Portion,pid))==0
        assert s.scalar(select(Reservation.operation_id).filter_by(portion_id=pid,active=True))==op


def test_start_racing_edit_allows_only_one_transition(system):
    x=system
    pid,*_=x.receive()
    op,data=x.prepare(pid)
    def act(which):
        try:
            return (which, x.start(op,250) if which=="start" else edit(x,op,data))
        except DomainError:
            return ("conflict",None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(act,("start","edit")))
    assert sum(kind=="conflict" for kind,_ in results)==1
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Reservation).where(Reservation.active==True))==1
        assert s.scalar(select(func.sum(Ledger.delta_mg)))==20000000


def test_edit_form_preselects_own_reserved_source_and_destinations_and_supports_delete(system):
    x=system
    pid,*_=x.receive()
    op,data=x.prepare(pid)
    client=client_for(x)
    page=client.get('/jobs/'+op)
    assert "Edit prepared job" in page.text and "Delete prepared job" in page.text
    page=client.get('/jobs/'+op+'/edit')
    for value in (pid,data["selected_id"],data["pass_through_id"],data["recipe_id"],data["capture_model_id"]):
        assert 'value="'+value+'" selected' in page.text
    payload={**data,"csrf":token(page),"key":token(page,"key"),"ready":"on","confirm_unstarted":"on"}
    assert client.post('/jobs/'+op+'/edit',data={**payload,"csrf":"invalid"}).status_code==403
    page=client.post('/jobs/'+op+'/edit',data=payload)
    assert page.status_code==200,page.text
    new=page.url.path.rsplit('/',1)[-1]
    assert new!=op and "View the previous job configuration" in page.text
    assert "Open corrected job" in client.get('/jobs/'+op).text
    assert op not in client.get('/').text
    assert client.post('/jobs/'+new+'/delete',data={"csrf":"bad","key":"no-delete","confirm_unstarted":"on"}).status_code==403
    page=client.post('/jobs/'+new+'/delete',data={"csrf":token(page),"key":"web-delete-prepared","confirm_unstarted":"on"})
    assert page.status_code==200 and new not in page.text
    client.close()


def test_edit_uses_original_simulation_version_and_recomputes_planned_count(system):
    x=system
    pid,*_=x.receive()
    initial,data=x.prepare(pid)
    x.workflow.transition(x.uid,initial,"cancelled",reason="Use versioned simulation settings")
    data={k:v for k,v in data.items() if k!="capture_model_id"}
    op=x.workflow.prepare(x.uid,"versioned-prepared-job",data)
    with x.db.read() as s:
        settings=current_settings(s)
    SimulationConfiguration(x.db).save(x.uid,"change-default-after-prepare",dict(previous_id=settings.id,
        mean_mass_g="0.2",capture_percent="100",material_label="Green coffee",reason="Test changed defaults"))
    client=client_for(x)
    page=client.get('/jobs/'+op+'/edit')
    assert 'value="'+settings.id+'"' in page.text
    new=edit(x,op,{**data,"simulation_settings_id":settings.id,"planned_mass":"1"})
    with x.db.read() as s:
        assumption=s.scalar(select(SimulationAssumption).filter_by(operation_id=new))
        assert assumption.settings_id==settings.id and assumption.data["rounded_particles"]==6667
        assert s.get(Version,s.get(Operation,new).capture_model_id).data["probability"]=="0.99"
        assert s.scalar(select(SimulationAssumption).filter_by(operation_id=op)).data["rounded_particles"]==133333
    client.close()
