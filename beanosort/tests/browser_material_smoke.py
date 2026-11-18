"""Immediate movements and reweighing in a disposable browser/database."""
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

from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
PYTHON=ROOT/'.venv/bin/python'
BOOTSTRAP=r'''
import json,os
from pathlib import Path
from sqlalchemy import select
from beanosort.cli import initialize,migrate
from beanosort.db import Database
from beanosort.models import Station
from beanosort.service import Workflow
migrate(os.environ['BEANOSORT_DATABASE_URL'])
db=Database(os.environ['BEANOSORT_DATABASE_URL'],'simulation')
uid=initialize(db,'browser',os.environ['BEANOSORT_ADMIN_PASSWORD']);workflow=Workflow(db)
with db.read() as s:station=s.scalar(select(Station.id).filter_by(name='Receiving desk'))
fixture={}
for index,label in enumerate('ABCDEF',1):
    cid=workflow.create_container(uid,label='Material bin '+label,location='Test shelf',capacity='1',tare_mass='0.1')
    pid=workflow.receive(uid,'receipt-bin-'+label,dict(container_id=cid,station_id=station,net_mass=f'0.0{index}',ready=True,
        evidence=dict(kind='simulation',method='Test scale',tare_basis='Net after tare',precision_mg=1))) if index<=4 else None
    fixture[label]={'container':cid,'portion':pid}
Path(os.environ['MATERIAL_FIXTURE']).write_text(json.dumps(fixture))
'''


def main():
    artifacts=ROOT/'var/qa/material-browser';artifacts.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='material-browser-',dir=ROOT/'var') as tmp:
        tmp=Path(tmp)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        url=f'http://127.0.0.1:{port}';password=secrets.token_urlsafe(24)
        env={**os.environ,'BEANOSORT_DATABASE_URL':'sqlite:///'+str(tmp/'test.db'),'BEANOSORT_ENVIRONMENT':'simulation',
             'BEANOSORT_SECRET':secrets.token_urlsafe(48),'BEANOSORT_PUBLIC_URL':url,
             'BEANOSORT_ADMIN_PASSWORD':password,'MATERIAL_FIXTURE':str(tmp/'fixture.json')}
        subprocess.run([str(PYTHON),'-c',BOOTSTRAP],env=env,cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
        fixture=json.loads((tmp/'fixture.json').read_text())
        with (artifacts/'server.log').open('w') as log:
            server=subprocess.Popen([str(PYTHON),'-m','uvicorn','beanosort.web:create_app','--factory','--host','127.0.0.1',
                                     '--port',str(port)],env=env,cwd=ROOT,stdout=log,stderr=log)
            try:
                for _ in range(100):
                    try:urllib.request.urlopen(url+'/health',timeout=1).close();break
                    except OSError:time.sleep(.1)
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True)
                    context=browser.new_context(viewport={'width':1440,'height':1000});page=context.new_page()
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(url+'/login');page.locator('[name=username]').fill('browser');page.locator('[name=password]').fill(password)
                    page.get_by_role('button',name='Sign in',exact=True).click()
                    page.goto(url+'/handling');assert page.locator('.grid.two > section').count()==4
                    def move(a,b,amount,whole=False,reason=''):
                        page.goto(url+'/handling/contents')
                        page.locator('[name=source_id]').select_option(fixture[a]['container'])
                        page.locator('[name=destination_id]').select_option(fixture[b]['container'])
                        if whole:
                            page.locator('[name=moved_mass]').fill('0.008')
                            page.locator('[name=empty_source]').check()
                            expect(page.locator('[name=moved_mass]')).to_be_disabled()
                            expect(page.locator('[data-moved-mass-label]')).to_have_css('text-decoration-line','line-through')
                            assert not page.locator('[name=moved_mass]').evaluate('(input)=>input.required')
                            assert page.locator('form[data-material-form]').evaluate("f=>!new FormData(f).has('moved_mass')")
                            page.locator('[name=empty_source]').uncheck()
                            expect(page.locator('[name=moved_mass]')).to_be_enabled()
                            assert page.locator('[name=moved_mass]').input_value()=='0.008'
                            assert page.locator('[name=moved_mass]').evaluate('(input)=>input.required')
                            expect(page.locator('[data-moved-mass-label]')).to_have_css('text-decoration-line','none')
                            page.locator('[name=moved_mass]').fill('')
                            page.locator('[name=empty_source]').check()
                        else:page.locator('[name=moved_mass]').fill(amount)
                        if reason:page.locator('[name=reason]').fill(reason)
                        assert page.locator('[data-material-preview]').inner_text().startswith('Source:')
                        page.get_by_role('button',name='Record movement',exact=True).click()
                        page.get_by_role('heading',name='Movement recorded',exact=True).wait_for()
                        return page.url
                    move('A','E','0.01',True)
                    move('B','F','0.005')
                    move('E','B','0.009',True,'Actual weighing after emptying')
                    record=move('C','B','0.007')
                    def weigh(label,value,basis='gross'):
                        page.goto(url+'/handling/reconcile?container_id='+fixture[label]['container'])
                        page.locator('[name=weighing_basis]').select_option(basis)
                        page.locator('[name=weight]').fill(value);page.locator('[name=reason]').fill('Current scale reading')
                        page.get_by_role('button',name='Record weighing',exact=True).click()
                        page.get_by_role('heading',name='Weighing recorded',exact=True).wait_for()
                        return page.url
                    weighing=weigh('B','0.129');weigh('F','0.1');weigh('D','0.045','net')
                    page.goto(url+'/handling/contents')
                    labels=page.locator('[name=source_id] option').all_text_contents()
                    assert any('0.029 kg contents' in label and 'nominal capacity 1.000 kg' in label for label in labels)
                    assert not any(item['portion'] and item['portion'] in label for item in fixture.values() for label in labels)
                    page.locator('[name=source_id]').select_option(fixture['C']['container'])
                    assert page.locator('[name=destination_id] option[value="'+fixture['C']['container']+'"]').evaluate('(option)=>option.disabled')
                    page.locator('[name=destination_id]').select_option(fixture['E']['container'])
                    page.locator('[name=moved_mass]').fill('0.001')
                    other=page.context.new_page();other.goto(url+'/handling/reconcile?container_id='+fixture['C']['container'])
                    other.locator('[name=weighing_basis]').select_option('net');other.locator('[name=weight]').fill('0.022')
                    other.locator('[name=reason]').fill('Parallel weighing');other.get_by_role('button',name='Record weighing',exact=True).click()
                    other.get_by_role('heading',name='Weighing recorded',exact=True).wait_for();other.close()
                    page.get_by_role('button',name='Record movement',exact=True).click()
                    page.get_by_role('heading',name='Record not changed',exact=True).wait_for()
                    assert 'changed' in page.inner_text('main')
                    for width in (1440,768,390):
                        page.set_viewport_size({'width':width,'height':1000})
                        for name,path in [('index',url+'/handling'),('contents',url+'/handling/contents'),('reconcile',url+'/handling/reconcile'),
                                          ('movement',record),('weighing',weighing),('package',url+'/handling/package')]:
                            page.goto(path)
                            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(name,width)
                            page.screenshot(path=str(artifacts/f'{name}-{width}.png'),full_page=True)
                    assert not errors,errors
                    browser.close()
                with sqlite3.connect(tmp/'test.db') as db:
                    assert db.execute('SELECT sum(delta_mg) FROM ledger').fetchone()[0]==96000
                    assert db.execute('SELECT count(*) FROM reconciliations').fetchone()[0]==0
                    assert db.execute("SELECT count(*) FROM operations WHERE kind IN ('contents_move','weight_adjustment') AND status!='completed'").fetchone()[0]==0
                    assert db.execute("SELECT count(*) FROM receipts").fetchone()[0]==4
                    assert db.execute("SELECT count(*) FROM measurements m JOIN handling_plans p ON p.operation_id=m.operation_id WHERE json_extract(p.data,'$.empty_source')=1 AND m.role='moved'").fetchone()[0]==0
                result={'four_movement_cases':'passed','whole_moves_without_weighing':'passed','crossed_label_and_restored_draft':'passed','gross_net_and_zero_weighings':'passed',
                        'stale_form_rejected':'passed','exact_mass_ledger_total_mg':96000,'viewports':[1440,768,390],'page_errors':errors}
                (artifacts/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
            finally:server.terminate();server.wait(timeout=20)


if __name__=='__main__':main()
