"""Authenticated operator tab. Delegates camera-only actions to the owning live job."""
import json
from fastapi import Request
from fastapi.responses import JSONResponse,Response
from starlette.concurrency import run_in_threadpool
from .auth import require_role
from .models import MachineJob,Operation
from .quantities import DomainError
from .service import get


def register_routes(app,db,machine,user_for,render,form):
    def job_for(key):
        with db.read() as s:
            job=get(s,MachineJob,key)
            if job.request.get('source')!='live':raise DomainError('Manual high-speed camera controls require a live sorting job')
            return job

    @app.get('/jobs/{key}/fastcam')
    def camera_page(request:Request,key:str):
        user=user_for(request);require_role(user,'operator','supervisor','admin')
        job=job_for(key)
        with db.read() as s:
            return render(request,'manual_fastcam.html',user=user,db_session=s,op=get(s,Operation,key),job=job)

    @app.get('/jobs/{key}/fastcam/status')
    def camera_status(request:Request,key:str):
        user=user_for(request);require_role(user,'operator','supervisor','admin');job=job_for(key)
        try:result=machine.transport.call(job.endpoint,f'/v1/jobs/{job.remote_id}/fastcam/status')
        except DomainError as exc:return JSONResponse({'error':str(exc)},status_code=503,headers={'Cache-Control':'no-store'})
        return JSONResponse(result,headers={'Cache-Control':'no-store'})

    @app.get('/jobs/{key}/fastcam/frame')
    def camera_frame(request:Request,key:str):
        user=user_for(request);require_role(user,'operator','supervisor','admin');job=job_for(key)
        return Response(machine.transport.preview(job.endpoint,job.remote_id),media_type='image/jpeg',
            headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

    @app.post('/jobs/{key}/fastcam/commands')
    async def camera_command(request:Request,key:str):
        user=user_for(request);require_role(user,'operator','supervisor','admin');data=await form(request);job=job_for(key)
        try:payload=json.loads(data.get('payload',''))
        except (ValueError,TypeError):raise DomainError('Invalid camera command') from None
        if not isinstance(payload,dict) or set(payload)-{'action','settings'}:raise DomainError('Invalid camera command fields')
        if not isinstance(data.get('key'),str) or not 1<=len(data['key'])<=160:raise DomainError('Camera command identity required')
        # No outbox: replaying uncertain camera activation later would be misleading.
        try:
            result=await run_in_threadpool(machine.transport.call,job.endpoint,f'/v1/jobs/{job.remote_id}/fastcam/commands',
                {**payload,'id':data['key'],'operator':user.id})
        except DomainError as exc:return JSONResponse({'error':str(exc)},status_code=409,headers={'Cache-Control':'no-store'})
        return JSONResponse(result,headers={'Cache-Control':'no-store'})
