"""Automatic fill pauses and user overrides on a disposable simulation database."""
import json
from decimal import Decimal
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
PYTHON=ROOT/'.venv/bin/python'
BOOTSTRAP=r'''
import json,os
from pathlib import Path
from sqlalchemy import select
from beanosort.cli import initialize,migrate
from beanosort.db import Database
from beanosort.models import Station,Version
from beanosort.service import Workflow
migrate(os.environ['BEANOSORT_DATABASE_URL'])
db=Database(os.environ['BEANOSORT_DATABASE_URL'],'simulation')
uid=initialize(db,'browser',os.environ['BEANOSORT_ADMIN_PASSWORD']);workflow=Workflow(db)
with db.read() as s:
    stations={v.name:v.id for v in s.scalars(select(Station))}
    versions={v.kind:v.id for v in s.scalars(select(Version).where(Version.name!='Simulation capture'))}
containers={}
for name,capacity,quantity in [('Source','10','2'),('Selected','10',None),('Receiving','1','0.9'),('Replacement','10',None)]:
    cid=workflow.create_container(uid,label=name,location='Test shelf',capacity=capacity,tare_mass='0.1');containers[name]=cid
    if quantity:
        pid=workflow.receive(uid,'receive-capacity-'+name,dict(container_id=cid,station_id=stations['Receiving desk'],net_mass=quantity,
            ready=True,evidence=dict(kind='simulation',method='Test scale',tare_basis='net after tare')))
        if name=='Source':source=pid
op=workflow.prepare(uid,'browser-capacity-job',dict(portion_id=source,planned_mass='2',station_id=stations['Training sorter'],
    selected_id=containers['Selected'],pass_through_id=containers['Receiving'],ready=True,recipe_id=versions['recipe'],
    capture_model_id=versions['capture_model'],policy_id=versions['reconciliation_policy']))
Path(os.environ['CAPACITY_FIXTURE']).write_text(json.dumps({'operation':op,'containers':containers}))
'''


def main():
    artifacts=ROOT/'var/qa/capacity-browser';artifacts.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='capacity-browser-',dir=ROOT/'var') as temporary:
        tmp=Path(temporary)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        url=f'http://127.0.0.1:{port}';password=secrets.token_urlsafe(24)
        env={**os.environ,'BEANOSORT_DATABASE_URL':'sqlite:///'+str(tmp/'test.db'),'BEANOSORT_ENVIRONMENT':'simulation',
            'BEANOSORT_SECRET':secrets.token_urlsafe(48),'BEANOSORT_PUBLIC_URL':url,'BEANOSORT_ADMIN_PASSWORD':password,
            'CAPACITY_FIXTURE':str(tmp/'fixture.json')}
        subprocess.run([str(PYTHON),'-c',BOOTSTRAP],env=env,cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
        fixture=json.loads((tmp/'fixture.json').read_text());op=fixture['operation']
        with (artifacts/'server.log').open('w') as log:
            server=subprocess.Popen([str(PYTHON),'-m','uvicorn','beanosort.web:create_app','--factory','--host','127.0.0.1',
                '--port',str(port)],env=env,cwd=ROOT,stdout=log,stderr=log)
            try:
                for _ in range(100):
                    try:urllib.request.urlopen(url+'/health',timeout=1).close();break
                    except OSError:time.sleep(.1)
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True)
                    context=browser.new_context(viewport={'width':1440,'height':1050});page=context.new_page()
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(url+'/login');page.locator('[name=username]').fill('browser');page.locator('[name=password]').fill(password)
                    page.get_by_role('button',name='Sign in',exact=True).click()
                    page.goto(url+'/jobs/'+op)
                    panel=page.locator('[data-capacity-monitor]')
                    assert panel.is_visible() and panel.locator('.capacity-message.orange').count()==1
                    fills=page.locator('[data-receiver-fill]')
                    assert fills.is_visible() and fills.get_by_role('progressbar').count()==2
                    assert fills.get_by_role('progressbar',name='Pass Through · Receiving fill').get_attribute('aria-valuenow')=='90'
                    assert fills.get_by_role('progressbar',name='Selected · Selected fill').get_attribute('aria-valuenow')=='0'
                    assert fills.get_by_role('progressbar',name='Pass Through · Receiving fill').locator('.receiver-fill-amount.orange').count()==1
                    assert fills.get_by_role('progressbar',name='Selected · Selected fill').locator('.receiver-fill-amount.green').count()==1
                    page.locator('[name=count]').fill('2000')
                    page.get_by_role('button',name='Start simulation workflow',exact=True).click()
                    page.get_by_role('heading',name='Running',exact=True).wait_for()
                    page.get_by_role('button',name='Generate all remaining particles',exact=True).click()
                    page.get_by_role('heading',name='Paused',exact=True).wait_for()
                    assert '100.0%' in panel.inner_text()
                    with sqlite3.connect(tmp/'test.db') as connection:
                        first=dict(connection.execute('SELECT id,digest FROM particles'))
                        assert len(first)<250
                    for expected in (105,110):
                        page.get_by_role('button',name='Override capacity warning & resume',exact=True).click()
                        page.get_by_role('heading',name='Running',exact=True).wait_for()
                        assert f'{expected}%' in panel.inner_text()
                        if expected==105:
                            response=context.request.post(url+'/jobs/'+op+'/simulate-batch',form={'csrf':panel.get_attribute('data-csrf')})
                            assert response.ok and response.json()['paused']
                            live=fills.get_by_role('progressbar',name='Pass Through · Receiving fill')
                            expect(live).to_have_attribute('aria-valuenow','105')
                            expect(live).to_have_attribute('aria-valuemax','125')
                            page.reload()
                        else:
                            page.get_by_role('button',name='Generate all remaining particles',exact=True).click()
                        page.get_by_role('heading',name='Paused',exact=True).wait_for()
                        assert f'{expected}.0%' in panel.inner_text()
                        assert panel.locator('.capacity-message.red').count()==1
                        gauge=fills.get_by_role('progressbar',name='Pass Through · Receiving fill')
                        assert Decimal(gauge.get_attribute('aria-valuenow'))==expected
                        assert gauge.get_attribute('aria-valuemax')=='125'
                        assert gauge.locator('.receiver-fill-amount.red').evaluate('el=>el.getBoundingClientRect().width')>0
                    for width in (1440,768,390):
                        page.set_viewport_size({'width':width,'height':1050})
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(artifacts/f'paused-overfill-{width}.png'),full_page=True)
                    page.get_by_role('link',name='Change containers',exact=True).first.click()
                    page.get_by_role('heading',name='Change output containers',exact=True).wait_for()
                    page.locator('[name=pass_through_id]').select_option(fixture['containers']['Replacement'])
                    for field in ('feeding_stopped','in_flight_clear','ready'):page.locator('[name='+field+']').check()
                    page.get_by_role('button',name='Record replacements',exact=True).click()
                    page.get_by_role('heading',name='Paused',exact=True).wait_for()
                    assert not panel.is_visible()
                    assert fills.get_by_role('progressbar',name='Pass Through · Replacement fill').get_attribute('aria-valuenow')=='0'
                    page.get_by_role('button',name='Resume sorting workflow',exact=True).click()
                    page.get_by_role('heading',name='Running',exact=True).wait_for()
                    page.get_by_role('button',name='Generate all remaining particles',exact=True).click()
                    page.wait_for_function("document.querySelector('main').textContent.includes('2000 particle records received')")
                    assert not errors,errors
                    browser.close()
                with sqlite3.connect(tmp/'test.db') as connection:
                    assert connection.execute('SELECT COUNT(*) FROM particles').fetchone()[0]==2000
                    assert all(dict(connection.execute('SELECT id,digest FROM particles'))[key]==value for key,value in first.items())
                    assert connection.execute("SELECT COUNT(*) FROM audit WHERE action='capacity_pause'").fetchone()[0]==3
                    assert connection.execute("SELECT COUNT(*) FROM audit WHERE action='capacity_override'").fetchone()[0]==2
                    assert connection.execute('SELECT SUM(delta_mg) FROM ledger').fetchone()[0]==2900000
                result={'warning_at_90_and_start_allowed':True,'automatic_pauses':[100,105,110],
                    'receiving_fill_bars_and_over_100_scale':True,
                    'fill_updates_without_reloading':True,
                    'operator_overrides':True,'replacement_resets_receiving_fill':True,'original_particle_evidence_preserved':True,
                    'ledger_unchanged_by_capacity_monitor':True,'viewports':[1440,768,390],'page_errors':errors}
                (artifacts/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
            finally:server.terminate();server.wait(timeout=20)


if __name__=='__main__':main()
