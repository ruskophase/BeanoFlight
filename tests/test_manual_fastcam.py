"""Manual diagnostics use the active job's camera-only capability; all hardware is fake."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from uuid import uuid4
import pytest
from beanoflight.machine_jobs.manual_fastcam import ManualCamera,validate_request
from beanoflight.machine_jobs.server import Manager,make_handler
from beanoflight.machine_jobs.store import Store
from beanoflight.machine_jobs.fastcam import CameraHTTP
from test_machine_fastcam import Camera
from test_machine_jobs import config


class FakeCamera(Camera):
    def __init__(self,external=True):
        super().__init__();self.status['settings']['external_trigger']=external
    def request(self,path,body=None):
        settings=deepcopy(self.status['settings'])
        result=super().request(path,body)
        if path=='fastcam/settings':
            self.status['settings']={**settings,**body}
            return deepcopy(self.status)
        return result
    def frame(self):return b'\xff\xd8synthetic-jpeg'


def wait(camera):
    deadline=time.monotonic()+3
    while camera.snapshot()['busy'] and time.monotonic()<deadline:time.sleep(.005)
    assert not camera.snapshot()['busy']
    return camera.snapshot()['last_command']


def action(camera,name,**extra):
    camera.submit(str(uuid4()),{'action':name,'operator':'Test operator',**extra})
    return wait(camera)


@pytest.mark.parametrize('external',[False,True])
def test_manual_camera_works_without_lease_acquisition_and_closes_with_job(external):
    client=FakeCamera(external);events=[];active=[True]
    m=ManualCamera(client,lambda:active[0],None,lambda *v:events.append(v))
    try:
        assert m.read_status()['controls_available'] and not m.mode
        assert not any(body is not None for _,body,_ in client.calls)  # opening tab is read-only
        assert action(m,'take_manual')['state']=='acknowledged'
        assert action(m,'start')['state']=='acknowledged'
        assert action(m,'record')['state']=='acknowledged'
        assert action(m,'stop_recording')['state']=='acknowledged'
        assert action(m,'record')['state']=='acknowledged'
    finally:active[0]=False;m.close()
    commands=[body['command'] for path,body,_ in client.calls if path=='fastcam/command']
    assert commands==['start','arm' if external else 'record','disarm' if external else 'stop',
        'arm' if external else 'record','stop_camera']
    assert [(path,body) for path,body,_ in client.calls if path=='high-speed/enabled']==(
        [('high-speed/enabled',{'enabled':True}),('high-speed/enabled',{'enabled':False})] if external else [])
    assert all(path in {'fastcam/status','fastcam/command','state','high-speed/enabled'} for path,_,_ in client.calls)
    assert not m.read_status()['controls_available']
    assert not client.status['camera_running']
    assert any(e[0]=='manual_fastcam_requested' and e[1]['operator']=='Test operator' for e in events)
    assert events[-1][0]=='manual_fastcam_closed'


def test_handover_ends_automatic_window_before_manual_start_and_never_restarts_it():
    client=FakeCamera(False);timeline=[]
    class Schedule:
        def finish(self):timeline.append('automatic-stopped')
        def snapshot(self):return {'state':'interrupted','plan':{'start_seconds':10}}
    m=ManualCamera(client,lambda:True,Schedule(),lambda *v:timeline.append(v[0]))
    try:
        assert action(m,'take_manual')['state']=='acknowledged'
        assert timeline.index('automatic-stopped')<timeline.index('manual_fastcam_result')
        action(m,'start');action(m,'take_manual')
        assert timeline.count('automatic-stopped')==1
        assert not any(body and body.get('command')=='record' for _,body,_ in client.calls)
    finally:m.close()


def test_settings_validation_and_capture_lock_preserve_preview_controls():
    client=FakeCamera(False);m=ManualCamera(client,lambda:True,None,lambda *v:None)
    try:
        action(m,'take_manual')
        assert action(m,'settings',settings={'exposure_ms':6})['state']=='acknowledged'
        action(m,'start')
        assert action(m,'settings',settings={'fps':150})['state']=='failed'
        assert 'Stop the USB camera' in m.snapshot()['error']
        assert action(m,'settings',settings={'disable_preview':True})['state']=='acknowledged'
        assert client.status['settings']['disable_preview'] is True
        assert not any(path=='high-speed/config' for path,_,_ in client.calls)
    finally:m.close()
    for value in ({'settings':{'gpio':33}},{'settings':{'fps':float('nan')}},{'settings':{'external_trigger':1}},
                  {'settings':{'device':'/tmp/foo'}},{'settings':{'preview_fps':1.5}}):
        with pytest.raises(ValueError):validate_request({'action':'settings','operator':'Test',**value})
    with pytest.raises(ValueError):validate_request({'action':'conveyor/enabled','operator':'Test'})
    with pytest.raises(ValueError):validate_request({'action':'start','operator':'Test','client_id':'other-owner'})


def test_end_during_uncertain_start_prevents_trigger_enable_and_still_cleans_camera():
    entered=threading.Event();release=threading.Event()
    class Blocked(FakeCamera):
        def request(self,path,body=None):
            if body and body.get('command')=='start':entered.set();assert release.wait(3)
            return super().request(path,body)
    client=Blocked();m=ManualCamera(client,lambda:True,None,lambda *v:None)
    action(m,'take_manual');m.submit('start-once',{'action':'start','operator':'Test'})
    assert entered.wait(1);m.request_stop();release.set();m.close()
    assert not any(path=='high-speed/enabled' and body['enabled'] for path,body,_ in client.calls)
    assert not client.status['camera_running']


def test_unknown_start_ack_is_not_retried_and_shutdown_is_attempted():
    class Lost(FakeCamera):
        def request(self,path,body=None):
            result=super().request(path,body)
            if body and body.get('command')=='start':raise TimeoutError('Lost start acknowledgement')
            return result
    c=Lost(False);m=ManualCamera(c,lambda:True,None,lambda *v:None)
    action(m,'take_manual');assert action(m,'start')['state']=='failed';m.close()
    assert [b['command'] for p,b,_ in c.calls if p=='fastcam/command']==['start','stop_camera']
    assert not c.status['camera_running']


def test_terminal_replay_and_unstarted_jobs_cannot_issue_manual_commands(tmp_path):
    manager=Manager(config(tmp_path));key=str(uuid4());manager.store.create(key,{'source':'recorded'})
    try:
        with pytest.raises(ValueError,match='live source'):manager.camera_status(key)
        manager.store.status(key,{'state':'completed'})
        live=str(uuid4());manager.store.create(live,{'source':'live'})
        assert not manager.camera_status(live)['controls_available']
        with pytest.raises(ValueError):manager.camera_command(live,'id',{'action':'take_manual','operator':'Test'})
    finally:manager.lockfile.close()


def test_manager_command_deduplication_and_payload_scope(tmp_path):
    manager=Manager(config(tmp_path));key=str(uuid4());manager.store.create(key,{'source':'live'})
    client=FakeCamera(False);m=ManualCamera(client,lambda:True,None,lambda *v:None)
    manager.runners[key]=SimpleNamespace(manual_fastcam=m)
    try:
        data={'action':'take_manual','operator':'Operator 1'}
        manager.camera_command(key,'claim',data);wait(m)
        for _ in range(2):manager.camera_command(key,'start',{'action':'start','operator':'Operator 1'})
        wait(m)
        assert sum(bool(b and b.get('command')=='start') for _,b,_ in client.calls)==1
        with pytest.raises(ValueError,match='different action'):
            manager.camera_command(key,'start',{'action':'stop_camera','operator':'Operator 1'})
        assert manager.camera_frame(key).startswith(b'\xff\xd8')
        assert not any('acquire' in p or 'conveyor' in p or 'valve' in p for p,_,_ in client.calls)
    finally:m.close();manager.lockfile.close()


def test_delayed_status_does_not_overwrite_acknowledged_camera_settings():
    entered=threading.Event();release=threading.Event()
    class Delayed(FakeCamera):
        def request(self,path,body=None):
            result=super().request(path,body)
            if threading.current_thread().name=='delayed-poll' and path=='fastcam/status':
                entered.set();assert release.wait(3)
            return result
    c=Delayed(False);m=ManualCamera(c,lambda:True,None,lambda *v:None)
    try:
        action(m,'take_manual')
        poll=threading.Thread(target=m.read_status,name='delayed-poll');poll.start();assert entered.wait(1)
        action(m,'settings',settings={'exposure_ms':6})
        release.set();poll.join(1)
        assert m.snapshot()['camera']['settings']['exposure_ms']==6
    finally:release.set();m.close()


def test_http_camera_actions_use_the_existing_job_lease_without_ESP_takeover(tmp_path):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from urllib.request import Request,urlopen
    from urllib.error import HTTPError
    key=str(uuid4());lease_id='beanosort-job:'+key;device=FakeCamera(True);posts=[]
    class ESP(BaseHTTPRequestHandler):
        def log_message(self,*v):pass
        def do_GET(self):self.handle_request(False)
        def do_POST(self):self.handle_request(True)
        def handle_request(self,post):
            path=self.path.removeprefix('/api/');body=None
            if post:
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert body.pop('client_id')==lease_id  # Simulates the real controller lease gate.
                posts.append(path)
                assert path in {'fastcam/command','fastcam/settings','high-speed/enabled'}
            data=device.request(path,body)
            payload=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Length',str(len(payload)))
            self.end_headers();self.wfile.write(payload)
    esp=ThreadingHTTPServer(('127.0.0.1',0),ESP);esp_thread=threading.Thread(target=esp.serve_forever);esp_thread.start()
    manager=Manager(config(tmp_path));manager.store.create(key,{'source':'live'})
    camera=ManualCamera(CameraHTTP('http://127.0.0.1:'+str(esp.server_port),lease_id),lambda:True,None,
        lambda kind,data:manager.store.append(key,kind,data))
    manager.runners[key]=SimpleNamespace(manual_fastcam=camera)
    api=ThreadingHTTPServer(('127.0.0.1',0),make_handler(manager,'test-token'*8))
    api_thread=threading.Thread(target=api.serve_forever);api_thread.start()
    base='http://127.0.0.1:'+str(api.server_port)+'/v1/jobs/'+key+'/fastcam/'
    def call(path,body=None):
        with urlopen(Request(base+path,None if body is None else json.dumps(body).encode(),
            {'Authorization':'Bearer '+'test-token'*8}),timeout=3) as r:return json.load(r)
    try:
        with pytest.raises(HTTPError) as e:urlopen(base+'status')
        assert e.value.code==401
        assert call('status')['controls_available'] and posts==[]
        for i,name in enumerate(('take_manual','start','record','stop_recording','stop_camera')):
            body={'id':str(i),'action':name,'operator':'Operator'}
            call('commands',body);wait(camera);call('commands',body)  # lost-ACK replay does not re-execute
            assert camera.snapshot()['last_command']['state']=='acknowledged'
        assert posts==['fastcam/command','high-speed/enabled','fastcam/command','fastcam/command',
            'high-speed/enabled','fastcam/command']
        assert manager.store.events(key)[0]['kind']=='manual_fastcam_requested'
    finally:
        camera.close();api.shutdown();api_thread.join();api.server_close();manager.lockfile.close()
        esp.shutdown();esp_thread.join();esp.server_close()


def test_real_schedule_worker_is_finished_before_manual_recording_beyond_old_deadline():
    from beanoflight.machine_jobs.fastcam import ScheduledCapture
    from test_machine_fastcam import plan,wait as wait_schedule
    clock=[100.];client=FakeCamera(False)
    schedule=ScheduledCapture(client,plan(False),lambda *v:None,clock=lambda:clock[0])
    schedule.prepare();schedule.begin(100);clock[0]=110;wait_schedule(schedule,'recording')
    m=ManualCamera(client,lambda:True,schedule,lambda *v:None)
    try:
        assert action(m,'take_manual')['state']=='acknowledged'
        assert not schedule.thread.is_alive()
        assert schedule.snapshot()['state']=='interrupted'
        action(m,'start');action(m,'record')
        clock[0]=145
        assert client.status['recording'] and not schedule.thread.is_alive()
    finally:m.close();schedule.finish()


def test_busy_rejection_does_not_reserve_command_id_or_repeat_uncertain_start(tmp_path):
    entered=threading.Event();release=threading.Event()
    class Blocked(FakeCamera):
        def request(self,path,body=None):
            if body and body.get('command')=='start':entered.set();assert release.wait(3)
            return super().request(path,body)
    manager=Manager(config(tmp_path));key=str(uuid4());manager.store.create(key,{'source':'live'})
    c=Blocked(False);m=ManualCamera(c,lambda:True,None,lambda *v:None);manager.runners[key]=SimpleNamespace(manual_fastcam=m)
    try:
        data={'action':'take_manual','operator':'Test'};manager.camera_command(key,'claim',data);wait(m)
        start={'action':'start','operator':'Test'};manager.camera_command(key,'start-once',start);assert entered.wait(1)
        manager.camera_command(key,'start-once',start)  # Same id is an acknowledgement replay even while busy.
        with pytest.raises(ValueError,match='current camera command'):
            manager.camera_command(key,'later',{'action':'stop_camera','operator':'Test'})
        with manager.store.connect() as db:assert db.execute('select count(*) from commands').fetchone()[0]==2
        release.set();wait(m)
        manager.camera_command(key,'later',{'action':'stop_camera','operator':'Test'});wait(m)
        assert m.snapshot()['last_command']['state']=='acknowledged'
    finally:release.set();m.close();manager.lockfile.close()


def test_external_trigger_enable_waits_for_USB_camera_readiness():
    class DelayedReady(FakeCamera):
        reads=0;starting=False
        def request(self,path,body=None):
            if path=='high-speed/enabled' and body['enabled']:assert self.status['camera_ready']
            result=super().request(path,body)
            if body and body.get('command')=='start':
                self.starting=True;self.status['camera_ready']=False;result['camera_ready']=False
            if self.starting and path=='fastcam/status':
                self.reads+=1
                if self.reads>=2:self.status['camera_ready']=True;result['camera_ready']=True
            return result
    client=DelayedReady();m=ManualCamera(client,lambda:True,None,lambda *v:None)
    try:
        action(m,'take_manual');assert action(m,'start')['state']=='acknowledged'
        assert client.reads>=2
    finally:m.close()
