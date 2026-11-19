"""Optional USB-camera schedule; all network IO stays outside frame processing."""
import json
import math
import threading
import time
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler

SETTINGS=('device','width','fps','exposure_ms','preview_fps','output_folder','external_trigger',
          'disable_preview','disable_preview_during_recording')
TRIGGER=('mode','multiplier','fixed_frequency_hz','pulse_us','illumination')


def number(value,low,high,name,*,integer=False):
    if type(value) not in (int,float) or not math.isfinite(value) or not low<=value<=high or (integer and int(value)!=value):
        raise ValueError('Invalid FastCamUSB '+name)
    return value


def validate(plan,duration,source,cameras=None):
    if plan is None:return
    if source!='live' or not isinstance(plan,dict) or plan.get('schema')!='beano-fastcam-plan/v1':
        raise ValueError('FastCamUSB recording requires a live job and a valid capture plan')
    start=number(plan.get('start_seconds'),0,duration,'start time')
    end=number(plan.get('end_seconds'),0,duration,'end time')
    if end<=start:raise ValueError('FastCamUSB end must be after start and within the job duration')
    if plan.get('time_basis')!='elapsed_since_first_feed':raise ValueError('Unknown FastCamUSB timing basis')
    settings=plan.get('settings')
    if not isinstance(settings,dict) or set(settings)!=set(SETTINGS):raise ValueError('Incomplete FastCamUSB settings')
    if settings['width'] not in (640,1280):raise ValueError('Invalid FastCamUSB resolution')
    for name,low,high,integer in [('fps',1,200,False),('exposure_ms',.1,100,False),('preview_fps',1,60,True)]:
        number(settings[name],low,high,name,integer=integer)
    if settings['device'] is not None and (not isinstance(settings['device'],str) or len(settings['device'])>4096):
        raise ValueError('Invalid FastCamUSB device')
    if not isinstance(settings['output_folder'],str) or not 1<=len(settings['output_folder'])<=4096:
        raise ValueError('Enter the FastCamUSB output folder')
    for name in ('external_trigger','disable_preview','disable_preview_during_recording'):
        if type(settings[name]) is not bool:raise ValueError('Invalid FastCamUSB '+name)
    if settings['external_trigger']:
        trigger=plan.get('trigger')
        if not isinstance(trigger,dict) or set(trigger)!=set(TRIGGER):raise ValueError('Pin the GPIO33 trigger settings')
        if trigger['mode'] not in ('multiple','fixed') or type(trigger['illumination']) is not bool:
            raise ValueError('Invalid FastCamUSB trigger mode')
        number(trigger['multiplier'],1,16,'trigger multiplier',integer=True)
        number(trigger['fixed_frequency_hz'],1,199.999,'fixed trigger rate')
        number(trigger['pulse_us'],1,1_000_000,'trigger pulse',integer=True)
        if trigger['mode']=='fixed' and trigger['illumination']:raise ValueError('Shared illumination requires a multiple trigger rate')
        if cameras:
            period=round(1_000_000/cameras['frame_rate_hz'])
            slot=period//trigger['multiplier'] if trigger['mode']=='multiple' else round(1_000_000/trigger['fixed_frequency_hz'])
            width=cameras['strobe_lead_us']+max(cameras['trigger_low_us'],trigger['pulse_us'])+cameras['strobe_lag_us']
            if slot<=5000 or width>=slot:raise ValueError('FastCamUSB trigger pulses must fit and remain below 200 Hz')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class CameraHTTP:
    def __init__(self,url,client_id=''):
        self.url=url.rstrip('/');self.client_id=client_id
        self.opener=build_opener(ProxyHandler({}),NoRedirect())
    def request(self,path,body=None):
        data=None if body is None else json.dumps({'client_id':self.client_id,**body},allow_nan=False).encode()
        request=Request(self.url+'/api/'+path,data,{'Content-Type':'application/json'})
        with self.opener.open(request,timeout=12 if body is not None else 3) as response:
            raw=response.read(256*1024+1)
            if len(raw)>256*1024:raise RuntimeError('FastCamUSB response exceeds the supported limit')
            result=json.loads(raw)
            if not isinstance(result,dict):raise RuntimeError('Invalid FastCamUSB status')
            if path.startswith('fastcam/') and (not isinstance(result.get('settings'),dict) or
                any(type(result.get(key)) is not bool for key in ('camera_running','recording','armed','finishing'))):
                raise RuntimeError('FastCamUSB did not return confirmed camera/recording state')
            return result
    def frame(self):
        request=Request(self.url+'/api/fastcam/frame',headers={'Accept':'image/jpeg'})
        with self.opener.open(request,timeout=3) as response:
            body=response.read(4*1024*1024+1)
            if len(body)>4*1024*1024 or not body.startswith(b'\xff\xd8'):
                raise ValueError('Camera preview is not a supported JPEG')
            return body


def defaults(controller_url):
    client=CameraHTTP(controller_url);camera=client.request('fastcam/status');state=client.request('state')
    settings={key:camera['settings'][key] for key in SETTINGS};settings['external_trigger']=True
    high=state.get('high_speed',{})
    return {'settings':settings,'trigger':{key:high.get(key) for key in TRIGGER},
        'trigger_available':bool(high.get('available')),'source':'Current Waveform Generator / FastCamUSB listener settings',
        'read_at':time.time(),'listener_url':camera.get('listener_url')}


class ScheduledCapture:
    def __init__(self,client,plan,emit,*,clock=time.monotonic,monitor_interval=1):
        self.client=client;self.plan=plan;self.emit=emit;self.clock=clock
        self.stop=threading.Event();self.ready=threading.Event();self.go=threading.Event();self.lock=threading.Lock()
        self.origin=None;self.owned=False;self.triggered=False;self.thread=threading.Thread(target=self._work,name='fastcam-schedule',daemon=True)
        self.monitor_stop=threading.Event();self.monitor_thread=None;self.monitor_interval=monitor_interval
        self.data={'state':'preparing','plan':plan,'error':None,'start_acknowledged':False,'stop_acknowledged':False}
    def snapshot(self):
        with self.lock:return dict(self.data)
    def update(self,state,**values):
        with self.lock:self.data={**self.data,'state':state,**values}
        self.emit('fastcam',{'state':state,**values,'elapsed_seconds':self.clock()-self.origin if self.origin is not None else None})
    def observe(self,status):
        keep={k:status[k] for k in ('camera_running','camera_ready','armed','recording','finishing','recorded_frames',
            'video_path','timestamp_path','capture_fps','stream_fps','sequence_gaps','invalid','error','settings',
            'recorder','recorder_policy') if k in status}
        with self.lock:
            previous=self.data.get('camera_report',{})
            for key in ('video_path','timestamp_path'):
                if not keep.get(key) and previous.get(key):keep[key]=previous[key]
            values={'camera_report':keep,'last_status_received_at':time.time(),'monitor_error':None}
            if status.get('error') and self.data.get('start_acknowledged') and not self.data.get('recording_error'):
                diagnostics=status.get('recorder') or {}
                values.update(recording_error=status['error'],failure_at=diagnostics.get('failure_at'),
                    failure_recording_elapsed_seconds=diagnostics.get('failure_elapsed_seconds'),
                    failure_observed_elapsed_seconds=self.clock()-self.origin if self.origin is not None else None)
            self.data={**self.data,**values}
    def _monitor(self):
        # A slow GET must never delay the independent timer issuing STOP/DISARM.
        while not self.monitor_stop.is_set():
            try:
                status=self.client.request('fastcam/status')
                if self.monitor_stop.is_set():return  # Ignore late replies after shutdown began.
                self.observe(status)
            except Exception as exc:
                if self.monitor_stop.is_set():return
                with self.lock:self.data={**self.data,'monitor_error':str(exc),'monitor_error_at':time.time()}
            with self.lock:report=dict(self.data)
            self.emit('fastcam',report)
            self.monitor_stop.wait(self.monitor_interval)
    def prepare(self):
        self.thread.start()
        if not self.ready.wait(25):self.request_stop();raise RuntimeError('FastCamUSB preparation timed out before feeding')
        if self.snapshot()['error']:raise RuntimeError('FastCamUSB preparation failed: '+self.snapshot()['error'])
        if self.stop.is_set():raise RuntimeError('FastCamUSB preparation was cancelled before feeding')
    def begin(self,origin):
        with self.lock:
            if self.origin is None:self.origin=origin
        self.go.set()
    def request_stop(self):self.stop.set()
    def finish(self):
        self.request_stop();self.thread.join(75)
        if self.thread.is_alive():self.update('unconfirmed',error='FastCamUSB shutdown is not confirmed; check the camera')
    def wait_until(self,deadline):
        while not self.stop.is_set():
            remaining=deadline-self.clock()
            if remaining<=0:return True
            self.stop.wait(min(.1,remaining))
        return False
    def command(self,name):
        result=self.client.request('fastcam/command',{'command':name});self.observe(result)
        # A recorder error is evidence to preserve; it does not negate a confirmed
        # STOP acknowledgement or prevent the camera cleanup command.
        if result.get('error') and name not in {'stop','disarm','stop_camera'}:raise RuntimeError(result['error'])
        if name in {'arm','record'} and not (result.get('armed') or result.get('recording')):
            raise RuntimeError('FastCamUSB did not acknowledge recording activation')
        return result
    def _work(self):
        recording=False;error=None;ended_early=False
        try:
            status=self.client.request('fastcam/status')
            if any(status.get(k) for k in ('camera_running','recording','armed','finishing')):
                raise RuntimeError('FastCamUSB is already in use; this job did not take over the existing session')
            applied=self.client.request('fastcam/settings',self.plan['settings']);self.observe(applied)
            if any(applied.get('settings',{}).get(key)!=value for key,value in self.plan['settings'].items()):
                raise RuntimeError('FastCamUSB did not confirm the requested camera settings')
            if self.plan['settings']['external_trigger']:
                high=self.client.request('state').get('high_speed',{})
                if not high.get('available') or high.get('enabled') or any(high.get(key)!=value for key,value in self.plan['trigger'].items()):
                    raise RuntimeError('Controller did not confirm the pinned GPIO33 trigger settings')
                self.update('preparing',trigger_report={key:high.get(key) for key in (*TRIGGER,'frequency_hz','enabled')})
            if self.stop.is_set():return
            self.owned=True;self.command('start')  # An uncertain reply still requires cleanup, never a retry.
            deadline=self.clock()+10
            while not self.stop.is_set():
                status=self.client.request('fastcam/status');self.observe(status)
                if status.get('camera_ready'):break
                if status.get('error') or self.clock()>=deadline:raise RuntimeError(status.get('error') or 'FastCamUSB was not ready')
                self.stop.wait(.1)
            self.update('waiting');self.ready.set()
            while not self.go.wait(.1):
                if self.stop.is_set():return
            if not self.wait_until(self.origin+self.plan['start_seconds']):return
            external=self.plan['settings']['external_trigger']
            self.update('starting_recording',requested_start_seconds=self.plan['start_seconds'])
            self.command('arm' if external else 'record');recording=True
            if self.stop.is_set():return
            if external:
                self.triggered=True
                self.client.request('high-speed/enabled',{'enabled':True})
            self.update('recording',start_acknowledged=True,actual_start_seconds=self.clock()-self.origin)
            self.monitor_thread=threading.Thread(target=self._monitor,name='fastcam-monitor',daemon=True)
            self.monitor_thread.start()
            self.wait_until(self.origin+self.plan['end_seconds'])
        except Exception as exc:
            error=type(exc).__name__+': '+str(exc)
        finally:
            self.monitor_stop.set()
            if self.triggered:
                try:self.client.request('high-speed/enabled',{'enabled':False})
                except Exception as exc:error=error or 'GPIO33 shutdown unconfirmed: '+str(exc)
            if recording:
                try:
                    ended_early=self.clock()<self.origin+self.plan['end_seconds']
                    self.update('finishing',actual_stop_requested_seconds=self.clock()-self.origin,window_ended_early=ended_early)
                    status=self.command('disarm' if self.plan['settings']['external_trigger'] else 'stop')
                    deadline=self.clock()+10
                    while status.get('finishing') and self.clock()<deadline:
                        time.sleep(.1);status=self.client.request('fastcam/status');self.observe(status)
                    if any(status.get(k) for k in ('recording','armed','finishing')):raise RuntimeError('Camera recording stop is unconfirmed')
                    self.update('finishing',stop_acknowledged=True)
                except Exception as exc:error=error or str(exc)
            if self.owned:
                try:
                    status=self.command('stop_camera')
                    if any(status.get(k) for k in ('camera_running','recording','armed','finishing')):raise RuntimeError('Camera shutdown is unconfirmed')
                except Exception as exc:error=error or str(exc)
            if self.monitor_thread:self.monitor_thread.join(3.5)
            evidence=self.snapshot();error=error or evidence.get('recording_error')
            observed=evidence.get('failure_observed_elapsed_seconds')
            recorded=evidence.get('failure_recording_elapsed_seconds')
            failed_early=bool(evidence.get('recording_error') and (
                (observed is not None and observed<self.plan['end_seconds']) or
                (recorded is not None and recorded<self.plan['end_seconds']-self.plan['start_seconds'])))
            try:self.update('failed' if error else 'interrupted' if ended_early else 'completed' if recording else 'cancelled',
                error=error,ended_early=ended_early or failed_early,recording_failed_early=failed_early)
            finally:self.ready.set()
