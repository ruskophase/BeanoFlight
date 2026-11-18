"""Real browser + HTTP job protocol on disposable inventory; no hardware imports."""
import json,os,secrets,socket,subprocess,tempfile,threading,time,urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1]


def main():
    artifacts=ROOT/'var/qa/machine-jobs';artifacts.mkdir(parents=True,exist_ok=True)
    state={'job':None,'state':'prepared','active':0.,'last':time.monotonic(),'events':[],'commands':[]}
    settings=json.loads((ROOT/'src/beanosort/static/sorter_candidate_settings.json').read_text())
    token=secrets.token_urlsafe(40)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):self.respond()
        def do_POST(self):self.respond()
        def respond(self):
            assert self.headers.get('Authorization')=='Bearer '+token
            data=json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.command=='POST' else None
            if self.path=='/v1/catalog':
                result={'machine_id':'fake-browser','configuration':{},'settings':[{'id':'test-bundle','name':settings['name'],'values':settings}],
                    'sources':[{'id':'live','name':'Live cameras','physical':True}],'maximum_duration_seconds':1800,'maximum_pause_seconds':600}
            else:
                tick=time.monotonic()
                if state['state']=='running':state['active']+=tick-state['last']
                state['last']=tick
                if data and self.path.endswith('/commands'):
                    state['commands'].append(data['action']);state['state']={'pause':'paused','resume':'running','end':'completed','stop':'failed'}[data['action']]
                elif data:
                    state['job']=data;state['state']='running'
                    state['events']=[{'sequence':1,'kind':'bean','created_at':'2026-11-17T10:00:00+00:00','data':{
                        'bean_sequence':10,'ordinal':0,'features':{'caml_lab_l_mean':21,'camr_lab_l_mean':22},'sample_count':2}}]
                if state['job'] and state['active']>=state['job']['duration_seconds']:state['state']='completed'
                status={'state':state['state'],'active_seconds':state['active'],'elapsed_seconds':state['active']+1,
                    'cursor':len(state['events']),'final_sequence':len(state['events']) if state['state'] in {'completed','failed'} else None,
                    'metrics':{'tracked_beans':1,'measurements':2,'beans_with_colour':1,'beans_with_volume':1},'pause_drained':True}
                if '/events?' in self.path:
                    after=int(self.path.split('after=')[1].split('&')[0]);result={'status':status,'events':[e for e in state['events'] if e['sequence']>after]}
                else:result=status
            body=json.dumps(result).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    api=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=api.serve_forever,daemon=True);thread.start()
    with tempfile.TemporaryDirectory(dir=ROOT/'var',prefix='machine-browser-') as directory:
        tmp=Path(directory);(tmp/'token').write_text(token)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        url=f'http://127.0.0.1:{port}';password=secrets.token_urlsafe(30)
        env={**os.environ,'BEANOSORT_DATABASE_URL':'sqlite:///'+str(tmp/'test.db'),'BEANOSORT_ENVIRONMENT':'simulation',
            'BEANOSORT_SECRET':secrets.token_urlsafe(48),'BEANOSORT_ADMIN_PASSWORD':password,
            'BEANOSORT_MACHINE_JOBS_TOKEN_FILE':str(tmp/'token'),'BEANOSORT_MACHINE_JOBS_PORT':str(api.server_port)}
        bootstrap='''
from beanosort.cli import initialize,migrate
from beanosort.db import Database
from beanosort.service import Workflow
from beanosort.models import Station
from sqlalchemy import select
import os,json
u=os.environ['BEANOSORT_DATABASE_URL'];migrate(u);d=Database(u,'simulation');uid=initialize(d,'browser',os.environ['BEANOSORT_ADMIN_PASSWORD']);w=Workflow(d)
a=w.create_container(uid,label='Source',location='Shelf',capacity='20',tare_mass='0.5')
b=w.create_container(uid,label='Receiver',location='Machine',capacity='20',tare_mass='0.5')
with d.read() as s:station=s.scalar(select(Station.id).filter_by(name='Receiving desk'))
p=w.receive(uid,'receive-fixture',dict(container_id=a,station_id=station,net_mass='2',ready=True,supplier='Test',origin='Test',supplier_lot='1',bean_type='beans',evidence={'kind':'simulation','method':'scale','tare_basis':'net','precision_mg':1}))
print(json.dumps({'portion':p,'receiver':b}))
'''
        completed=subprocess.run([str(ROOT/'.venv/bin/python'),'-c',bootstrap],env=env,cwd=ROOT,check=False,capture_output=True,text=True)
        if completed.returncode:raise RuntimeError(completed.stderr)
        ids=json.loads(completed.stdout.splitlines()[-1])
        with (artifacts/'server.log').open('w') as log:
            server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','beanosort.web:create_app','--factory','--host','127.0.0.1','--port',str(port)],env=env,cwd=ROOT,stdout=log,stderr=log)
            try:
                for _ in range(100):
                    try:urllib.request.urlopen(url+'/health',timeout=1).close();break
                    except OSError:time.sleep(.1)
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(headless=True);context=browser.new_context(viewport={'width':1440,'height':1100});page=context.new_page();errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(url+'/login');page.get_by_label('Username',exact=True).fill('browser');page.get_by_label('Password',exact=True).fill(password);page.get_by_role('button',name='Sign in',exact=True).click()
                    page.goto(url+'/jobs/new');page.locator('[name=portion_id]').select_option(ids['portion'])
                    option=page.locator('[name=recipe_id] option[data-machine-recipe=true]').get_attribute('value')
                    page.locator('[name=recipe_id]').select_option(option)
                    expect(page.locator('#machine-prepare')).to_be_visible();expect(page.locator('[name=selected_id]')).to_be_hidden()
                    page.locator('[name=pass_through_id]').select_option(ids['receiver']);page.locator('[name=duration_seconds]').fill('10')
                    page.get_by_label('No image saves',exact=True).check();page.locator('[name=ready]').check()
                    page.screenshot(path=str(artifacts/'prepare.png'),full_page=True)
                    page.get_by_role('button',name='Validate & reserve',exact=True).click();joburl=page.url
                    page.get_by_role('button',name='START',exact=True).click();expect(page.locator('[data-machine-state]')).to_have_text('running',timeout=15000)
                    page.get_by_role('button',name='PAUSE',exact=True).click();expect(page.locator('[data-machine-state]')).to_have_text('paused',timeout=15000)
                    before=state['active'];time.sleep(1.5);assert abs(state['active']-before)<.2
                    page.close();time.sleep(.7);page=context.new_page();page.goto(joburl)
                    expect(page.locator('[data-machine-state]')).to_have_text('paused');page.screenshot(path=str(artifacts/'paused.png'),full_page=True)
                    page.get_by_role('button',name='RESUME',exact=True).click();expect(page.locator('[data-machine-state]')).to_have_text('running',timeout=15000)
                    page.get_by_role('button',name='END',exact=True).click();page.wait_for_url('**/weigh',timeout=15000)
                    expect(page.get_by_role('heading',name='Weigh containers')).to_be_visible()
                    page.screenshot(path=str(artifacts/'weigh.png'),full_page=True)
                    assert state['commands']==['resume','pause','resume','end'];assert state['job']['image_mode']=='none';assert not errors,errors
                    browser.close()
                (artifacts/'result.json').write_text(json.dumps({'passed':True,'commands':state['commands'],'no_hardware':True,'image_bytes_transferred':0},indent=2))
                print('Browser machine job checks passed')
            finally:server.terminate();server.wait(timeout=15)
    api.shutdown();api.server_close();thread.join()
if __name__=='__main__':main()
