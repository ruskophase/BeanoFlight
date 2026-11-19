"""USB recording schedules with fake clocks/devices; no camera/GPIO calls."""
from copy import deepcopy
import threading
import time
import pytest

from beanoflight.machine_jobs.fastcam import ScheduledCapture,defaults,validate


def plan(external=True):
    return {'schema':'beano-fastcam-plan/v1','time_basis':'elapsed_since_first_feed','start_seconds':10,'end_seconds':40,
        'settings':{'device':None,'width':1280,'fps':180,'exposure_ms':5,'preview_fps':30,
            'output_folder':'/test/recordings','external_trigger':external,'disable_preview':False,'disable_preview_during_recording':True},
        'trigger':{'mode':'multiple','multiplier':3,'fixed_frequency_hz':180,'pulse_us':50,'illumination':True} if external else None}


class Camera:
    def __init__(self,busy=False,fail=None):
        self.calls=[];self.fail=fail
        self.status={'camera_running':busy,'camera_ready':busy,'recording':False,'armed':False,'finishing':False,
            'settings':deepcopy(plan()['settings']),'video_path':None,'timestamp_path':None,'recorded_frames':0}
    def request(self,path,body=None):
        self.calls.append((path,body,threading.current_thread().name))
        if self.fail is not None and body and self.fail==body.get('command'):raise RuntimeError('Synthetic lost acknowledgement')
        if path=='state':return {'high_speed':{**plan()['trigger'],'available':True,'enabled':False,'frequency_hz':178.5}}
        if path=='fastcam/settings':self.status['settings']=deepcopy(body)
        if path=='fastcam/command':
            command=body['command']
            if command=='start':self.status.update(camera_running=True,camera_ready=True)
            if command in ('arm','record'):self.status.update(recording=True,armed=command=='arm')
            if command in ('stop','disarm'):self.status.update(recording=False,armed=False,recorded_frames=5400,
                video_path='/test/capture.avi',timestamp_path='/test/capture.csv')
            if command=='stop_camera':self.status.update(camera_running=False,camera_ready=False,recording=False,armed=False)
        return deepcopy(self.status)


def wait(capture,state):
    until=time.monotonic()+3
    while capture.snapshot()['state']!=state and time.monotonic()<until:time.sleep(.01)
    assert capture.snapshot()['state']==state,capture.snapshot()


@pytest.mark.parametrize('external',[True,False])
def test_ten_to_forty_window_is_independent_and_repeat_begin_does_not_reset_clock(external):
    clock=[100.];camera=Camera();events=[]
    capture=ScheduledCapture(camera,plan(external),lambda *args:events.append(args),clock=lambda:clock[0])
    capture.prepare();assert capture.snapshot()['state']=='waiting'
    assert not any(body and body.get('command') in ('arm','record') for _,body,_ in camera.calls)
    capture.begin(100);capture.begin(120)  # resume cannot move the original epoch
    clock[0]=110;wait(capture,'recording')
    clock[0]=140;capture.thread.join(3);assert not capture.thread.is_alive()
    status=capture.snapshot()
    assert status['state']=='completed' and status['start_acknowledged'] and status['stop_acknowledged']
    assert status['actual_start_seconds']==10 and status['actual_stop_requested_seconds']==40
    assert status['camera_report']['video_path']=='/test/capture.avi' and not status['ended_early']
    commands=[body['command'] for path,body,_ in camera.calls if path=='fastcam/command']
    assert commands==['start','arm' if external else 'record','disarm' if external else 'stop','stop_camera']
    triggers=[body for path,body,_ in camera.calls if path=='high-speed/enabled']
    assert triggers==([{'enabled':True},{'enabled':False}] if external else [])
    assert all(name=='fastcam-schedule' or (name=='fastcam-monitor' and path=='fastcam/status' and body is None)
        for path,body,name in camera.calls)
    assert all(kind=='fastcam' for kind,_ in events)


@pytest.mark.parametrize('started',[True,False])
def test_end_cleans_up_and_records_a_shortened_or_unstarted_window(started):
    clock=[100.];camera=Camera();capture=ScheduledCapture(camera,plan(),lambda *args:None,clock=lambda:clock[0])
    capture.prepare();capture.begin(100)
    if started:clock[0]=110;wait(capture,'recording')
    clock[0]=125 if started else 105;capture.finish()
    assert not capture.thread.is_alive() and not camera.status['camera_running']
    assert capture.snapshot()['state']==('interrupted' if started else 'cancelled')


def test_existing_camera_is_not_taken_over_and_uncertain_activation_is_never_retried():
    camera=Camera(busy=True);capture=ScheduledCapture(camera,plan(),lambda *args:None)
    with pytest.raises(RuntimeError,match='already in use'):capture.prepare()
    capture.finish();assert all(body is None for _,body,_ in camera.calls)
    clock=[100.];camera=Camera(fail='arm');capture=ScheduledCapture(camera,plan(),lambda *args:None,clock=lambda:clock[0])
    capture.prepare();capture.begin(100);clock[0]=110;capture.thread.join(3)
    assert capture.snapshot()['state']=='failed' and 'lost acknowledgement' in capture.snapshot()['error']
    assert not capture.snapshot()['start_acknowledged'] and not camera.status['camera_running']
    assert sum(bool(body and body.get('command')=='arm') for _,body,_ in camera.calls)==1
    assert not any(path=='high-speed/enabled' for path,_,_ in camera.calls)


def test_plan_validation_rejects_bad_windows_replay_and_trigger_configuration():
    cameras={'frame_rate_hz':59.5,'trigger_low_us':50,'strobe_lead_us':250,'strobe_lag_us':10}
    validate(plan(),60,'live',cameras);validate(None,60,'live')
    for changes in ({'start_seconds':40},{'end_seconds':61},{'start_seconds':float('nan')},{'time_basis':'unknown'}):
        with pytest.raises(ValueError):validate({**plan(),**changes},60,'live',cameras)
    with pytest.raises(ValueError):validate(plan(),60,'recorded',cameras)
    p=plan();p['trigger']['multiplier']=4
    with pytest.raises(ValueError,match='below 200'):validate(p,60,'live',cameras)
    p=plan();p['settings']['fps']=float('inf')
    with pytest.raises(ValueError):validate(p,60,'live',cameras)


def test_unconfirmed_settings_fail_before_camera_start():
    class WrongSettings(Camera):
        def request(self,path,body=None):
            result=super().request(path,body)
            if path=='fastcam/settings':result['settings']['fps']=10
            return result
    camera=WrongSettings();capture=ScheduledCapture(camera,plan(),lambda *args:None)
    with pytest.raises(RuntimeError,match='confirm the requested'):capture.prepare()
    capture.finish()
    assert not any(path=='fastcam/command' for path,_,_ in camera.calls)


def test_defaults_read_current_gui_values_without_commands(monkeypatch):
    import beanoflight.machine_jobs.fastcam as module
    values=plan(False);calls=[]
    class Reader:
        def __init__(self,*args):pass
        def request(self,path):
            calls.append(path)
            return {'settings':values['settings']} if path=='fastcam/status' else {'high_speed':{**plan()['trigger'],'available':True}}
    monkeypatch.setattr(module,'CameraHTTP',Reader)
    result=defaults('http://fake')
    assert calls==['fastcam/status','state'] and values['settings']['external_trigger'] is False
    assert result['settings']['external_trigger'] is True and result['settings']['fps']==180
    assert result['trigger']['multiplier']==3


def until(predicate):
    deadline=time.monotonic()+3
    while not predicate() and time.monotonic()<deadline:time.sleep(.005)
    assert predicate()


def test_failure_is_reported_during_window_with_source_timestamp_and_stop_acknowledgement():
    clock=[100.];camera=Camera()
    capture=ScheduledCapture(camera,plan(False),lambda *args:None,clock=lambda:clock[0],monitor_interval=.01)
    capture.prepare();capture.begin(100);clock[0]=110;wait(capture,'recording')
    clock[0]=125
    camera.status.update(error='Synthetic recorder buffer overflow',recorder={
        'failure_at':'2026-11-19T04:45:04+00:00','failure_elapsed_seconds':14.7,
        'peak_buffered_frames':900,'policy':{'frame_limit':900}})
    until(lambda:capture.snapshot().get('recording_error'))
    assert capture.snapshot()['state']=='recording'  # Window timing remains independent.
    assert capture.snapshot()['failure_observed_elapsed_seconds']==25
    clock[0]=140;capture.thread.join(3)
    d=capture.snapshot()
    assert d['state']=='failed' and d['stop_acknowledged']
    assert d['ended_early'] and d['recording_failed_early'] and not d['window_ended_early']
    assert d['actual_stop_requested_seconds']==40
    assert d['failure_at']=='2026-11-19T04:45:04+00:00'
    assert d['camera_report']['recorder']['peak_buffered_frames']==900
    assert not camera.status['camera_running']


def test_blocked_status_poll_cannot_delay_scheduled_stop_or_replace_final_report():
    blocked=threading.Event();release=threading.Event();stopped=threading.Event()
    class SlowCamera(Camera):
        def request(self,path,body=None):
            if path=='fastcam/status' and threading.current_thread().name=='fastcam-monitor':
                blocked.set();assert release.wait(3)
                return {'error':'Late stale response','settings':{},'recorded_frames':0}
            if body and body.get('command')=='stop':stopped.set()
            return super().request(path,body)
    clock=[100.];camera=SlowCamera()
    capture=ScheduledCapture(camera,plan(False),lambda *args:None,clock=lambda:clock[0])
    capture.prepare();capture.begin(100);clock[0]=110;wait(capture,'recording')
    assert blocked.wait(1)
    clock[0]=140
    assert stopped.wait(1), 'STOP waited for the blocked monitor GET'
    release.set();capture.thread.join(3)
    assert capture.snapshot()['state']=='completed'
    assert capture.snapshot()['camera_report']['recorded_frames']==5400
    assert not capture.snapshot().get('recording_error')


def test_monitor_disconnect_reports_unknown_telemetry_without_inventing_recording_failure():
    class Disconnected(Camera):
        def request(self,path,body=None):
            if path=='fastcam/status' and threading.current_thread().name=='fastcam-monitor':
                raise TimeoutError('Synthetic status timeout')
            return super().request(path,body)
    clock=[100.];camera=Disconnected()
    capture=ScheduledCapture(camera,plan(False),lambda *args:None,clock=lambda:clock[0])
    capture.prepare();capture.begin(100);clock[0]=110;wait(capture,'recording')
    until(lambda:capture.snapshot().get('monitor_error'))
    assert not capture.snapshot().get('recording_error')
    assert capture.snapshot()['start_acknowledged']
    clock[0]=140;capture.thread.join(3)
    assert capture.snapshot()['state']=='completed' and capture.snapshot()['stop_acknowledged']


def test_previous_session_error_in_settings_response_does_not_fail_new_recording():
    class OldError(Camera):
        def request(self,path,body=None):
            status=super().request(path,body)
            if path=='fastcam/settings':status['error']='Previous recording failed'
            return status
    clock=[100.];capture=ScheduledCapture(OldError(),plan(False),lambda *args:None,clock=lambda:clock[0])
    capture.prepare();capture.begin(100);clock[0]=110;wait(capture,'recording')
    clock[0]=140;capture.thread.join(3)
    assert capture.snapshot()['state']=='completed'
