"""Link recorded crop events to beans; proxy only their authenticated PNG reads."""
from sqlalchemy import select

from .models import MachineEvent,MachineJob
from .quantities import DomainError

CAMERAS=('CamL','CamR')


def reference(event):
    if not isinstance(event,dict) or event.get('kind')!='images':return None
    data=event.get('data',{})
    if not isinstance(data,dict) or not isinstance(data.get('paths'),list):return None
    bean=data.get('bean_sequence');frame=data.get('frame_index')
    if type(bean) is not int or bean<0 or type(frame) is not int or frame<0:return None
    cameras=[cam for cam in CAMERAS if f'stereo/{bean}/{frame:08d}-{cam}.png' in data.get('paths',[])]
    return {'bean_sequence':bean,'frame_index':frame,'cameras':cameras} if cameras else None


def references(session,job,sequences):
    if job.request.get('image_mode')!='stereo' or job.state not in {'completed','failed'} or not sequences:return {}
    beans=list({seq for seq in sequences if type(seq) is int})
    def records():
        for start in range(0,len(beans),250):
            yield from session.execute(select(MachineEvent.sequence,MachineEvent.data).where(
                MachineEvent.operation_id==job.operation_id,MachineEvent.data['kind'].as_string()=='images',
                MachineEvent.data['data']['bean_sequence'].as_integer().in_(beans[start:start+250])))
    result={}
    for sequence,event in records():
        ref=reference(event)
        if not ref:continue
        bean=ref['bean_sequence'];old=result.get(bean)
        if old and (old['frame_index'],old['event_sequence'])<=(ref['frame_index'],sequence):continue
        result[bean]={**ref,'event_sequence':sequence,'photos':{cam:
            f'/jobs/{job.operation_id}/measurements/photos/{sequence}/{cam}' if cam in ref['cameras'] else None
            for cam in CAMERAS}}
    return result


def register_routes(app,db,machine,user_for):
    from fastapi import HTTPException,Request
    from fastapi.responses import Response

    @app.get('/jobs/{key}/measurements/photos/{sequence}/{camera}')
    def photograph(request:Request,key:str,sequence:int,camera:str):
        user_for(request)
        if not 1<=sequence<=2**63-1 or camera not in CAMERAS:raise HTTPException(404,'Unknown saved photograph')
        with db.read() as session:
            job=session.get(MachineJob,key);event=session.get(MachineEvent,(key,sequence))
            ref=reference(event.data) if event else None
            if not job or job.request.get('image_mode')!='stereo' or not ref or camera not in ref['cameras']:
                raise HTTPException(404,'No saved photograph for this run and camera')
            if job.state not in {'completed','failed'}:raise HTTPException(409,'Photographs load after the machine run finishes')
        # Release the DB read transaction before potentially slow machine IO.
        try:body=machine.transport.crop(job.endpoint,job.remote_id,sequence,camera)
        except DomainError as exc:raise HTTPException(503,str(exc)) from None
        return Response(body,media_type='image/png',headers={'Cache-Control':'private, max-age=300',
                         'X-Content-Type-Options':'nosniff'})
