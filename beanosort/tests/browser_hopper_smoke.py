"""Hopper-emptying WebGUI acceptance in a disposable database; no live stock."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SETUP = r'''
from pathlib import Path
import json,os,sys
from beanosort.cli import initialize,migrate
from beanosort.db import Database
from beanosort.service import Workflow
from beanosort.models import Station,Version
from beanosort.references import import_bundle,create_recipe
from beanosort.sorting_modes import HOPPER_RECIPE_NAME
from sqlalchemy import select
sys.path.insert(0,str(Path.cwd()/'tests'))
from reference_fixture import bundle
root=Path(sys.argv[1]);url=os.environ['BEANOSORT_DATABASE_URL'];migrate(url)
db=Database(url,'simulation');uid=initialize(db,'admin','browser-password-123');wf=Workflow(db)
dataset=import_bundle(db,uid,os.environ.get('BEANOSORT_REFERENCE_BUNDLE') or bundle(root/'bundle'),root/'reference',name='Browser hopper reference')
normal=create_recipe(db,uid,'browser-normal','Browser dark outliers',dataset)
hopper=create_recipe(db,uid,'browser-hopper',HOPPER_RECIPE_NAME,dataset,feature='area_mm2',relation='pass_all')
with db.read() as s:
 stations={x.name:x.id for x in s.scalars(select(Station))}
containers=[wf.create_container(uid,label=label,location='Browser shelf',capacity='30',tare_mass='0.5') for label in ('Hopper source','Other pass vessel')]
portion=wf.receive(uid,'browser-receive',dict(container_id=containers[0],station_id=stations['Receiving desk'],net_mass='10',ready=True,evidence=dict(kind='simulation',method='browser fixture',tare_basis='net beans')))
print(json.dumps(dict(source=portion,container=containers[0],normal=normal,hopper=hopper)))
'''


def main():
    artifacts = ROOT/'var/qa/hopper'; artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='beanosort-hopper-') as directory:
        root=Path(directory)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        url=f'http://127.0.0.1:{port}'
        env={**os.environ,'BEANOSORT_DATABASE_URL':'sqlite:///'+str(root/'browser.db'),
             'BEANOSORT_ENVIRONMENT':'simulation','BEANOSORT_SECRET':secrets.token_hex(32),'BEANOSORT_PUBLIC_URL':url}
        setup=subprocess.run([str(ROOT/'.venv/bin/python'),'-c',SETUP,str(root)],cwd=ROOT,env=env,capture_output=True,text=True)
        if setup.returncode:
            raise RuntimeError(setup.stderr)
        state=json.loads(setup.stdout.splitlines()[-1])
        with (artifacts/'server.log').open('w') as log:
            server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','beanosort.web:create_app','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                for _ in range(100):
                    try: urllib.request.urlopen(url+'/health',timeout=1).close();break
                    except Exception: time.sleep(.1)
                errors=[]
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000})
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(url+'/login');page.locator('[name=username]').fill('admin');page.locator('[name=password]').fill('browser-password-123');page.get_by_role('button',name='Sign in',exact=True).click()
                    page.goto(url+'/recipes/'+state['hopper']);assert 'All beans to Pass-through' in page.locator('body').inner_text()
                    page.goto(url+'/jobs/new?recipe_id='+state['hopper'])
                    page.locator('[name=portion_id]').select_option(state['source'])
                    selected=page.locator('[name=selected_id]');passed=page.locator('[name=pass_through_id]')
                    assert selected.is_disabled() and not selected.is_visible()
                    passed.select_option(state['container'])
                    page.locator('[name=recipe_id]').select_option(state['normal'])
                    assert selected.is_visible() and not selected.is_disabled() and passed.input_value()==''
                    page.locator('[name=recipe_id]').select_option(state['hopper']);passed.select_option(state['container'])
                    page.locator('[name=planned_mass]').fill('2');page.locator('[name=ready]').check()
                    page.get_by_role('button',name='Validate & reserve',exact=True).click();job=page.url
                    page.locator('[name=count]').fill('200');page.get_by_role('button',name='Start simulation workflow',exact=True).click()
                    page.locator('form[action$="/simulate"] button').click()
                    page.locator('[name=target]').select_option('finishing')
                    page.get_by_role('button',name='Record transition',exact=True).click()
                    assert page.url==job+'/weigh'
                    assert page.locator('[data-weigh-gross]').count()==1
                    assert page.locator('[name=gross_selected]').count()==page.locator('[name=gross_remainder]').count()==0
                    assert page.locator('[name=gross_pass_through]').input_value()=='10.500'
                    for width in (1440,768,390):
                        page.set_viewport_size({'width':width,'height':1000})
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
                    page.screenshot(path=str(artifacts/'one-vessel-390.png'),full_page=True)
                    page.locator('[name=weighed_pass_through]').check();page.get_by_role('button',name='Reconcile',exact=True).click()
                    page.locator('[name=accept_review]').check();page.get_by_role('button',name='Accept reconciliation & release outputs',exact=True).click()
                    assert 'Completed' in page.locator('h1').inner_text()
                    assert 'Source / Pass-through: Hopper source (10 kg)' in page.locator('[data-operation-caption]').first.inner_text()
                    page.locator('form[action$="/replay"] button').click()
                    page.goto(url+'/containers/'+state['container']);assert '10 kg' in page.locator('body').inner_text()
                    page.screenshot(path=str(artifacts/'completed-container-390.png'),full_page=True)
                    browser.close()
                assert not errors,errors
                print(json.dumps({'workflow':'all-pass recipe, exclusive normal mode, shared source, one gross weighing, reconciliation/release and replay passed','viewports':[1440,768,390],'page_errors':errors}))
            finally:
                server.terminate();server.wait(timeout=20)


if __name__=='__main__': main()
