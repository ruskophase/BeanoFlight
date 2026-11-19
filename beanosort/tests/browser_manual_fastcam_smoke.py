"""Real templates/JS, synthetic camera API only. No operational machine calls."""
import base64
from copy import deepcopy
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/f'.venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'))
from jinja2 import Environment,FileSystemLoader
from markupsafe import Markup


def main():
    artifacts=ROOT/'var/qa/manual-fastcam';artifacts.mkdir(parents=True,exist_ok=True)
    templates=Environment(loader=FileSystemLoader(ROOT/'src/beanosort/templates'),autoescape=True)
    context={'op':SimpleNamespace(id='live-job'),'job':None,'csrf':'synthetic-csrf','newkey':lambda:'test-page-prefix',
        'tokens':lambda csrf:Markup('<input type="hidden" name="csrf" value="synthetic-csrf">'),
        'user':SimpleNamespace(username='Test operator',role='operator'),'environment':'simulation','timezone':'Africa/Johannesburg'}
    manual=templates.get_template('manual_fastcam.html').render(**context)
    job=templates.get_template('machine_job.html').render(**context,machine_job={'state':'running',
        'request':{'source':'live','settings':{'name':'Test'},'duration_seconds':60,'image_mode':'none','fastcam':None}})
    job='<!doctype html><meta charset="utf-8">'+job
    c={'state':'stopped','settings':{'width':1280,'fps':180,'exposure_ms':5,'preview_fps':15,'device':None,
        'output_folder':'/offline-test/recordings','external_trigger':True,'disable_preview':False,'disable_preview_during_recording':True},
        'camera_running':False,'camera_ready':False,'recording':False,'armed':False,'finishing':False,'preview_disabled':False,
        'capture_fps':180,'stream_fps':180,'recorded_frames':0,'playback_fps':180}
    state={'manual':False,'busy':False,'controls_available':True,'connected':True,'camera':c,'last_received_at':time.time(),
        'high_speed':{'available':True,'enabled':False,'frequency_hz':178.5}}
    actions=[];errors=[];job_polls=[];frame_reads=[];offline=[False]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True);browser_context=browser.new_context(viewport={'width':1440,'height':1100})
        scratch=browser_context.new_page()
        jpeg=base64.b64decode(scratch.evaluate("""()=>{const c=document.createElement('canvas');c.width=c.height=4;const x=c.getContext('2d');x.fillStyle='#779955';x.fillRect(0,0,4,4);return c.toDataURL('image/jpeg').split(',')[1];}"""))
        scratch.close()
        def route(r):
            request=r.request;path=request.url.split('http://manual-camera.test')[-1]
            if path.startswith('/static/'):
                file=ROOT/'src/beanosort/static'/path.split('/')[-1]
                return r.fulfill(path=str(file),content_type='text/css' if file.suffix=='.css' else 'application/javascript')
            if path=='/jobs/live-job':return r.fulfill(body=job,content_type='text/html')
            if path=='/jobs/live-job/fastcam':return r.fulfill(body=manual,content_type='text/html')
            if path.endswith('/machine-status'):
                job_polls.append(time.time())
                return r.fulfill(json={'state':'running','pending_commands':[],'cursor':1,
                    'status':{'metrics':{},'active_seconds':len(job_polls),'manual_fastcam':state}})
            if path.endswith('/status'):
                if offline[0]:return r.fulfill(status=503,json={'error':'Synthetic connection loss'})
                state['last_received_at']=time.time();return r.fulfill(json=state)
            if path.endswith('/frame'):
                frame_reads.append(time.time());return r.fulfill(body=jpeg,content_type='image/jpeg')
            if path.endswith('/commands'):
                msg=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+request.headers['content-type']+'\r\n\r\n').encode()+request.post_data_buffer)
                fields={part.get_param('name',header='content-disposition'):part.get_payload(decode=True).decode() for part in msg.iter_parts()}
                assert fields['csrf']=='synthetic-csrf'
                payload=json.loads(fields['payload']);actions.append(payload)
                a=payload['action'];state['last_command']={'id':fields['key'],**payload,'state':'acknowledged'}
                if a=='take_manual':state['manual']=True
                elif a=='settings':c['settings'].update(payload['settings'])
                elif a=='start':c.update(camera_running=True,camera_ready=True,state='preview');state['high_speed']['enabled']=True
                elif a=='record':c.update(recording=True,armed=True,state='armed')
                elif a=='stop_recording':c.update(recording=False,armed=False,state='preview',recorded_frames=5400,video_path='/offline-test/video.avi')
                elif a=='stop_camera':c.update(camera_running=False,camera_ready=False,recording=False,armed=False,state='stopped');state['high_speed']['enabled']=False
                else:raise AssertionError(a)
                c['preview_disabled']=c['settings']['disable_preview'] or c['recording'] and c['settings']['disable_preview_during_recording']
                return r.fulfill(json={'accepted':True})
            raise AssertionError('Unexpected browser request: '+request.url)
        browser_context.route('http://manual-camera.test/**',route)
        parent=browser_context.new_page();parent.on('pageerror',lambda e:errors.append(str(e)));parent.goto('http://manual-camera.test/jobs/live-job')
        with browser_context.expect_page() as opened:parent.get_by_role('link',name='Manual high-speed camera ↗').click()
        page=opened.value;page.on('pageerror',lambda e:errors.append(str(e)));page.wait_for_load_state();page.bring_to_front()
        expect(page.get_by_role('button',name='Use manual camera controls',exact=True)).to_be_enabled()
        assert actions==[]
        page.get_by_role('button',name='Use manual camera controls',exact=True).click()
        exposure=page.get_by_label('Exposure (µs)',exact=True)
        expect(exposure).to_be_enabled();exposure.fill('6200');page.wait_for_timeout(2300)
        expect(exposure).to_have_value('6200')  # Polling in both tabs must not replace typed values.
        assert len(job_polls)>=2
        page.get_by_role('button',name='Apply camera settings',exact=True).click()
        expect(page.locator('[data-camera-unsaved]')).to_have_text('')
        assert actions[-1]['settings']=={'exposure_ms':6.2}
        page.get_by_role('button',name='Start camera',exact=True).click()
        expect(page.get_by_role('button',name='Record (arm)',exact=True)).to_be_enabled()
        page.wait_for_function("document.querySelector('[data-camera-preview]').naturalWidth===4")
        page.get_by_role('button',name='Record (arm)',exact=True).click()
        expect(page.get_by_role('button',name='Stop recording',exact=True)).to_be_enabled()
        page.get_by_label('Disable camera preview during recording',exact=True).uncheck()
        page.get_by_role('button',name='Apply camera settings',exact=True).click()
        expect(page.locator('[data-camera-unsaved]')).to_have_text('')
        assert c['recording'] and actions[-1]['settings']=={'disable_preview_during_recording':False}
        for width,height in ((1440,1100),(768,1024),(390,844)):
            page.set_viewport_size({'width':width,'height':height})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
            page.screenshot(path=str(artifacts/f'manual-{width}.png'),full_page=True)
        page.get_by_role('button',name='Stop recording',exact=True).click()
        expect(page.locator('[data-camera-files]')).to_contain_text('/offline-test/video.avi')
        page.get_by_role('button',name='Stop camera',exact=True).click()
        expect(page.get_by_role('button',name='Start camera',exact=True)).to_be_enabled()
        exposure.fill('7100');offline[0]=True
        expect(page.locator('[data-camera-state]')).to_contain_text('disconnected')
        expect(page.get_by_role('button',name='Start camera',exact=True)).to_be_disabled()
        offline[0]=False;expect(page.get_by_role('button',name='Use manual camera controls',exact=True)).to_be_hidden()
        expect(page.locator('[data-camera-state]')).to_contain_text('Manual camera mode')
        expect(exposure).to_have_value('7100')
        before=len(actions);page.close();parent.wait_for_timeout(500);assert len(actions)==before
        assert frame_reads and not errors,errors
        assert all(a['action'] in {'take_manual','settings','start','record','stop_recording','stop_camera'} for a in actions)
        browser.close()
    (artifacts/'browser-result.json').write_text(json.dumps({'passed':True,'synthetic_only':True,'actions':actions,
        'parent_job_polls':len(job_polls),'preview_reads':len(frame_reads),'unsaved_edits_survive_polling_and_reconnect':True,
        'widths':[1440,768,390],'javascript_errors':errors,'physical_hardware_commands':0},indent=2)+'\n')
    print('Manual FastCamUSB browser checks passed; all camera actions used synthetic HTTP routes')


if __name__=='__main__':main()
