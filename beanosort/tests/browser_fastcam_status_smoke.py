"""Render recorder telemetry in a real browser using synthetic HTTP status only."""
import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright,expect


def main():
    root=Path(__file__).resolve().parents[1]
    artifacts=root/'var/qa/fastcam-storage-review-20261119'
    artifacts.mkdir(parents=True,exist_ok=True)
    attrs=['state','time','error','pending','coverage','images','fastcam','fastcam-files','fastcam-diagnostics']
    html='<h1>Job</h1><section id="machine-job" data-status-url="/status">'+''.join(
        '<p data-machine-'+a+'></p>' for a in attrs)+'<progress data-machine-progress></progress><p data-machine-change></p></section>'
    status={'state':'running','status':{'metrics':{},'fastcam':{'state':'recording','start_acknowledged':True,
        'last_status_received_at':time.time(),'camera_report':{'recorded_frames':1000,'video_path':'/example.avi',
        'recorder':{'policy':{'frame_limit':900,'byte_limit':67108864,'stall_threshold_ms':100},
        'buffered_frames':234,'buffered_bytes':5000000,'peak_buffered_frames':240,'peak_buffered_bytes':5300000,
        'max_write_ms':1300,'stall_count':1,'rejected_frames':0,'abandoned_frames':0,
        'diagnostics_path':'/example.diagnostics.json'}}}},'pending_commands':[],'cursor':1}
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1300,'height':800});errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('http://recorder.test/',lambda r:r.fulfill(content_type='text/html',body=html))
        page.route('http://recorder.test/status',lambda r:r.fulfill(json=status))
        page.goto('http://recorder.test/')
        page.add_script_tag(path=str(root/'src/beanosort/static/machine_job.js'))
        details=page.locator('[data-machine-fastcam-diagnostics]')
        expect(details).to_contain_text('234 / 900 frames')
        expect(details).to_contain_text('1300.0 ms')
        expect(page.locator('[data-machine-fastcam-files]')).to_contain_text('/example.diagnostics.json')
        f=status['status']['fastcam'];f.update(recording_error='Synthetic overflow',failure_observed_elapsed_seconds=25)
        f['camera_report']['recorder']['failure_at']='2026-11-19T04:45:04+00:00'
        expect(page.locator('[data-machine-fastcam]')).to_contain_text('Synthetic overflow')
        expect(details).to_contain_text('Pi recorder failure time (UTC)')
        expect(details).to_contain_text('job +25.0 s')
        f['monitor_error']='Synthetic network timeout';f['last_status_received_at']=time.time()-10
        expect(page.locator('[data-machine-fastcam]')).to_contain_text('current recording state is unknown')
        page.screenshot(path=str(artifacts/'recorder-telemetry-ui.png'),full_page=True)
        f['camera_report'].pop('recorder')
        expect(details).to_have_text('Recorder diagnostics are unavailable for this recording.')
        assert not errors,errors
        browser.close()
    (artifacts/'recorder-browser-result.json').write_text(json.dumps({'passed':True,'hardware_commands':0,
        'checks':['queue and stalls','diagnostics path','early failure','stale telemetry','historical missing telemetry']},indent=2)+'\n')
    print('Recorder telemetry browser checks passed (synthetic HTTP only)')


if __name__=='__main__':main()
