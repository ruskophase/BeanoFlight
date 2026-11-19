"""Receiving legacy vessels with missing weights; disposable database only."""
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

from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
PYTHON=ROOT/'.venv/bin/python'


def main():
    artifacts=ROOT/'var/qa/receiving';artifacts.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='receiving-browser-',dir=ROOT/'var') as temp:
        temp=Path(temp)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        url=f'http://127.0.0.1:{port}';password=secrets.token_urlsafe(24)
        env={**os.environ,'BEANOSORT_DATABASE_URL':'sqlite:///'+str(temp/'test.db'),
             'BEANOSORT_ENVIRONMENT':'simulation','BEANOSORT_SECRET':secrets.token_urlsafe(48),
             'BEANOSORT_ADMIN_PASSWORD':password,'BEANOSORT_PUBLIC_URL':url}
        setup='''
from alembic import command
from alembic.config import Config
import os,sqlite3
config=Config('alembic.ini');config.attributes['database_url']=os.environ['BEANOSORT_DATABASE_URL']
command.upgrade(config,'0004')
with sqlite3.connect(os.environ['BEANOSORT_DATABASE_URL'].removeprefix('sqlite:///')) as c:
    c.execute("INSERT INTO containers(id,created_at,label,kind,location,capacity_mg) VALUES ('legacy-one','2000','Legacy tare missing','bin','Floor',25000000)")
    c.execute("INSERT INTO containers(id,created_at,label,kind,location) VALUES ('legacy-two','2000','Legacy both weights missing','bin','Floor')")
'''
        subprocess.run([str(PYTHON),'-c',setup],cwd=ROOT,env=env,check=True,stdout=subprocess.DEVNULL)
        subprocess.run([str(PYTHON),'-m','beanosort.cli','init','--username','browser'],cwd=ROOT,env=env,check=True,stdout=subprocess.DEVNULL)
        with (artifacts/'server.log').open('w') as log:
            server=subprocess.Popen([str(PYTHON),'-m','uvicorn','beanosort.web:create_app','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                for _ in range(100):
                    try:urllib.request.urlopen(url+'/health',timeout=1).close();break
                    except OSError:time.sleep(.1)
                else:raise RuntimeError('Receiving browser server did not start')
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050})
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(url+'/login');page.get_by_label('Username',exact=True).fill('browser');page.get_by_label('Password',exact=True).fill(password)
                    page.get_by_role('button',name='Sign in',exact=True).click();page.get_by_role('link',name='Receive material',exact=True).click()
                    form=page.locator('form[action="/receive"]');form.get_by_role('button',name='Next: receiving containers',exact=True).click()
                    first=form.locator('[name="receiving_container_id"]').first;first.select_option('legacy-one')
                    row=form.locator('[data-container-rows] [data-load]').first
                    assert row.locator('[data-tare-entry]').is_visible() and not row.locator('[data-capacity-entry]').is_visible()
                    form.get_by_role('button',name='Add another container',exact=True).click()
                    second=form.locator('[name="receiving_container_id"]').nth(1)
                    assert second.locator('option[value="legacy-one"]').evaluate('el => el.disabled && el.hidden')
                    second.select_option('legacy-two');assert first.locator('option[value="legacy-two"]').evaluate('el => el.disabled && el.hidden')
                    form.locator('[name="ready"]').check();form.get_by_role('button',name='Next: weigh beans',exact=True).click()
                    assert page.locator('[data-receiving-step="1"]').is_visible()
                    form.locator('[name="receiving_tare_mass"]').first.fill('0.500')
                    form.locator('[name="receiving_tare_mass"]').nth(1).fill('0.750')
                    form.locator('[name="receiving_capacity"]').nth(1).fill('20')
                    first.select_option('');assert second.locator('option[value="legacy-one"]').evaluate('el => !el.disabled && !el.hidden')
                    first.select_option('legacy-one');assert form.locator('[name="receiving_tare_mass"]').first.input_value()==''
                    form.locator('[name="receiving_tare_mass"]').first.fill('0.500')
                    assert form.locator('[name="receiving_tare_mass"]').nth(1).input_value()=='0.750'
                    form.locator('[data-container-rows] [data-load]').nth(1).get_by_role('button',name='Remove this container',exact=True).click()
                    assert first.locator('option[value="legacy-two"]').evaluate('el => !el.disabled && !el.hidden')
                    form.get_by_role('button',name='Add another container',exact=True).click()
                    form.locator('[name="receiving_container_id"]').nth(1).select_option('legacy-two')
                    form.locator('[name="receiving_tare_mass"]').nth(1).fill('0.750');form.locator('[name="receiving_capacity"]').nth(1).fill('20')
                    for width in (1440,768,390):
                        page.set_viewport_size({'width':width,'height':1050})
                        assert not page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
                        page.screenshot(path=str(artifacts/f'inline-weights-{width}.png'),full_page=True)
                    form.get_by_role('button',name='Next: weigh beans',exact=True).click()
                    assert page.locator('[data-receiving-step="2"]').is_visible()
                    assert form.locator('[name="receiving_net_mass"]').first.get_attribute('max')=='30'
                    assert form.locator('[name="receiving_net_mass"]').nth(1).get_attribute('max')=='24'
                    form.locator('[name="receiving_net_mass"]').first.fill('1.250');form.locator('[name="receiving_net_mass"]').nth(1).fill('24.000')
                    for checkbox in form.locator('[name="tared"]').all():checkbox.check()
                    form.get_by_role('button',name='Next: storage location',exact=True).click()
                    for select in form.locator('[name="receiving_location_id"]').all():
                        value=select.locator('option').filter(has_text='Floor').get_attribute('value');select.select_option(value)
                    form.locator('[name="placed"]').check();form.get_by_role('button',name='Receive into stock',exact=True).click()
                    assert '25.250 kg' in page.inner_text('main') and '24.000 kg' in page.inner_text('main')
                    page.goto(url+'/receive');assert page.locator('[name="receiving_container_id"] option[value="legacy-one"]').count()==0
                    page.get_by_role('button',name='Next: receiving containers',exact=True).click()
                    page.locator('summary').filter(has_text='Containers unavailable for receiving').click()
                    assert 'Legacy tare missing' in page.inner_text('main') and 'Already contains material' in page.inner_text('main')
                    assert not errors,errors
                    with sqlite3.connect(temp/'test.db') as c:
                        assert c.execute("SELECT tare_mg,capacity_mg FROM containers WHERE id='legacy-one'").fetchone()==(500000,25000000)
                        assert c.execute("SELECT tare_mg,capacity_mg FROM containers WHERE id='legacy-two'").fetchone()==(750000,20000000)
                        assert c.execute('SELECT SUM(delta_mg) FROM ledger').fetchone()[0]==25250000
                    print(json.dumps({'inline_missing_weights':'passed','duplicate_choices_and_restoration':'passed','net_mass_and_120_percent_limit':'passed','occupied_container_explanation':'passed','viewports':[1440,768,390],'page_errors':errors}));browser.close()
            finally:server.terminate();server.wait(timeout=15)


if __name__=='__main__':main()
