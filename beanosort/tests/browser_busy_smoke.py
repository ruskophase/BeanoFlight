"""Hold actual workflow requests to check waiting feedback, using isolated stock."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import parse_qs

from playwright.async_api import async_playwright, expect
from browser_hopper_smoke import ROOT, SETUP


async def exercise(url, state, artifacts):
    errors=[];checks=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        page=await browser.new_page(viewport={'width':1440,'height':1000})
        page.on('pageerror',lambda error:errors.append(str(error)))
        cdp=await page.context.new_cdp_session(page)
        probes=[]
        page.on('console',lambda message:probes.append(json.loads(message.text.removeprefix('BUSY_TEST:'))) if message.text.startswith('BUSY_TEST:') else None)
        # Inspect before native navigation suspends JS in the outgoing document.
        # During the request, compositor screenshots verify animation continues.
        await page.add_init_script('''
        for (const kind of ['click','submit']) window.addEventListener(kind,event=>{
          const modal=document.querySelector('[data-workflow-busy]');
          if (!modal?.open) return;
          console.debug('BUSY_TEST:'+JSON.stringify({kind,open:modal.open,
            prevented:event.defaultPrevented,title:modal.querySelector('[data-busy-title]').textContent,
            animation:getComputedStyle(modal.querySelector('.request-spinner')).animationName,
            busy:document.querySelector('main').getAttribute('aria-busy')}));
        });''')

        async def pending(path, method, trigger, message, form=None, screenshot=False, reduced=False):
            """A synthetic network gate keeps the old document visible for assertions."""
            arrived=asyncio.Event();release=asyncio.Event();requests=[]
            start_probe=len(probes)
            await page.emulate_media(reduced_motion='reduce' if reduced else 'no-preference')
            if screenshot:await page.set_viewport_size({'width':390,'height':1000})
            async def gate(route):
                if route.request.method!=method:
                    await route.continue_();return
                requests.append(parse_qs(route.request.post_data or ''))
                arrived.set()
                await release.wait()
                await route.continue_()
            await page.route(url+path,gate)
            action=asyncio.create_task(trigger())
            try:
                await asyncio.wait_for(arrived.wait(),15)
                assert any(probe['title']==message and probe['open'] and probe['busy']=='true' for probe in probes[start_probe:])
                first=await asyncio.wait_for(cdp.send('Page.captureScreenshot',{'format':'png'}),10)
                await asyncio.sleep(.35)
                second=await asyncio.wait_for(cdp.send('Page.captureScreenshot',{'format':'png'}),10)
                if not reduced:
                    assert hashlib.sha256(base64.b64decode(first['data'])).digest()!=hashlib.sha256(base64.b64decode(second['data'])).digest(),'Spinner froze during native navigation'
                assert probes[-1]['animation']==('none' if reduced else 'beanosort-spin')
                if form:
                    # Native controls remain successful; duplicate Enter/programmatic
                    # submissions are blocked without stripping CSRF/review values.
                    assert len(requests)==1
                    assert requests[0].get('csrf')
                if screenshot:
                    (artifacts/'waiting-390.png').write_bytes(base64.b64decode(second['data']))
                checks.append({'request':path.rsplit('/',1)[-1],'method':method,'submissions':len(requests),'message':message})
            finally:
                release.set()
                await asyncio.wait_for(action,30)
                # Removing the last route changes Chromium's cache mode. Keep
                # it stable until the new document and its assets have loaded.
                await page.wait_for_load_state('load')
                await page.unroute(url+path,gate)
            await expect(page.locator('[data-workflow-busy]')).not_to_be_visible()
            await expect(page.locator('main')).to_be_visible()
            await page.emulate_media(reduced_motion='no-preference')
            await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
            return requests[0]

        await page.goto(url+'/login')
        await page.locator('[name=username]').fill('admin');await page.locator('[name=password]').fill('browser-password-123')
        await page.get_by_role('button',name='Sign in',exact=True).click()
        await page.goto(url+'/jobs/new?recipe_id='+state['hopper'])
        await page.locator('[name=portion_id]').select_option(state['source'])
        await page.locator('[name=pass_through_id]').select_option(state['container'])
        await page.locator('[name=planned_mass]').fill('2');await page.locator('[name=ready]').check()
        await page.get_by_role('button',name='Validate & reserve',exact=True).click()
        job=page.url;path=job.removeprefix(url)
        await page.locator('[name=count]').fill('200')
        await page.get_by_role('button',name='Start simulation workflow',exact=True).click()
        await page.locator('form[action$="/simulate"] button').click()

        # A genuine server validation error replaces the waiting state normally.
        await page.locator('[name=target]').select_option('paused')
        await pending(path+'/transition','POST',lambda:page.get_by_role('button',name='Record transition',exact=True).click(),
                      'Recording the workflow pause…','form[action$="/transition"]')
        await expect(page.get_by_role('heading',name='Record not changed',exact=True)).to_be_visible()
        await page.goto(job)
        await page.locator('[name=target]').select_option('finishing')
        data=await pending(path+'/transition','POST',lambda:page.get_by_role('button',name='Record transition',exact=True).click(),
                           'Recording the finish boundary…','form[action$="/transition"]')
        assert data['target']==['finishing'] and page.url==job+'/weigh'
        # The normal path has no intermediate state or navigation click.
        assert await page.locator('[name=target]').count()==0
        await page.get_by_role('link',name='Back to sorting job',exact=True).click()
        await expect(page.get_by_role('heading',name='Finishing',exact=True)).to_be_visible()
        await pending(path+'/weigh','GET',lambda:page.get_by_role('link',name='Continue to container weighing',exact=True).click(),
                      'Calculating container weights and bean estimates…',reduced=True)
        # Back to an earlier GET must not restore its old busy veil.
        await page.go_back();await expect(page.get_by_role('heading',name='Finishing',exact=True)).to_be_visible()
        await expect(page.locator('[data-workflow-busy]')).not_to_be_visible()
        await page.get_by_role('link',name='Continue to container weighing',exact=True).click()
        # Invalid forms never lock the page or show a spinner.
        await page.get_by_role('button',name='Reconcile',exact=True).click()
        await expect(page.locator('[data-workflow-busy]')).not_to_be_visible()
        assert page.url==job+'/weigh'
        await page.locator('[data-weigh-confirm]').check()
        data=await pending(path+'/weigh/review','POST',lambda:page.get_by_role('button',name='Reconcile',exact=True).click(),
                           'Calculating reconciliation and bean estimates…','[data-weighing-form]')
        assert data['gross_pass_through']==['10.500'] and data['weighed_pass_through']==['on'] and data.get('key')
        await page.get_by_role('button',name='Accept reconciliation & release outputs',exact=True).click()
        await expect(page.locator('[data-workflow-busy]')).not_to_be_visible()
        await page.locator('[name=accept_review]').check()
        # A valid secondary submission while busy must not issue any navigation.
        await page.evaluate("window.BeanoSortBusy.show('Testing duplicate submission guard…'); document.querySelector('[data-weighing-accept]').requestSubmit()")
        assert probes[-1]['kind']=='submit' and probes[-1]['prevented']
        await page.evaluate('window.BeanoSortBusy.reset()')
        data=await pending(path+'/weigh/accept','POST',lambda:page.get_by_role('button',name='Accept reconciliation & release outputs',exact=True).click(),
                           'Recording final reconciliation and updating stock…','[data-weighing-accept]',screenshot=True)
        assert data.get('review_token') and data['accept_review']==['on'] and data.get('reason')
        await expect(page.get_by_role('heading',name='Completed',exact=True)).to_be_visible()
        # Escape cannot visually cancel a request that has already been issued.
        await page.evaluate("window.BeanoSortBusy.show('Testing waiting feedback…')")
        await page.keyboard.press('Escape')
        await expect(page.locator('[data-workflow-busy]')).to_be_visible()
        await page.evaluate('window.BeanoSortBusy.reset()')
        for width in (1440,768,390):
            await page.set_viewport_size({'width':width,'height':1000})
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        assert not errors,errors
        await browser.close()
    return {'delayed_requests':checks,'validation_and_error_recovery':'passed','back_navigation':'passed',
            'reduced_motion':'passed','viewports':[1440,768,390],'page_errors':errors}


def main():
    artifacts=ROOT/'var/qa/busy';artifacts.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='beanosort-busy-') as directory:
        root=Path(directory)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        url=f'http://127.0.0.1:{port}'
        env={**os.environ,'BEANOSORT_DATABASE_URL':'sqlite:///'+str(root/'browser.db'),
            'BEANOSORT_ENVIRONMENT':'simulation','BEANOSORT_SECRET':secrets.token_hex(32),'BEANOSORT_PUBLIC_URL':url}
        setup=subprocess.run([str(ROOT/'.venv/bin/python'),'-c',SETUP,str(root)],cwd=ROOT,env=env,capture_output=True,text=True)
        if setup.returncode:raise RuntimeError(setup.stderr)
        state=json.loads(setup.stdout.splitlines()[-1])
        with (artifacts/'server.log').open('w') as log:
            server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','beanosort.web:create_app','--factory',
                '--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                for _ in range(100):
                    try:urllib.request.urlopen(url+'/health',timeout=1).close();break
                    except OSError:time.sleep(.1)
                result=asyncio.run(asyncio.wait_for(exercise(url,state,artifacts),90))
                with sqlite3.connect(root/'browser.db') as db:
                    assert db.execute('SELECT count(*) FROM reconciliations').fetchone()[0]==1
                    assert db.execute('SELECT sum(delta_mg) FROM ledger').fetchone()[0]==10000000
                    assert db.execute('SELECT count(*) FROM particles').fetchone()[0]==200
                result['single_completion_and_mass_conservation']='passed'
                (artifacts/'result.json').write_text(json.dumps(result,indent=2)+'\n')
                print(json.dumps(result))
            finally:server.terminate();server.wait(timeout=20)


if __name__=='__main__':main()
