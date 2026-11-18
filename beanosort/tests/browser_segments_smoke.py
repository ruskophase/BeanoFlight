"""Two swaps, saved weights, storage move and five-vessel reconciliation; isolated DB."""
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
from browser_hopper_smoke import ROOT,SETUP


def main():
    artifacts=ROOT/'var/qa/segments';artifacts.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='beanosort-segments-') as directory:
        root=Path(directory)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        url=f'http://127.0.0.1:{port}'
        env={**os.environ,'BEANOSORT_DATABASE_URL':'sqlite:///'+str(root/'browser.db'),'BEANOSORT_ENVIRONMENT':'simulation',
             'BEANOSORT_SECRET':secrets.token_hex(32),'BEANOSORT_PUBLIC_URL':url}
        setup_code=SETUP.replace("('Hopper source','Other pass vessel')","('Segment source','Selected A','Pass B','Selected C','Pass D')")
        setup_code=setup_code.replace('source=portion,container=containers[0],normal=normal,hopper=hopper',
            'source=portion,container=containers[0],normal=normal,hopper=hopper,selected=containers[1],passed=containers[2],selected2=containers[3],passed2=containers[4]')
        setup=subprocess.run([str(ROOT/'.venv/bin/python'),'-c',setup_code,str(root)],cwd=ROOT,env=env,capture_output=True,text=True)
        if setup.returncode:raise RuntimeError(setup.stderr)
        state=json.loads(setup.stdout.splitlines()[-1])
        with (artifacts/'server.log').open('w') as log:
            server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','beanosort.web:create_app','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                for _ in range(100):
                    try:urllib.request.urlopen(url+'/health',timeout=1).close();break
                    except Exception:time.sleep(.1)
                errors=[]
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000})
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(url+'/login');page.locator('[name=username]').fill('admin');page.locator('[name=password]').fill('browser-password-123');page.get_by_role('button',name='Sign in',exact=True).click()
                    page.goto(url+'/jobs/new?recipe_id='+state['normal'])
                    page.locator('[name=portion_id]').select_option(state['source']);page.locator('[name=planned_mass]').fill('2')
                    page.locator('[name=selected_id]').select_option(state['selected']);page.locator('[name=pass_through_id]').select_option(state['passed']);page.locator('[name=ready]').check()
                    page.get_by_role('button',name='Validate & reserve',exact=True).click();job=page.url
                    page.locator('[name=count]').fill('1000');page.get_by_role('button',name='Start simulation workflow',exact=True).click()
                    page.locator('form[action$="/simulate"] button').click()
                    for role,value in (('selected_id',state['selected2']),('pass_through_id',state['passed2'])):
                        page.get_by_role('button',name='Pause & change containers',exact=True).click()
                        page.locator('[name='+role+']').select_option(value)
                        for field in ('feeding_stopped','in_flight_clear','ready'):page.locator('[name='+field+']').check()
                        page.get_by_role('button',name='Record replacements',exact=True).click()
                        assert 'Sorting workflow paused' in page.locator('body').inner_text()
                        if role=='selected_id':
                            first=page.locator('.collection-card').filter(has=page.get_by_role('link',name='Selected A',exact=True)).first
                            first.locator('summary').click();first.locator('[name=gross]').fill('0.550');first.locator('[name=weighed]').check()
                            first.get_by_role('button',name='Save collection weighing',exact=True).click()
                            page.get_by_role('link',name='Move removed container',exact=True).first.click()
                            page.locator('[name=location_id]').select_option(index=1);page.locator('[name=physical_confirmation]').check()
                            page.get_by_role('button',name='Record container move',exact=True).click();page.goto(job)
                        page.get_by_role('button',name='Resume sorting workflow',exact=True).click()
                        page.locator('form[action$="/simulate"] button').click()
                    while page.locator('form[action$="/simulate"] button').count():
                        page.locator('form[action$="/simulate"] button').click()
                    page.locator('[name=target]').select_option('finishing')
                    page.get_by_role('button',name='Record transition',exact=True).click()
                    assert page.url==job+'/weigh'
                    assert page.locator('[data-weigh-gross]').count()==5
                    assert page.locator('[name=gross_selected]').input_value()=='0.550'
                    for code,gross in {'pass_through':'0.950','selected_2':'0.650','pass_through_2':'1.850','remainder':'8.500'}.items():
                        page.locator('[name=gross_'+code+']').fill(gross)
                    for confirm in page.locator('[data-weigh-confirm]').all():confirm.check()
                    for width in (1440,768,390):
                        page.set_viewport_size({'width':width,'height':1000});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    page.screenshot(path=str(artifacts/'five-vessels-390.png'),full_page=True)
                    page.get_by_role('button',name='Reconcile',exact=True).click();assert '0.000 kg' in page.locator('body').inner_text()
                    page.locator('[name=accept_review]').check();page.get_by_role('button',name='Accept reconciliation & release outputs',exact=True).click()
                    assert 'Completed' in page.locator('h1').inner_text()
                    page.locator('form[action$="/replay"] button').click()
                    last=page.locator('.collection-card').filter(has=page.get_by_role('link',name='Selected C',exact=True)).first
                    last.locator('a[href^="/portions/"]').click()
                    page.get_by_role('link',name='Population charts & bean photos',exact=True).first.click()
                    report=json.loads(page.locator('#population-data').text_content());assert report['populations']['input']['count']=='750'
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    page.screenshot(path=str(artifacts/'collection-charts-390.png'),full_page=True)
                    page.goto(url+'/');assert '10 kg' in page.locator('body').inner_text()
                    browser.close()
                assert not errors,errors
                print(json.dumps({'workflow':'pause, replace one/both streams across two boundaries, saved weighing, storage move, five-vessel reconciliation, scoped charts and replay passed','viewports':[1440,768,390],'page_errors':errors}))
            finally:server.terminate();server.wait(timeout=20)


if __name__=='__main__':main()
