"""Authenticated job API, with on-demand saved crops for completed runs."""
import argparse
import fcntl
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import hmac
import json
import math
from pathlib import Path
import signal
import threading
from urllib.parse import urlsplit,parse_qs
import uuid
from .store import Store,encode,fingerprint
from .runner import Runner
from .provenance import configuration
from .crops import read_crop
from .fastcam import defaults as fastcam_defaults,validate as validate_fastcam
from .manual_fastcam import validate_request as validate_manual_fastcam


class Manager:
    def __init__(self,config):
        self.config=config;self.root=Path(config['data_root']);self.root.mkdir(parents=True,exist_ok=True)
        self.lockfile=(self.root/'service.lock').open('a')
        fcntl.flock(self.lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.store=Store(self.root/'journal.sqlite');self.store.recover();self.lock=threading.Lock();self.runners={}
    def catalog(self):
        bundles={}
        for path in self.config.get('settings_files',[self.config['settings_file']]):
            b=json.loads(Path(path).read_text());key=fingerprint(b)
            bundles[key]={'id':key,'name':b['name'],'values':b}
        return {'schema':'beano-machine-api/v1','machine_id':self.config['machine_id'],
            'recipes':['all_pass_through'],'image_modes':['raw','stereo','none'],
            'settings':list(bundles.values()),
            'sources':[{'id':'live','name':'Live cameras','physical':True}]+[
                {'id':k,'name':v['name'],'physical':False} for k,v in self.config.get('recordings',{}).items()],
            'maximum_pause_seconds':self.config.get('maximum_pause_seconds',600),
            'maximum_duration_seconds':1800,'images_over_network':True,
            'crop_delivery':'committed stereo PNGs on demand, terminal jobs only; no RAW download',
            'fastcam_capture_supported':True,
            'manual_fastcam_supported':True,
            'configuration':configuration(self.config)}
    def start(self,key,request):
        try:uuid.UUID(key)
        except (ValueError,TypeError):raise ValueError('Job ID must be a UUID')
        # Reconnect/retry with the identical manifest never starts a second run.
        with self.lock:
            try:existing=self.store.job(key)
            except KeyError:existing=None
            if existing:
                if fingerprint(existing['request'])!=fingerprint(request):raise ValueError('Job already has a different manifest')
                return existing
            if request.get('machine_id')!=self.config['machine_id']:raise ValueError('Machine identity differs from the prepared job')
            if request.get('recipe')!='all_pass_through':raise ValueError('Only All Pass-through is supported')
            if request.get('image_mode') not in {'raw','stereo','none'}:raise ValueError('Choose an image mode')
            if request.get('source') not in {'live',*self.config.get('recordings',{})}:raise ValueError('Unknown source')
            duration=request.get('duration_seconds')
            if type(duration) not in (int,float) or not math.isfinite(duration) or not 1<=duration<=1800:raise ValueError('Duration must be 1–1800 active seconds')
            catalog=self.catalog();allowed=next((b for b in catalog['settings'] if b['id']==request.get('settings_sha256')),None)
            if allowed is None:raise ValueError('Settings bundle is no longer available')
            if request.get('configuration')!=catalog['configuration']:raise ValueError('Calibration or nozzle map changed since preparation')
            if request.get('settings_sha256')!=allowed['id'] or fingerprint(request.get('settings'))!=allowed['id']:
                raise ValueError('Settings changed since preparation; prepare a new job')
            validate_fastcam(request.get('fastcam'),duration,request['source'],request['settings'].get('cameras'))
            if self.store.create(key,request):
                runner=Runner(self.store,key,request,self.config);self.runners[key]=runner;runner.thread.start()
            return self.store.job(key)
    def command(self,key,command_id,action):
        if not isinstance(command_id,str) or not 1<=len(command_id)<=200:raise ValueError('Command identity required')
        if action not in {'pause','resume','end','stop'}:raise ValueError('Unknown action')
        with self.lock:
            status=self.store.job(key)
            if status['state'] in {'completed','failed'}:return status
            runner=self.runners.get(key)
            if runner is None:raise ValueError('Run has no executor')
            return self.store.command(key,command_id,action,lambda:runner.command(action))
    def close(self):
        for runner in self.runners.values():
            if runner.thread.is_alive():runner.command('end')
        for runner in self.runners.values():runner.thread.join(45)
    def manual_camera(self,key):
        job=self.store.job(key)
        if job['request'].get('source')!='live':raise ValueError('Manual camera controls require a live source')
        runner=self.runners.get(key)
        return job,getattr(runner,'manual_fastcam',None)
    def camera_status(self,key):
        job,camera=self.manual_camera(key)
        if camera:return camera.read_status()
        return {**(job.get('manual_fastcam') or {}),'controls_available':False,'connected':False,
            'reason':'Camera controls are available while this live job is running or paused'}
    def camera_command(self,key,command_id,data):
        validate_manual_fastcam(data)
        if not isinstance(command_id,str) or not 1<=len(command_id)<=160:raise ValueError('Camera command identity required')
        with self.lock:
            _,camera=self.manual_camera(key)
            if camera is None:raise ValueError('This live job has no active camera operator')
            camera.require_active()
            return self.store.command(key,'manual-fastcam:'+command_id,'manual-fastcam:'+fingerprint(data),
                lambda:camera.submit(command_id,data),validate=lambda:camera.check_submission(data))
    def camera_frame(self,key):
        _,camera=self.manual_camera(key)
        if camera is None:raise ValueError('Camera preview requires an active live job')
        camera.require_active()
        return camera.client.frame()


def make_handler(manager,token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):self.handle_request(False)
        def do_POST(self):self.handle_request(True)
        def handle_request(self,post):
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):
                return self.respond(401,{'error':'Authentication required'})
            try:
                u=urlsplit(self.path);parts=u.path.strip('/').split('/');data={}
                if post:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=65536:raise ValueError('Invalid request size')
                    data=json.loads(self.rfile.read(length))
                if parts==['v1','catalog'] and not post:result=manager.catalog()
                elif parts==['v1','fastcam-defaults'] and not post:
                    result=fastcam_defaults(manager.config['controller_url'])
                elif len(parts)>=3 and parts[:2]==['v1','jobs']:
                    key=parts[2]
                    if len(parts)==3:
                        result=manager.start(key,data) if post else manager.store.job(key)
                    elif len(parts)==4 and parts[3]=='events' and not post:
                        q=parse_qs(u.query);after=int(q.get('after',['0'])[0]);limit=int(q.get('limit',['100'])[0])
                        result={'events':manager.store.events(key,after,limit),'status':manager.store.job(key)}
                    elif len(parts)==6 and parts[3]=='crops' and not post:
                        body=read_crop(manager.store,manager.root,key,int(parts[4]),parts[5])
                        self.send_response(200)
                        self.send_header('Content-Type','image/png')
                        self.send_header('Content-Length',str(len(body)))
                        self.send_header('Cache-Control','private, max-age=300')
                        self.send_header('X-Content-Type-Options','nosniff')
                        self.end_headers();self.wfile.write(body)
                        return
                    elif len(parts)==4 and parts[3]=='commands' and post:result=manager.command(key,data.get('id'),data.get('action'))
                    elif len(parts)==5 and parts[3]=='fastcam':
                        if parts[4]=='status' and not post:result=manager.camera_status(key)
                        elif parts[4]=='commands' and post:
                            if not isinstance(data,dict) or set(data)-{'id','action','settings','operator'}:raise ValueError('Unknown camera command fields')
                            result=manager.camera_command(key,data.get('id'),{k:v for k,v in data.items() if k!='id'})
                        elif parts[4]=='frame' and not post:
                            body=manager.camera_frame(key)
                            self.send_response(200);self.send_header('Content-Type','image/jpeg')
                            self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
                            self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(body);return
                        else:raise KeyError('Unknown camera endpoint')
                    else:raise KeyError('Unknown endpoint')
                else:raise KeyError('Unknown endpoint')
                self.respond(200,result)
            except (ValueError,TypeError) as exc:self.respond(409,{'error':str(exc)})
            except KeyError as exc:self.respond(404,{'error':str(exc)})
            except Exception:self.respond(500,{'error':'Machine job service failed; inspect its local log'})
        def respond(self,code,data):
            body=encode(data).encode();self.send_response(code)
            self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    return Handler


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True)
    a=p.parse_args();config=json.loads(a.config.read_text());token=Path(config['token_file']).read_text().strip()
    if len(token)<32:raise ValueError('Machine API token must contain at least 32 characters')
    manager=Manager(config);server=ThreadingHTTPServer((config.get('host','0.0.0.0'),config.get('port',8091)),make_handler(manager,token))
    def stop(*_):threading.Thread(target=server.shutdown,daemon=True).start()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    try:server.serve_forever()
    finally:manager.close();server.server_close()
if __name__=='__main__':main()
