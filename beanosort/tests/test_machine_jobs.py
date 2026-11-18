from copy import deepcopy
from uuid import uuid4
import pytest
from sqlalchemy import select,func
from beanosort.config import Settings
from beanosort.machine_jobs import Integration,install_recipe,compact
from beanosort.models import MachineJob,MachineEvent,MachineCommand,Operation,Particle,Run,MonitorEvent
from beanosort.quantities import DomainError
from test_web import client_for,token


def prepared(x, destination=None, *, source='live', image_mode='none'):
    recipe=install_recipe(x.db);pid,*_=x.receive('1')
    dest=destination or x.workflow.create_container(x.uid,label='Machine receiver '+uuid4().hex,location='Test',capacity='20',tare_mass='0.5')
    request={'machine_id':'test-machine','source':source,'recipe':'all_pass_through','image_mode':image_mode,'duration_seconds':60,
             'settings':{'name':'Test','conveyor':{'speed_steps_per_second':20},'vibration_motor':{'duty_percent':38},'cameras':{'frame_rate_hz':59.5}},'settings_sha256':'a'*64}
    op=x.workflow.prepare(x.uid,str(uuid4()),dict(portion_id=pid,planned_mass='1',
        station_id=x.stations['Training sorter' if x.db.environment=='simulation' else 'Manual run record'],selected_id=None,
        pass_through_id=dest,ready=True,recipe_id=recipe,capture_model_id=x.versions['capture_model'],policy_id=x.versions['reconciliation_policy'],
        machine={'remote_id':str(uuid4()),'endpoint':'http://test:8091','request':request}))
    return op


class FakeMachine:
    def __init__(self):self.calls=[];self.state='running';self.events=[];self.lost=False;self.gap=False
    def call(self,endpoint,path,body=None):
        self.calls.append((path,body))
        if self.lost:raise DomainError('LAN disconnected')
        if body is not None:
            if 'action' not in body:self.state='paused' if body.get('start_paused') else 'running'
            if 'action' in body:self.state={'pause':'paused','resume':'running','end':'draining','stop':'failed'}[body['action']]
            return {'accepted':True}
        after=int(path.split('after=')[1].split('&')[0]);events=[e for e in self.events if e['sequence']>after][:100]
        if self.gap and events:events=events[1:]
        return {'events':events,'status':{'state':self.state,'cursor':len(self.events),
            'final_sequence':len(self.events) if self.state in {'completed','failed'} else None,'metrics':{},'pause_drained':True}}


def integration(x):
    i=Integration(x.db,x.workflow,Settings(str(x.db.engine.url),x.db.environment,'a'*40))
    i.transport=FakeMachine();return i


def bean_event(sequence=1):
    return {'sequence':sequence,'kind':'bean','created_at':'2026-11-17T10:00:00+00:00',
        'data':{'bean_sequence':123,'ordinal':0,'sample_count':2,'features':{'caml_lab_l_mean':21,'camr_lab_l_mean':23,
            'rotational_ellipsoid_volume_proxy_mm3':125},'measurement_status':'measured'}}


@pytest.mark.parametrize('environment',['simulation','real'])
def test_machine_start_pause_resume_end_and_reconciliation(factory,environment):
    x=factory(environment);op=prepared(x);i=integration(x)
    i.enqueue(x.uid,op,'start-key','start');i.tick()
    with x.db.read() as s:
        run=s.scalar(select(Run).filter_by(operation_id=op))
        assert run.seed is None and run.expected_particles is None
        assert s.get(MachineJob,op).state=='running'
    i.enqueue(x.uid,op,'pause-key','pause');i.tick()
    with x.db.read() as s:assert s.get(Operation,op).status=='paused'
    i.enqueue(x.uid,op,'resume-key','resume');i.tick()
    i.transport.events=[bean_event()];i.tick();i.tick()
    with x.db.read() as s:
        assert s.scalar(select(func.count(Particle.id)))==1
        assert s.scalar(select(Particle)).simulated_destination is None
        assert s.scalar(select(func.count(MonitorEvent.id)))==0
        assert x.workflow.profile(s,s.get(Operation,op))['counts']['tracked']==1
    i.enqueue(x.uid,op,'end-key','end');i.tick()
    client=client_for(x)
    assert client.get('/jobs/'+op+'/weigh').status_code==409
    i.transport.state='completed';i.transport.events.append({'sequence':2,'kind':'completed','data':{},'created_at':'2026-11-17T10:00:01+00:00'});i.tick()
    status=client.get('/jobs/'+op+'/machine-status').json()
    assert status['weigh_url']=='/jobs/'+op+'/weigh'
    page=client.get(status['weigh_url']);assert page.status_code==200
    assert 'Pass-through' in page.text and 'Source container' in page.text


def test_lan_loss_outbox_replay_and_journal_gap(system):
    x=system;op=prepared(x);i=integration(x);i.transport.lost=True
    i.enqueue(x.uid,op,'s','start');i.tick()
    with x.db.read() as s:
        assert not s.get(MachineCommand,'s').acknowledged
        assert s.get(MachineJob,op).cursor==0
    i.transport.lost=False;i.tick();i.enqueue(x.uid,op,'p','pause');i.enqueue(x.uid,op,'p','pause');i.tick()
    i.transport.events=[bean_event(),{'sequence':2,'kind':'measurement','data':{},'created_at':'now'}];i.transport.gap=True;i.tick()
    with x.db.read() as s:
        assert s.get(MachineJob,op).cursor==0
        assert 'gap' in s.get(MachineJob,op).status['connection_error']
        assert s.scalar(select(func.count(Particle.id)))==0
    i.transport.gap=False;i.tick();i.tick()
    with x.db.read() as s:
        assert s.get(MachineJob,op).cursor==2
        assert s.scalar(select(func.count(MachineEvent.sequence)))==2
        assert s.scalar(select(func.count(Particle.id)))==1
    assert len([b for _,b in i.transport.calls if b and b.get('action')=='pause'])==1


def test_machine_html_and_legacy_bypass_rejected(system):
    x=system;op=prepared(x);client=client_for(x);page=client.get('/jobs/'+op)
    assert page.status_code==200
    assert 'START' in page.text and 'No image saves' in page.text
    assert 'Start simulation workflow' not in page.text
    for action in ['start','transition','simulate','simulate-batch','replay','import']:
        result=client.post('/jobs/'+op+'/'+action,data={'csrf':token(page),'key':str(uuid4())})
        assert result.status_code==409
    assert client.post('/jobs/'+op+'/machine/start',data={'key':'bad-csrf'}).status_code==403


def test_acknowledgement_watermark_blocks_early_weighing(system):
    x=system;op=prepared(x);i=integration(x);i.enqueue(x.uid,op,'s','start');i.tick()
    i.transport.events=[{'sequence':n,'kind':'measurement','data':{'x':n},'created_at':'now'} for n in range(1,202)]
    i.transport.state='completed';i.tick()
    with x.db.read() as s:
        assert s.get(MachineJob,op).state=='draining'
        assert s.get(Operation,op).status=='running'
    i.tick();i.tick()
    with x.db.read() as s:
        assert s.get(MachineJob,op).cursor==201
        assert s.get(Operation,op).status=='finishing'


def test_crash_after_particle_ingest_replays_safely(system):
    x=system;op=prepared(x);i=integration(x);i.enqueue(x.uid,op,'s','start');i.tick()
    event=bean_event()
    with x.db.read() as s:run=s.scalar(select(Run).filter_by(operation_id=op));job=s.get(MachineJob,op)
    x.workflow.ingest(x.uid,run.id,'machine:1:1',[compact(event['data'],job.remote_id,'live')])
    i.transport.events=[event];i.tick()
    with x.db.read() as s:assert s.scalar(select(func.count(Particle.id)))==1
    with pytest.raises(DomainError,match='synthetic generation'):x.workflow.simulate_next(x.uid,run.id)


def test_full_receiver_starts_capture_paused_until_operator_override(system):
    from beanosort.models import Container,Audit
    x=system;_,cid,*_=x.receive('20')
    with x.db.write() as s:s.get(Container,cid).capacity_mg=20000000
    op=prepared(x,cid);i=integration(x);i.enqueue(x.uid,op,'start-full','start');i.tick()
    with x.db.read() as s:
        assert s.get(MachineJob,op).request['start_paused'] is True
        assert s.get(MachineJob,op).state=='paused'
        assert s.get(Operation,op).status=='paused'
    assert not [b for _,b in i.transport.calls if b and b.get('action')=='resume']
    i.enqueue(x.uid,op,'resume-full','resume');i.tick()
    with x.db.read() as s:
        assert s.get(MachineJob,op).state=='running'
        assert s.scalar(select(Audit).filter_by(operation_id=op,action='capacity_override'))
