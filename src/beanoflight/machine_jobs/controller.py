"""Single lease owner. Heartbeats are independent of camera and journal work."""
import json
import threading
import time
import urllib.request
from urllib.error import HTTPError


class Controller:
    def __init__(self,url,job_id):
        self.url=url.rstrip('/');self.client_id='beanosort-job:'+job_id
        self.owned=False;self.error=None;self.quit=threading.Event();self.thread=None
        self.last_progress=time.monotonic();self.watch_progress=False

    def api(self,path,**data):
        req=urllib.request.Request(self.url+'/api/'+path,
            json.dumps({'client_id':self.client_id,**data}).encode(),{'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=2) as r: return json.load(r)
        except HTTPError as exc:
            try:detail=json.loads(exc.read(4096)).get('detail',str(exc))
            except ValueError:detail=str(exc)
            raise RuntimeError(str(detail)) from None

    def acquire(self):
        self.api('control/acquire',operator_name='BeanoSort job');self.owned=True
        self.thread=threading.Thread(target=self._heartbeat,daemon=True);self.thread.start()
        self.api('stop')

    def state(self):
        with urllib.request.urlopen(self.url+'/api/state',timeout=2) as r:return json.load(r)

    def _heartbeat(self):
        while not self.quit.wait(.75):
            try:
                self.api('control/heartbeat')
                if self.watch_progress and time.monotonic()-self.last_progress>5:
                    raise RuntimeError('Image processing stopped making progress')
            except Exception as exc:
                self.error=str(exc)
                try:self.api('stop')
                except Exception:pass
                return

    def configure(self,b):
        c=b['conveyor'];v=b['vibration_motor'];a=b['cameras']
        self.api('conveyor/config',speed_sps=c['speed_steps_per_second'],direction=c['direction'],
            acceleration_sps2=c['acceleration_steps_per_second_squared'])
        self.api('vibration/config',frequency_hz=v['frequency_hz'],duty_percent=v['duty_percent'],direction=v['direction'])
        self.api('triggers/config',frequency_hz=a['frame_rate_hz'],low_us=a['trigger_low_us'])
        self.api('strobe/config',lead_us=a['strobe_lead_us'],lag_us=a['strobe_lag_us'])
        self.api('strobe/enabled',enabled=True);self.api('triggers/enabled',enabled=True)

    def verify_configuration(self,b):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            if self.error:raise RuntimeError(self.error)
            s=self.state();c=s.get('conveyor',{});v=s.get('vibration_motor',{});a=b['cameras']
            if (s.get('lease',{}).get('client_id')==self.client_id and s.get('mode')!='FAILSAFE'
                and s.get('triggers',{}).get('enabled') and s.get('strobe',{}).get('enabled')
                and abs(s['triggers']['frequency_hz']-a['frame_rate_hz'])<.01
                and s['triggers']['low_us']==a['trigger_low_us']
                and s['strobe']['lead_us']==a['strobe_lead_us'] and s['strobe']['lag_us']==a['strobe_lag_us']
                and c.get('speed_sps')==b['conveyor']['speed_steps_per_second']
                and c.get('direction')==b['conveyor']['direction']
                and v.get('frequency_hz')==b['vibration_motor']['frequency_hz']
                and v.get('duty_percent')==b['vibration_motor']['duty_percent']
                and v.get('direction')==b['vibration_motor']['direction']
                and not c.get('enabled') and not v.get('enabled') and not s.get('high_speed',{}).get('enabled')):
                return
            time.sleep(.1)
        raise RuntimeError('Controller did not confirm the pinned settings and capture readiness')

    def feed(self,enabled):
        self.api('conveyor/enabled',enabled=enabled);self.api('vibration/enabled',enabled=enabled)

    def close(self):
        if not self.owned:return
        try:self.api('control/release')
        finally:
            self.quit.set()
            if self.thread:self.thread.join(3)
            self.owned=False
