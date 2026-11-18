"""LAN job adapter; numerical ingestion and on-demand saved crops are independent."""
import json
import logging
from pathlib import Path
import threading
import uuid
from urllib.error import HTTPError
from urllib.request import Request,build_opener,ProxyHandler
from sqlalchemy import select
from .auth import require_role
from .capture import digest,FEATURE_SCHEMA
from .models import (MachineJob,MachineEvent,MachineCommand,Version,Operation,Run,Station,User,identity,now)
from .quantities import DomainError
from .service import get,audit
from .sorter_controls import connection_for,Connection,NoRedirect

RECIPE={'schema':'beanosort-machine-recipe/v1','operation_mode':'all_pass_through','relation':'pass_all',
        'decision':'All observed beans pass through; no valve firing; measurements are observational'}


def install_recipe(db):
    with db.write() as s:
        if not s.scalar(select(Station).filter_by(name='Live sorter')):
            s.add(Station(id=identity('station'),name='Live sorter',kind='machine',
                          capabilities={'execution_methods':['manual','simulated']}))
        row=s.scalar(select(Version).filter_by(kind='recipe',name='All Pass-through · live machine'))
        if row:return row.id
        operator=s.scalar(select(User.id).where(User.active==True,User.role=='admin').order_by(User.created_at))
        if not operator:raise DomainError('Initialize an administrator before enabling machine jobs')
        row=Version(id=identity('version'),kind='recipe',name='All Pass-through · live machine',version=1,data=RECIPE,operator_id=operator)
        s.add(row);return row.id


class Transport:
    def __init__(self,token_file):self.token_file=token_file
    def call(self,endpoint,path,body=None):
        if not self.token_file:raise DomainError('Configure the machine job API token file on the BeanoSort server')
        token=Path(self.token_file).read_text().strip()
        request=Request(endpoint+path,None if body is None else json.dumps(body).encode(),
                        {'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        try:
            with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=3) as r:
                payload=r.read(4*1024*1024+1)
                if len(payload)>4*1024*1024:raise DomainError('Machine response exceeded the numerical page limit')
                return json.loads(payload)
        except HTTPError as exc:
            try:message=json.loads(exc.read(4096)).get('error','Machine request failed')
            except Exception:message='Machine request failed'
            raise DomainError(f'Machine: {message}') from None
        except OSError as exc:raise DomainError('Machine job service is unreachable; commands and evidence will retry') from exc

    def crop(self,endpoint,run_id,sequence,camera):
        """One bounded read, only on an operator's photo request; never a command."""
        limit=4*1024*1024
        try:
            if not self.token_file:raise DomainError('Machine photograph access is not configured')
            token=Path(self.token_file).read_text().strip()
            request=Request(f'{endpoint}/v1/jobs/{run_id}/crops/{sequence}/{camera}',
                            headers={'Authorization':'Bearer '+token,'Accept':'image/png'})
            with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=5) as response:
                body=response.read(limit+1)
                if response.headers.get_content_type()!='image/png' or len(body)>limit or not body.startswith(b'\x89PNG\r\n\x1a\n'):
                    raise DomainError('The machine did not return a supported saved photograph')
                return body
        except OSError:raise DomainError('Saved photograph unavailable from the machine') from None


class Integration:
    def __init__(self,db,workflow,settings):
        self.db=db;self.workflow=workflow;self.settings=settings
        self.transport=Transport(settings.machine_jobs_token_file)
        self.stop=threading.Event();self.lock=threading.Lock();self.thread=None
    @property
    def enabled(self):return bool(self.settings.machine_jobs_token_file)
    def endpoint(self):
        with self.db.read() as s:c=connection_for(s,self.settings)
        return Connection(c.id,c.host,self.settings.machine_jobs_port).url
    def catalog(self):return self.transport.call(self.endpoint(),'/v1/catalog')
    def prepare_manifest(self,data):
        catalog=self.catalog();endpoint=self.endpoint()
        selected=next((x for x in catalog['settings'] if x['id']==data.get('machine_settings')),None)
        if not selected:raise DomainError('Choose a current machine settings bundle')
        source=data.get('machine_source','live')
        if source not in {x['id'] for x in catalog['sources']}:raise DomainError('Unknown capture source')
        if source!='live' and self.db.environment!='simulation':raise DomainError('Replay requires isolated simulation inventory')
        mode=data.get('image_mode','none')
        if mode not in {'raw','stereo','none'}:raise DomainError('Choose an image saving option')
        try:duration=int(data.get('duration_seconds',''))
        except ValueError:raise DomainError('Enter a whole number of active run seconds') from None
        if not 1<=duration<=catalog['maximum_duration_seconds']:raise DomainError('Duration must be 1–1800 seconds')
        return {'endpoint':endpoint,'remote_id':str(uuid.uuid5(uuid.NAMESPACE_URL,'beanosort:'+data['key'])),
            'request':{'machine_id':catalog['machine_id'],'recipe':'all_pass_through','source':source,'paced':True,
            'duration_seconds':duration,'image_mode':mode,'settings':selected['values'],'settings_sha256':selected['id'],
            'configuration':catalog.get('configuration')}}
    def enqueue(self,user_id,operation_id,key,action):
        if action not in {'start','pause','resume','end','stop'}:raise DomainError('Unknown machine command')
        with self.db.write() as s:
            user=get(s,User,user_id);require_role(user,'operator','supervisor','admin')
            job=get(s,MachineJob,operation_id);op=get(s,Operation,operation_id)
            if action=='start' and op.status!='prepared':raise DomainError('Only prepared jobs can start')
            old=s.get(MachineCommand,key)
            if old:
                if old.operation_id!=operation_id or old.action!=action:raise DomainError('Command identity has already been used')
                return
            allowed={'start':{'prepared'},'pause':{'running','starting'},'resume':{'paused'},
                     'end':{'starting','running','paused','draining'},'stop':{'starting','running','paused','draining'}}
            if job.state not in allowed[action]:raise DomainError(f'Cannot {action} a {job.state} machine job')
            if action=='resume':
                from .collections import check_destination_contents
                check_destination_contents(s,op,start=True)
                from .models import Input,Portion
                for link in s.scalars(select(Input).filter_by(operation_id=op.id)):
                    if get(s,Portion,link.portion_id).hold_reason:raise DomainError('Source has a quality hold')
                from .sort_capacity import acknowledge
                acknowledge(s,user,op,self.db.environment)
            if action=='start':job.state='starting'
            s.add(MachineCommand(id=key,operation_id=operation_id,action=action,acknowledged=False))
            audit(s,user,'machine_command_requested',{'action':action,'remote_id':job.remote_id},op)
    def launch(self):
        if not self.enabled:return
        install_recipe(self.db)
        self.thread=threading.Thread(target=self._work,name='machine-journal-sync',daemon=True);self.thread.start()
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(5)
    def _work(self):
        while not self.stop.is_set():
            try:self.tick()
            except Exception:logging.exception('Machine journal synchronization failed')
            self.stop.wait(.5)
    def tick(self):
        if not self.lock.acquire(False):return
        try:
            with self.db.read() as s:
                jobs=list(s.scalars(select(MachineJob).where(MachineJob.state.notin_(['prepared','completed','failed','cancelled']))))
            for job in jobs:
                try:self.sync(job)
                except Exception as exc:
                    with self.db.write() as s:
                        current=s.get(MachineJob,job.operation_id)
                        current.status={**current.status,'connection_error':str(exc)}
        finally:self.lock.release()
    def sync(self,job):
        with self.db.read() as s:
            op=get(s,Operation,job.operation_id)
            commands=list(s.scalars(select(MachineCommand).where(MachineCommand.operation_id==op.id,
                          MachineCommand.acknowledged==False).order_by(MachineCommand.created_at,MachineCommand.id)))
        path='/v1/jobs/'+job.remote_id
        for command in commands:
            if command.action=='start':
                self.workflow.start(op.operator_id,op.id,source_run_id=job.remote_id)
                self.transport.call(job.endpoint,path,job.request)
                with self.db.read() as s:ready=get(s,Operation,op.id).status=='running'
                if ready:
                    self.transport.call(job.endpoint,path+'/commands',{'id':command.id+':feed','action':'resume'})
            else:self.transport.call(job.endpoint,path+'/commands',{'id':command.id,'action':command.action})
            with self.db.write() as s:s.get(MachineCommand,command.id).acknowledged=True
        result=self.transport.call(job.endpoint,path+'/events?after='+str(job.cursor)+'&limit=100')
        events=result['events'];status=result['status'];expected=job.cursor+1
        for event in events:
            if event['sequence']!=expected:raise DomainError('Numerical journal gap; reconciliation is blocked until it is recovered')
            expected+=1
        with self.db.read() as s:run=s.scalar(select(Run).filter_by(operation_id=op.id))
        beans=[compact(e['data'],job.remote_id,job.request['source']) for e in events if e['kind']=='bean']
        if beans:
            self.workflow.ingest(op.operator_id,run.id,'machine:'+str(job.cursor+1)+':'+str(expected-1),beans)
        # Ingest is itself duplicate-safe. A crash between ingest and cursor commit
        # replays the same immutable batch, never duplicates a particle.
        with self.db.write() as s:
            current=get(s,MachineJob,op.id);operation=get(s,Operation,op.id)
            if current.cursor!=job.cursor:return
            for event in events:
                key=(op.id,event['sequence']);old=s.get(MachineEvent,key);checksum=digest(event)
                if old and old.digest!=checksum:raise DomainError('Machine journal event changed after receipt')
                if not old:s.add(MachineEvent(operation_id=op.id,sequence=event['sequence'],digest=checksum,data=event))
            current.cursor=expected-1
            current.status={**status,'last_received_at':now(),'connection_error':None}
            final=status.get('final_sequence');finished=status['state'] in {'completed','failed'}
            current.state=status['state'] if not finished or (final is not None and current.cursor>=final) else 'draining'
            previous=operation.status
            if current.state=='completed':operation.status='finishing'
            elif current.state=='failed':operation.status='failed'
            elif current.state=='paused':operation.status='paused'
            elif current.state in {'running','draining'}:operation.status='running'  # cannot weigh during drain
            get(s,Station,op.station_id).last_received_at=now()
            if operation.status!=previous:audit(s,get(s,User,op.operator_id),'machine_state_confirmed',
                {'state':current.state,'journal_cursor':current.cursor,'final_sequence':final},operation)


def compact(bean,run_id,source):
    v=bean['features']
    def avg(field):
        values=[v.get(c+'_'+field) for c in ('caml','camr')];values=[x for x in values if x is not None]
        return sum(values)/len(values) if values else None
    provenance={'source':'machine-job:'+run_id,'version':'all-pass-v1'}
    return {'schema':FEATURE_SCHEMA,'key':run_id+':'+str(bean['bean_sequence']),'sequence':bean['ordinal'],
        'source_at':None,'decision':'pass','action':'not_issued','features':{
            'shape':None,'defects':None,'brightness':None,'lab_l':avg('lab_l_mean'),
            'lab_chroma':avg('lab_chroma_mean'),'size_mm':avg('ellipse_major_mm'),
            'area_mm2':v.get('projected_area_geomean_mm2'),
            'volume_proxy_mm3':v.get('rotational_ellipsoid_volume_proxy_mm3')},
        'evidence':{'features':{'kind':'image_derived',**provenance},
                    'decision':{'kind':'model_derived',**provenance},'action':{'kind':'controller_record',**provenance}},
        'machine_evidence':{**bean,'source':source,'physical_passage':source=='live',
            'destination_evidence':'intended Pass-through; physical arrival not independently measured'}}


def register_routes(app,db,machine,user_for,form):
    from fastapi import Request
    from fastapi.responses import JSONResponse,RedirectResponse
    from starlette.concurrency import run_in_threadpool
    @app.get('/jobs/{key}/machine-status')
    def status(request:Request,key:str):
        user_for(request)
        with db.read() as s:
            job=get(s,MachineJob,key);op=get(s,Operation,key)
            pending=list(s.scalars(select(MachineCommand.action).where(MachineCommand.operation_id==key,MachineCommand.acknowledged==False)))
            return JSONResponse({'state':job.state,'cursor':job.cursor,'status':job.status,'pending_commands':pending,
                'weigh_url':'/jobs/'+key+'/weigh' if op.status=='finishing' and job.state=='completed' else None},
                headers={'Cache-Control':'no-store'})
    @app.post('/jobs/{key}/machine/{action}')
    async def command(request:Request,key:str,action:str):
        user=user_for(request);data=await form(request)
        if action=='review-failure':
            with db.write() as s:
                require_role(user,'operator','supervisor','admin');job=get(s,MachineJob,key);op=get(s,Operation,key)
                if job.state!='failed' or job.status.get('final_sequence') is None or job.cursor<job.status['final_sequence']:
                    raise DomainError('Recover the final numerical journal before reviewing interrupted work')
                if data.get('accept_incomplete')!='on':raise DomainError('Acknowledge that the interrupted run may have incomplete measurements')
                op.status='finishing';audit(s,user,'machine_failure_reviewed',{'error':job.status.get('error'),'cursor':job.cursor},op)
            return RedirectResponse('/jobs/'+key+'/weigh',status_code=303)
        await run_in_threadpool(machine.enqueue,user.id,key,data['key'],action)
        return RedirectResponse('/jobs/'+key,status_code=303)
