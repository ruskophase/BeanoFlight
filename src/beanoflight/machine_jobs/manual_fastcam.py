"""Job-scoped manual USB-camera control. No independent ESP32 lease or frame work."""
import queue
import threading
import time
from .fastcam import SETTINGS,number

COMMANDS={'take_manual','start','stop_camera','record','stop_recording','settings'}
PREVIEW={'preview_fps','disable_preview','disable_preview_during_recording'}


def validate_request(data):
    if not isinstance(data,dict) or set(data)-{'action','settings','operator'}:
        raise ValueError('Invalid manual camera request')
    action=data.get('action')
    if action not in COMMANDS:raise ValueError('Unknown manual camera action')
    actor=data.get('operator')
    if not isinstance(actor,str) or not 1<=len(actor)<=128:raise ValueError('Operator identity required')
    if action!='settings':
        if 'settings' in data:raise ValueError('Settings require the settings action')
        return
    settings=data.get('settings')
    if not isinstance(settings,dict) or not settings or set(settings)-set(SETTINGS):raise ValueError('Unknown camera settings')
    for key,value in settings.items():
        if key=='device':
            if value is not None and (not isinstance(value,str) or not value.startswith('/dev/') or len(value)>4096):
                raise ValueError('Device must be null or a /dev/ path')
        elif key=='output_folder':
            if not isinstance(value,str) or not value.strip() or len(value)>4096 or '\0' in value:raise ValueError('Enter a Pi output folder')
        elif key=='width':
            if type(value) is not int or value not in (640,1280):raise ValueError('Choose 640 or 1280 width')
        elif key=='fps':number(value,1,200,'fps')
        elif key=='exposure_ms':number(value,.1,100,'exposure')
        elif key=='preview_fps':number(value,1,60,'preview fps',integer=True)
        elif type(value) is not bool:raise ValueError('Invalid camera option '+key)


class ManualCamera:
    def __init__(self,client,active,scheduled,emit):
        self.client=client;self.active=active;self.scheduled=scheduled;self.emit=emit
        self.lock=threading.Lock();self.quit=threading.Event();self.pending=queue.Queue(1)
        self.thread=threading.Thread(target=self._work,name='manual-fastcam',daemon=True)
        self.mode=False;self.touched=False;self.triggered=False;self.started=False
        self.revision=0
        self.data={'manual':False,'busy':False,'last_command':None,'error':None,'camera':None,'connected':False}

    def snapshot(self):
        with self.lock:return dict(self.data)

    def update(self,**values):
        with self.lock:self.data={**self.data,**values}

    def require_active(self):
        if self.quit.is_set() or not self.active():raise ValueError('Manual camera control requires this live job to be running or paused')

    def submit(self,key,data):
        validate_request(data);self.require_active()
        with self.lock:
            if self.data['busy']:raise ValueError('Wait for the current camera command to finish')
            if not self.mode and data['action']!='take_manual':raise ValueError('Choose Use manual camera controls first')
            self.data={**self.data,'busy':True,'last_command':{'id':key,**data,'state':'requested'},'error':None}
            self.revision+=1
            # Intent is journalled before queueing hardware work; never replayed automatically.
            try:self.emit('manual_fastcam_requested',{'id':key,**data})
            except Exception:
                self.data={**self.data,'busy':False};raise
            self.pending.put_nowait((key,data))
            if not self.started:
                self.started=True;self.thread.start()
        return {'accepted':True,'id':key}

    def check_submission(self,data):
        validate_request(data);self.require_active()
        with self.lock:
            if self.data['busy']:raise ValueError('Wait for the current camera command to finish')
            if not self.mode and data['action']!='take_manual':raise ValueError('Choose Use manual camera controls first')

    def request_stop(self):self.quit.set()

    def close(self):
        self.request_stop()
        if self.started:
            self.thread.join(75)
            if self.thread.is_alive():raise RuntimeError('Manual camera shutdown unconfirmed; inspect the Pi')

    def observe(self,status):
        self.update(camera=status,connected=True,last_received_at=time.time())

    def read_status(self):
        available=not self.quit.is_set() and self.active()
        if available:
            with self.lock:revision=self.revision
            try:
                camera=self.client.request('fastcam/status')
                high=self.client.request('state').get('high_speed',{})
                with self.lock:
                    if revision==self.revision:
                        self.data={**self.data,'camera':camera,'connected':True,'last_received_at':time.time(),
                            'high_speed':high,'connection_error':None}
            except Exception as exc:
                with self.lock:
                    if revision==self.revision:self.data={**self.data,'connected':False,'connection_error':str(exc)}
        result=self.snapshot()
        result['controls_available']=not self.quit.is_set() and self.active()
        result['scheduled']=self.scheduled.snapshot() if self.scheduled else None
        return result

    def command(self,name):
        status=self.client.request('fastcam/command',{'command':name});self.observe(status)
        if name in {'start','arm','record'} and status.get('error'):raise RuntimeError(status['error'])
        if name=='start' and not status.get('camera_running'):raise RuntimeError('Camera start was not acknowledged')
        if name in {'arm','record'} and not (status.get('recording') or status.get('armed')):
            raise RuntimeError('Recording activation was not acknowledged; it was not retried')
        return status

    def trigger(self,enabled):
        if enabled:
            self.require_active()
            self.triggered=True  # A lost acknowledgement still requires OFF during cleanup.
        self.client.request('high-speed/enabled',{'enabled':enabled})
        if not enabled:self.triggered=False

    def _execute(self,data):
        action=data['action'];self.require_active()
        if action=='take_manual':
            if self.mode:return
            if self.scheduled:
                self.scheduled.finish()  # Ends automatic window/cleanup before manual commands.
                if self.scheduled.snapshot().get('state')=='unconfirmed':raise RuntimeError('Scheduled camera shutdown is unconfirmed')
            self.require_active()
            status=self.client.request('fastcam/status');self.observe(status)
            if any(status.get(k) for k in ('camera_running','recording','armed','finishing')):
                raise RuntimeError('An existing camera session is still active; it was not taken over')
            self.mode=True;self.update(manual=True)
            return
        if not self.mode:raise ValueError('Manual camera mode is not active')
        status=self.client.request('fastcam/status');self.observe(status);self.require_active()
        if action=='settings':
            patch={k:v for k,v in data['settings'].items() if status.get('settings',{}).get(k)!=v}
            if patch.keys()-PREVIEW and any(status.get(k) for k in ('camera_running','recording','armed','finishing')):
                raise ValueError('Stop the USB camera before editing acquisition settings')
            if patch:
                result=self.client.request('fastcam/settings',patch);self.observe(result)
                if any(result.get('settings',{}).get(k)!=v for k,v in patch.items()):raise RuntimeError('Camera settings were not confirmed')
        elif action=='start':
            if any(status.get(k) for k in ('camera_running','recording','armed','finishing')):raise ValueError('Camera is already active')
            self.touched=True;ready=self.command('start');deadline=time.monotonic()+10
            while not ready.get('camera_ready'):
                self.require_active()
                if ready.get('error') or not ready.get('camera_running') or time.monotonic()>=deadline:
                    raise RuntimeError(ready.get('error') or 'USB camera did not become ready')
                self.quit.wait(.05);self.require_active()
                ready=self.client.request('fastcam/status');self.observe(ready)
            self.require_active()
            if status['settings']['external_trigger']:self.trigger(True)
        elif action=='record':
            if not status.get('camera_ready') or any(status.get(k) for k in ('recording','armed','finishing')):
                raise ValueError('Start the camera and wait until ready before recording')
            self.touched=True;self.command('arm' if status['settings']['external_trigger'] else 'record')
        elif action=='stop_recording':
            self.command('disarm' if status['settings']['external_trigger'] else 'stop')
        elif action=='stop_camera':
            self._cleanup()

    def _cleanup(self):
        errors=[]
        if self.triggered:
            try:self.trigger(False)
            except Exception as exc:errors.append('GPIO33 off unconfirmed: '+str(exc))
        if self.touched:
            try:
                status=self.command('stop_camera')
                deadline=time.monotonic()+10
                while status.get('finishing') and time.monotonic()<deadline:
                    time.sleep(.1);status=self.client.request('fastcam/status');self.observe(status)
                if any(status.get(k) for k in ('camera_running','recording','armed','finishing')):
                    raise RuntimeError('USB camera shutdown is unconfirmed')
                self.touched=False
            except Exception as exc:errors.append(str(exc))
        if errors:raise RuntimeError('; '.join(errors))

    def _work(self):
        try:
            while not self.quit.is_set():
                try:key,data=self.pending.get(timeout=.1)
                except queue.Empty:continue
                error=None
                try:self._execute(data)
                except Exception as exc:error=str(exc)
                result={'id':key,**data,'state':'failed' if error else 'acknowledged','error':error}
                with self.lock:
                    self.revision+=1
                    self.data={**self.data,'busy':False,'last_command':result,'error':error}
                self.emit('manual_fastcam_result',{**result,'camera':self.snapshot().get('camera')})
        finally:
            self.quit.set()
            try:
                key,data=self.pending.get_nowait()
                cancelled={'id':key,**data,'state':'cancelled','error':'Job ended before this camera command ran'}
                self.update(last_command=cancelled)
                self.emit('manual_fastcam_result',cancelled)
            except queue.Empty:pass
            try:
                self._cleanup();self.update(manual=False,busy=False,closed=True)
                self.emit('manual_fastcam_closed',self.snapshot())
            except Exception as exc:
                self.update(error=str(exc),closed=True)
                self.emit('manual_fastcam_cleanup_unconfirmed',{'error':str(exc)})
