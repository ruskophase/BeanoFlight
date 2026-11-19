import json
from sqlalchemy import func,select
from beanosort.models import Ledger,MachineCommand,MachineJob,User
from test_machine_jobs import prepared
from test_web import client_for,token


def test_manual_camera_page_link_auth_csrf_and_delegated_operator_requests(system):
    op=prepared(system);client=client_for(system);machine=client.app.state.machine_jobs;calls=[]
    class Camera:
        def call(self,endpoint,path,body=None):
            calls.append((endpoint,path,body))
            return {'connected':True,'controls_available':True,'camera':{'state':'stopped'}} if body is None else {'accepted':True}
        def preview(self,endpoint,run):calls.append((endpoint,run,'JPEG preview'));return b'\xff\xd8test-jpeg'
    machine.transport=Camera()
    job=client.get('/jobs/'+op)
    assert f'href="/jobs/{op}/fastcam" target="_blank" rel="noopener"' in job.text
    page=client.get('/jobs/'+op+'/fastcam')
    assert page.status_code==200 and 'Use manual camera controls' in page.text
    assert 'manual_fastcam.js' in page.text and calls==[]  # Page itself never queries/starts hardware.
    csrf=token(page)
    status=client.get('/jobs/'+op+'/fastcam/status')
    assert status.status_code==200 and status.headers['cache-control']=='no-store'
    assert calls[-1][2] is None
    assert client.post('/jobs/'+op+'/fastcam/commands',data={'payload':'{}'}).status_code==403
    payload={'action':'take_manual'}
    result=client.post('/jobs/'+op+'/fastcam/commands',data={'csrf':csrf,'key':'once-only','payload':json.dumps(payload)})
    assert result.status_code==200
    body=calls[-1][2]
    assert body['action']=='take_manual' and body['id']=='once-only' and body['operator']==system.uid
    assert 'client_id' not in body
    assert client.post('/jobs/'+op+'/fastcam/commands',data={'csrf':csrf,'key':'bad','payload':json.dumps({'action':'start','operator':'forged'})}).status_code==409
    image=client.get('/jobs/'+op+'/fastcam/frame')
    assert image.status_code==200 and image.headers['content-type']=='image/jpeg'
    with system.db.read() as s:
        assert s.scalar(select(func.count(MachineCommand.id)))==0  # No deferred activation/retry outbox.
        user=s.get(User,system.uid);assert user.username=='admin'
    client.close()


def test_replay_and_inactive_user_cannot_control_camera(system):
    op=prepared(system,source='recording');client=client_for(system)
    assert client.get('/jobs/'+op+'/fastcam').status_code==409
    assert '/fastcam" target="_blank"' not in client.get('/jobs/'+op).text
    live=prepared(system)
    with system.db.write() as s:s.get(User,system.uid).active=False
    for path in ('','/status','/frame'):
        response=client.get('/jobs/'+live+'/fastcam'+path,follow_redirects=False)
        assert response.status_code==303 and response.headers['location']=='/login'
    client.close()
