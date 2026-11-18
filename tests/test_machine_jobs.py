"""Machine journal and control boundary tests; never access physical devices."""
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from uuid import uuid4
import pytest
from beanoflight.machine_jobs.store import Store,fingerprint
from beanoflight.machine_jobs.server import Manager,make_handler
from beanoflight.machine_jobs.runner import Journal,Runner


def test_store_start_identity_command_deduplication_and_restart(tmp_path):
    s=Store(tmp_path/'j.db');key=str(uuid4());request={'a':1}
    assert s.create(key,request);assert not s.create(key,request)
    with pytest.raises(ValueError):s.create(key,{'a':2})
    with pytest.raises(ValueError):s.create(str(uuid4()),request)
    calls=[]
    for _ in range(2):s.command(key,'pause-1','pause',lambda:calls.append('pause'))
    assert calls==['pause']
    with pytest.raises(ValueError):s.command(key,'pause-1','resume',lambda:None)
    j=Journal(s,key)
    for n in range(150):j.put('measurement',{'n':n})
    j.flush();j.close();assert len(s.events(key,0,100))==100
    assert s.events(key,100,100)[0]['data']['n']==100
    s.recover();job=s.job(key)
    assert job['state']=='failed' and job['final_sequence']==151
    assert s.events(key,150)[0]['kind']=='failed'


def config(tmp_path):
    pack=tmp_path/'pack';pack.mkdir();(pack/'calibration.json').write_text('{}')
    nozzle=tmp_path/'map.json';nozzle.write_text('{}')
    settings=tmp_path/'settings.json';settings.write_text(json.dumps({'name':'Test','cameras':{'frame_rate_hz':59.5}}))
    return {'data_root':str(tmp_path/'jobs'),'machine_id':'machine-1','settings_file':str(settings),
            'calibration_pack':str(pack),'nozzle_map':str(nozzle),'recordings':{}}


def test_api_auth_and_manifest_pin_never_start_unvalidated_hardware(tmp_path,monkeypatch):
    from http.server import ThreadingHTTPServer
    from urllib.request import Request,urlopen
    from urllib.error import HTTPError
    m=Manager(config(tmp_path));catalog=m.catalog();calls=[]
    class FakeRunner:
        def __init__(self,*args):self.thread=SimpleNamespace(start=lambda:calls.append(args))
    monkeypatch.setattr('beanoflight.machine_jobs.server.Runner',FakeRunner)
    server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(m,'secret'*10));thread=threading.Thread(target=server.serve_forever);thread.start()
    url='http://127.0.0.1:'+str(server.server_port)
    try:
        with pytest.raises(HTTPError) as error:urlopen(url+'/v1/catalog')
        assert error.value.code==401
        def call(path,data=None):
            with urlopen(Request(url+path,None if data is None else json.dumps(data).encode(),{'Authorization':'Bearer '+'secret'*10}),timeout=2) as r:return json.load(r)
        assert call('/v1/catalog')['images_over_network'] is True
        body={'machine_id':'machine-1','recipe':'all_pass_through','image_mode':'none','source':'live',
            'duration_seconds':1,'settings':catalog['settings'][0]['values'],'settings_sha256':catalog['settings'][0]['id'],
            'configuration':catalog['configuration']}
        key=str(uuid4());call('/v1/jobs/'+key,body);call('/v1/jobs/'+key,body)
        assert len(calls)==1
        body['recipe']='white-beans'
        with pytest.raises(HTTPError):call('/v1/jobs/'+str(uuid4()),body)
        with pytest.raises(HTTPError):call('/images/photo.png')
    finally:server.shutdown();thread.join();server.server_close();m.lockfile.close()


def test_controller_pause_only_changes_feed_and_does_not_repeat_configuration(monkeypatch):
    from beanoflight.machine_jobs.controller import Controller
    c=Controller('http://unused','job');calls=[];monkeypatch.setattr(c,'api',lambda p,**d:calls.append((p,d)))
    b={'conveyor':{'speed_steps_per_second':20,'direction':'reverse','acceleration_steps_per_second_squared':10},
       'vibration_motor':{'frequency_hz':4000,'duty_percent':38,'direction':'clockwise'},
       'cameras':{'frame_rate_hz':59.5,'trigger_low_us':50,'strobe_lead_us':250,'strobe_lag_us':10}}
    c.configure(b);before=len(calls);c.feed(True);c.feed(False);c.feed(True)
    assert [p for p,_ in calls[before:]]==['conveyor/enabled','vibration/enabled']*3
    assert all('valve' not in p for p,_ in calls)


def test_journal_failure_is_explicit_and_not_silently_dropped(tmp_path,monkeypatch):
    s=Store(tmp_path/'j.db');key=str(uuid4());s.create(key,{})
    def fail(*a):raise OSError('disk full')
    monkeypatch.setattr(s,'append',fail);j=Journal(s,key);j.put('measurement',{'x':1})
    with pytest.raises(RuntimeError,match='journal failed'):j.flush()
    with pytest.raises(RuntimeError,match='journal failed'):j.put('measurement',{'x':2})
    j.close()


def test_restart_recovers_measured_active_beans(tmp_path):
    s=Store(tmp_path/'j.db');s.create('job',{})
    s.append('job','tracked',{'bean_sequence':42,'ordinal':0})
    for value in [10.,12.]:s.append('job','measurement',{'bean_sequence':42,'caml_lab_l_mean':value})
    s.recover();rows=s.events('job');bean=next(r['data'] for r in rows if r['kind']=='bean')
    assert bean['features']['caml_lab_l_mean']==11.
    assert bean['sample_count']==2 and bean['terminal_status']=='service_interrupted'
    assert s.job('job')['state']=='failed'


@pytest.mark.parametrize('start_paused',[False,True])
@pytest.mark.parametrize('prime_failure',[False,True])
def test_live_lifecycle_with_fake_devices_pause_keeps_cameras_and_duration_excludes_pause(tmp_path,monkeypatch,start_paused,prime_failure):
    from beanoflight.machine_jobs.provenance import configuration
    import beanoflight.live_source as live
    import beanoflight.analysis as analysis
    import beanoflight.calibration as calibration
    import beanoflight.nozzle_map as nozzle
    import beanoflight.machine_jobs.measurements as measurement
    import beanoflight.machine_jobs.runner as module
    c=config(tmp_path);c['controller_url']='http://fake'
    c['maximum_pause_seconds']=10
    class Device:
        instance=None
        def __init__(self,*a):Device.instance=self;self.owned=False;self.error=None;self.calls=[]
        def acquire(self):self.owned=True;self.calls.append('acquire')
        def configure(self,b):self.calls.append('configure')
        def verify_configuration(self,b):self.calls.append('verified')
        def feed(self,on):
            if on:assert Source.instance.primed,'Live source must be primed before feeding starts'
            self.calls.append(('feed',on))
        def api(self,path):self.calls.append(path)
        def close(self):self.owned=False;self.calls.append('release')
    class Producer:
        instance=None
        def __init__(self,*a,**k):Producer.instance=self;self.closed=False;self.clock_source_timestamp_ns=1;self.clock_monotonic_ns=1
        def start(self):pass
        def check_running(self):pass
        def clock_snapshot(self):return SimpleNamespace(source_to_monotonic_ns=lambda n:time.monotonic_ns())
        def drain_events(self):pass
        def close(self):self.closed=True
    class Source:
        instance=None
        def __init__(self,*a,**k):
            self.metadata=SimpleNamespace(width=1456,height=1088,frame_count=10000);self.pipeline_metadata={}
            Source.instance=self;self.background_ready=False;self.primed=False;self.prime_calls=0;self.indices=[];self.closed=False
        def acquire_background(self,*a):self.background_ready=True;return None
        def prime(self):
            assert self.background_ready,'Empty background must be acquired before priming'
            self.prime_calls+=1
            if prime_failure:raise live.SourceError('Synthetic first stereo pair unavailable')
            self.primed=True
        def frame(self,i):
            if i==0 and not self.primed:raise live.SourceError('live source must be primed before frame zero')
            assert i==len(self.indices)
            self.indices.append(i);time.sleep(.01);return object()
        def timestamp_ns(self,i):return 1000000000+i*16666667
        def release_frame(self,f):pass
        def stereo_statistics(self):return {}
        def close(self):self.closed=True
    class Engine:
        def __init__(self,*a,**k):self.tracker=SimpleNamespace(run_id='test')
        def process(self,*a):return SimpleNamespace(tracks=[],predictions=[])
    monkeypatch.setattr(module,'Controller',Device);monkeypatch.setattr(live,'FastCapLiveProducer',Producer)
    monkeypatch.setattr(live,'SharedMemoryRawStereoSource',Source);monkeypatch.setattr(analysis,'AnalysisEngine',Engine)
    monkeypatch.setattr(calibration.MetricPlaneCalibration,'from_pinkplane',lambda *a,**k:object())
    monkeypatch.setattr(nozzle,'resolve_gate_layout',lambda *a:SimpleNamespace(to_dict=lambda:{}))
    monkeypatch.setattr(measurement,'Measurements',lambda *a:SimpleNamespace(close=lambda:None))
    # The runner records the homography hash, even with fake devices.
    pack=Path(c['calibration_pack']);(pack/'geometry').mkdir();(pack/'geometry/homography.json').write_text('{}')
    b={'cameras':{'frame_rate_hz':60}}
    req={'source':'live','image_mode':'none','duration_seconds':.3,'settings':b,'settings_sha256':fingerprint(b),'configuration':configuration(c),'start_paused':start_paused}
    s=Store(tmp_path/'journal.db');key=str(uuid4());s.create(key,req);r=Runner(s,key,req,c);r.thread.start()
    if prime_failure:
        r.thread.join(5)
        assert not r.thread.is_alive() and r.state=='failed'
        assert 'Synthetic first stereo pair unavailable' in r.error
        assert ('feed',True) not in Device.instance.calls
        assert Source.instance.prime_calls==1 and Source.instance.indices==[] and Source.instance.closed
        assert Producer.instance.closed and Device.instance.calls[-1]=='release'
        assert r.active==0 and s.job(key)['final_sequence'] is not None
        return
    def wait(state):
        limit=time.monotonic()+5
        while r.state!=state and time.monotonic()<limit:time.sleep(.01)
        assert r.state==state,r.error
    if start_paused:
        wait('paused');assert ('feed',True) not in Device.instance.calls
        r.command('resume')
    wait('running');r.command('pause');wait('paused');before=r.active
    time.sleep(.2);assert abs(r.active-before)<.025 and not Producer.instance.closed
    r.command('resume');wait('running');r.thread.join(6)
    assert r.state=='completed',r.error
    assert .3<=r.active<.4 and r.status()['elapsed_seconds']>.5
    assert Device.instance.calls[:4]==['acquire','configure','verified',('feed',True)]
    assert ('feed',False) in Device.instance.calls and Device.instance.calls[-1]=='release'
    assert Producer.instance.closed and s.job(key)['final_sequence'] is not None
    assert Source.instance.prime_calls==1 and Source.instance.indices[0]==0 and Source.instance.closed
    assert not list((tmp_path/'jobs'/key).rglob('*.png'))
