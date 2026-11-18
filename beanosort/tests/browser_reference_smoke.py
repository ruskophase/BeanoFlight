"""Photographed reference → recipe → simulation → measured outputs, disposable DB.

BEANOSORT_REFERENCE_BUNDLE optionally points at a real archived statistics bundle.
The default is a tiny explicitly synthetic test reference. Never touches live stock.
"""
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
from reference_fixture import bundle

ROOT=Path(__file__).resolve().parents[1]
PYTHON=ROOT/'.venv/bin/python'


def inspect_chart(page, feature, bucket):
    report=json.loads(page.locator('#population-data').text_content())
    edges=report['definitions'][feature]['edges']
    chart=page.locator(f'[data-population-chart="{feature}"]')
    box=chart.bounding_box()
    midpoint=(edges[bucket]+edges[bucket+1])/2
    x=(54+(midpoint-edges[0])/(edges[-1]-edges[0])*526)/600*box['width']
    with page.expect_response(lambda response:'/inspect?' in response.url and response.request.resource_type=='fetch') as response:
        chart.click(position={'x':x,'y':box['height']*.4})
    assert response.value.status==200
    page.wait_for_function("document.querySelector('[data-population-inspector]').getAttribute('aria-busy') === 'false'")
    return response.value.json()


def check_inspector(page, artifacts, name):
    report=json.loads(page.locator('#population-data').text_content())
    for key in ('input','pass_through'):
        page.locator(f'[data-population-toggle="{key}"]').uncheck()
        assert page.locator(f'path[data-population-series="{key}"]').count()==0
    assert page.locator('path[data-population-series="selected"]').count()>0
    hist=report['populations']['expected_selected']['histograms']['lab_l']
    bucket=next(i for i,value in enumerate(hist) if float(value)>0)
    data=inspect_chart(page,'lab_l',bucket)
    assert data['population']=='selected' and data['membership_evidence']=='calculated_prediction'
    page.wait_for_function("document.querySelector('.inspection-beans img')?.naturalWidth > 0")
    assert page.locator('[data-inspector-beans] article').count()>0
    if os.environ.get('BEANOSORT_REFERENCE_BUNDLE'):
        assert page.locator('.bean-colour-swatch').count()>0
    inspector=page.locator('[data-population-inspector]').bounding_box()
    charts=page.locator('.population-charts').bounding_box()
    assert inspector['x']+inspector['width']<=charts['x']
    page.screenshot(path=str(artifacts/f'inspector-{name}-desktop.png'),full_page=True)
    if name=='run':
        with page.expect_response(lambda response:'/inspect?' in response.url and response.request.resource_type=='fetch') as response:
            page.locator('[data-population-basis]').select_option('simulated')
        assert response.value.json()['membership_evidence']=='simulation'
        page.wait_for_function("document.querySelector('[data-population-inspector]').getAttribute('aria-busy') === 'false'")
        page.locator('[data-population-basis]').select_option('expected')
        page.wait_for_function("document.querySelector('[data-population-inspector]').getAttribute('aria-busy') === 'false'")
    page.locator('[data-population-toggle="input"]').check()
    assert not page.locator('[data-inspector-population] option[value="input"]').is_disabled()
    with page.expect_response(lambda response:'/inspect?' in response.url and response.request.resource_type=='fetch'):
        page.locator('[data-inspector-population]').select_option('input')
    page.wait_for_function("document.querySelector('[data-population-inspector]').getAttribute('aria-busy') === 'false'")
    page.locator('[data-inspector-close]').click()
    page.locator('[data-population-toggle="selected"]').uncheck()
    histogram=report['populations']['input']['histograms']['lab_l']
    bucket=max(range(len(histogram)),key=lambda i:float(histogram[i]))
    data=inspect_chart(page,'lab_l',bucket)
    if data['pages']>1:
        first={bean['bean_sequence'] for bean in data['items']}
        with page.expect_response(lambda response:'/inspect?' in response.url and response.request.resource_type=='fetch') as response:
            page.locator('[data-inspector-next]').click()
        second=response.value.json()
        assert second['page']==2 and not first.intersection(bean['bean_sequence'] for bean in second['items'])
        page.wait_for_function("document.querySelector('[data-population-inspector]').getAttribute('aria-busy') === 'false'")
    for width in (768,390):
        page.set_viewport_size({'width':width,'height':1000})
        assert not page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
        page.locator('[data-population-inspector]').scroll_into_view_if_needed()
        page.screenshot(path=str(artifacts/f'inspector-{name}-{width}.png'),full_page=True)
    page.set_viewport_size({'width':1440,'height':1050})
    page.locator('[data-population-toggle="input"]').uncheck()
    assert page.locator('path[data-population-series]').count()==0
    assert page.locator('[data-population-inspector]').is_hidden()
    for key in ('input','selected','pass_through'):page.locator(f'[data-population-toggle="{key}"]').check()
    # Unknown/empty intervals remain empty instead of showing unrelated photos.
    page.locator('[data-population-toggle="input"]').uncheck()
    page.locator('[data-population-toggle="pass_through"]').uncheck()
    empty=next(i for i,value in enumerate(hist) if float(value)==0)
    empty_data=inspect_chart(page,'lab_l',empty)
    assert empty_data['matching_count']=='0' and page.locator('[data-inspector-beans] article').count()==0
    assert 'No corresponding beans' in page.locator('[data-inspector-beans]').inner_text()
    page.locator('[data-inspector-close]').click()
    # A failed fetch must show a retry state, not a falsely empty population.
    page.route('**/inspect?**',lambda route:route.fulfill(status=503,body='Unavailable'))
    chart=page.locator('[data-population-chart="lab_l"]');chart.focus();chart.press('Enter')
    page.locator('[data-inspector-retry]').wait_for(state='visible')
    assert 'Could not load beans' in page.locator('[data-inspector-status]').inner_text()
    page.unroute('**/inspect?**')
    with page.expect_response(lambda response:'/inspect?' in response.url and response.request.resource_type=='fetch') as response:
        page.locator('[data-inspector-retry]').click()
    assert response.value.status==200
    page.wait_for_function("document.querySelector('[data-population-inspector]').getAttribute('aria-busy') === 'false'")
    assert page.locator('[data-inspector-retry]').is_hidden()
    page.locator('[data-inspector-close]').click()
    for key in ('input','selected','pass_through'):page.locator(f'[data-population-toggle="{key}"]').check()


def main():
    artifacts=ROOT/'var/qa/reference';artifacts.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='reference-browser-',dir=ROOT/'var') as temporary:
        temporary=Path(temporary)
        source=Path(os.environ['BEANOSORT_REFERENCE_BUNDLE']) if os.environ.get('BEANOSORT_REFERENCE_BUNDLE') else bundle(temporary/'fixture')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        url=f'http://127.0.0.1:{port}';password=secrets.token_urlsafe(24)
        env={**os.environ,'BEANOSORT_DATABASE_URL':'sqlite:///'+str(temporary/'test.db'),
             'BEANOSORT_ENVIRONMENT':'simulation','BEANOSORT_SECRET':secrets.token_urlsafe(48),
             'BEANOSORT_ADMIN_PASSWORD':password,'BEANOSORT_PUBLIC_URL':url}
        for command in (['init'],['demo'],['reference-import',str(source),'--storage',str(temporary/'snapshots'),'--name','Photographed coffee reference']):
            subprocess.run([str(PYTHON),'-m','beanosort.cli',*command,'--username','browser'],cwd=ROOT,env=env,check=True,stdout=subprocess.DEVNULL)
        # Registering the new recipe must not change the repeatable legacy demo.
        subprocess.run([str(PYTHON),'-m','beanosort.cli','demo','--username','browser'],cwd=ROOT,env=env,check=True,stdout=subprocess.DEVNULL)
        setup='''
from sqlalchemy import select
from beanosort.config import Settings
from beanosort.db import Database
from beanosort.models import ReferenceDataset,User,Version
from beanosort.references import create_recipe,retire_recipe
db=Database(Settings.from_env().database_url,'simulation')
with db.read() as s:
    uid=s.scalar(select(User.id).filter_by(username='browser'))
    did=s.scalar(select(ReferenceDataset.id))
    legacy=s.scalar(select(Version.id).filter_by(kind='recipe',name='Selection fixture / manual record'))
create_recipe(db,uid,'large-area','Large area: above median plus 2 sigma',did,'area_mm2','above','2')
create_recipe(db,uid,'small-area','Small area: below median minus 2 sigma',did,'area_mm2','below','2')
retire_recipe(db,uid,'retire-legacy',legacy,'Operator requested removal')
'''
        subprocess.run([str(PYTHON),'-c',setup],cwd=ROOT,env=env,check=True,stdout=subprocess.DEVNULL)
        with (artifacts/'server.log').open('w') as log:
            server=subprocess.Popen([str(PYTHON),'-m','uvicorn','beanosort.web:create_app','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                for _ in range(100):
                    try:
                        urllib.request.urlopen(url+'/health',timeout=1).close();break
                    except OSError: time.sleep(.1)
                else: raise RuntimeError('Reference browser server did not start')
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(headless=True)
                    page=browser.new_page(viewport={'width':1440,'height':1050})
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(url+'/login');page.get_by_label('Username',exact=True).fill('browser');page.get_by_label('Password',exact=True).fill(password)
                    page.get_by_role('button',name='Sign in',exact=True).click()
                    page.get_by_role('link',name='Sorting recipes',exact=True).click()
                    assert 'Selection fixture / manual record' not in page.inner_text('main')
                    for name,targeted in [('Large area: above median plus 2 sigma','47'),('Small area: below median minus 2 sigma','15')]:
                        page.get_by_role('link',name=name,exact=True).click()
                        page.wait_for_function("document.querySelector('[data-population-chart=area_mm2] path') !== null")
                        area=json.loads(page.locator('#population-data').text_content())
                        assert area['recipe']['feature']=='area_mm2' and area['definitions']['area_mm2']['unit']=='mm²'
                        if os.environ.get('BEANOSORT_REFERENCE_BUNDLE'):
                            assert area['populations']['targeted']['count']==targeted
                        page.screenshot(path=str(artifacts/f'area-{area["recipe"]["relation"]}-desktop.png'),full_page=True)
                        page.get_by_role('link',name='All sorting recipes',exact=True).click()
                    page.get_by_role('link',name='Dark outliers: L* below median minus 2 sigma',exact=True).click()
                    page.wait_for_function("document.querySelector('[data-population-chart] path') !== null")
                    page.wait_for_function("document.querySelector('.bean-gallery img')?.naturalWidth > 0")
                    assert 'population σ' in page.inner_text('main')
                    if os.environ.get('BEANOSORT_REFERENCE_BUNDLE'):
                        reference=json.loads(page.locator('#population-data').text_content())
                        assert reference['populations']['targeted']['count']=='78'
                        assert reference['populations']['expected_selected']['count']=='77.22'
                    recipe_path=page.url
                    page.screenshot(path=str(artifacts/'recipe-desktop.png'),full_page=True)
                    check_inspector(page,artifacts,'preview')
                    page.get_by_role('link',name='Prepare sorting with this recipe',exact=True).click()
                    selected_recipe=page.locator('[name="recipe_id"]').input_value()
                    assert 'Dark outliers' in page.locator('[name="recipe_id"] option:checked').inner_text()
                    for name,label in [('portion_id','Ready source'),('selected_id','Ready Selected'),('pass_through_id','Ready Pass-through')]:
                        value=page.locator(f'select[name="{name}"] option').filter(has_text=label).get_attribute('value')
                        page.locator(f'[name="{name}"]').select_option(value)
                    page.locator('[name="planned_mass"]').fill('20');page.locator('[name="ready"]').check()
                    page.get_by_role('button',name='Validate & reserve',exact=True).click();job_path=page.url
                    page.locator('[name="count"]').fill('600');page.get_by_role('button',name='Start simulation workflow',exact=True).click()
                    page.get_by_role('button',name='Generate all remaining particles',exact=True).click()
                    page.wait_for_function("document.querySelector('main').textContent.includes('600 particle records received')")
                    page.get_by_role('button',name='Replay first batch safely',exact=True).click()
                    assert '600 particle records received' in page.inner_text('main')
                    page.get_by_role('link',name='Population charts & bean photos',exact=True).click()
                    page.wait_for_function("document.querySelector('[data-population-chart] path') !== null")
                    report_path=page.url
                    check_inspector(page,artifacts,'run')
                    page.locator('[data-population-basis]').select_option('simulated')
                    page.locator('[data-population-scale]').select_option('count')
                    page.get_by_role('link',name='Simulated Pass-through',exact=True).click()
                    page.wait_for_function("document.querySelector('.bean-gallery img')?.naturalWidth > 0")
                    page.screenshot(path=str(artifacts/'populations-desktop.png'),full_page=True)
                    for width in (768,390):
                        page.set_viewport_size({'width':width,'height':1000})
                        for name,target in [('recipe',recipe_path),('populations',report_path)]:
                            page.goto(target);page.wait_for_function("document.querySelector('[data-population-chart] path') !== null")
                            assert not page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
                            page.screenshot(path=str(artifacts/f'{name}-{width}.png'),full_page=True)
                    page.set_viewport_size({'width':1440,'height':1050});page.goto(job_path)
                    page.get_by_role('button',name='Record transition',exact=True).click()
                    assert page.url.endswith('/weigh')
                    for stream in ('selected','pass_through','remainder'):
                        assert page.locator(f'[name="gross_{stream}"]').input_value()
                        assert not page.locator(f'[name="weighed_{stream}"]').is_checked()
                    for field,value in [('selected','1.500'),('pass_through','19.400'),('remainder','0.500')]:
                        page.locator(f'[name="gross_{field}"]').fill(value)
                        page.locator(f'[name="weighed_{field}"]').check()
                    assert 'Net beans: 1.000 kg' in page.inner_text('main')
                    page.screenshot(path=str(artifacts/'weighing-desktop.png'),full_page=True)
                    page.set_viewport_size({'width':390,'height':1000})
                    assert not page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
                    page.screenshot(path=str(artifacts/'weighing-390.png'),full_page=True)
                    page.get_by_role('button',name='Reconcile',exact=True).click()
                    page.get_by_role('heading',name='Review reconciliation',exact=True).wait_for()
                    assert '+0.100 kg' in page.inner_text('main')
                    assert not page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
                    page.screenshot(path=str(artifacts/'weighing-review-390.png'),full_page=True)
                    page.locator('[name="accept_review"]').check()
                    page.get_by_role('button',name='Accept reconciliation & release outputs',exact=True).click()
                    page.get_by_role('heading',name='Completed',exact=True).wait_for()
                    page.get_by_role('link',name='Ready Selected',exact=True).first.click()
                    assert '1 kg' in page.inner_text('main')
                    assert 'Bean count estimated from container weighing' in page.inner_text('main')
                    page.get_by_role('link',name='Population charts & bean photos',exact=True).click()
                    assert 'population=selected' in page.url
                    page.wait_for_function("document.querySelector('[data-population-chart] path') !== null")
                    first_report=json.loads(page.locator('#population-data').text_content())
                    resort_destinations=[]
                    for label in ('Second pass Selected','Second pass Pass-through'):
                        page.goto(url+'/containers')
                        page.locator('.container-create > summary').click()
                        page.locator('[name="label"]').fill(label)
                        place=page.locator('[name="location_id"] option').nth(1).get_attribute('value')
                        page.locator('[name="location_id"]').select_option(place)
                        page.locator('[name="capacity"]').fill('30');page.locator('[name="tare_mass"]').fill('0.5')
                        page.get_by_role('button',name='Create container',exact=True).click()
                        assert page.url == url+'/containers'
                        resort_destinations.append(page.locator('tr[data-container-id]').filter(has_text=label).get_attribute('data-container-id'))
                    page.goto(url+'/jobs/new?recipe_id='+selected_recipe)
                    source=page.locator('select[name="portion_id"] option').filter(has_text='Ready Selected').get_attribute('value')
                    page.locator('[name="portion_id"]').select_option(source)
                    assert float(page.locator('[name="planned_mass"]').input_value())==1
                    assert page.locator('[name="portion_id"] option:checked').get_attribute('data-population-mean-mg')
                    for field,cid in zip(('selected_id','pass_through_id'),resort_destinations):page.locator('[name="'+field+'"]').select_option(cid)
                    page.locator('[name="ready"]').check();page.get_by_role('button',name='Validate & reserve',exact=True).click()
                    page.get_by_role('heading',name='Further sorting pass',exact=True).wait_for();second_job=page.url
                    page.locator('[name="count"]').fill('300');page.get_by_role('button',name='Start simulation workflow',exact=True).click()
                    page.get_by_role('button',name='Generate all remaining particles',exact=True).click()
                    page.wait_for_function("document.querySelector('main').textContent.includes('300 particle records received')")
                    page.get_by_role('button',name='Replay first batch safely',exact=True).click()
                    page.get_by_role('link',name='Population charts & bean photos',exact=True).click()
                    second_report=json.loads(page.locator('#population-data').text_content())
                    for feature,bins in first_report['populations']['simulated_selected']['histograms'].items():
                        for i,value in enumerate(bins):
                            if float(value)==0:assert float(second_report['populations']['input']['histograms'][feature][i])==0
                    page.screenshot(path=str(artifacts/'further-sort-populations-desktop.png'),full_page=True)
                    page.goto(second_job);page.get_by_role('button',name='Record transition',exact=True).click()
                    assert page.url.endswith('/weigh')
                    for stream in ('selected','pass_through','remainder'):
                        assert page.locator('[name="gross_'+stream+'"]').input_value()
                        page.locator('[name="weighed_'+stream+'"]').check()
                    page.get_by_role('button',name='Reconcile',exact=True).click();page.locator('[name="accept_review"]').check()
                    page.get_by_role('button',name='Accept reconciliation & release outputs',exact=True).click()
                    page.get_by_role('heading',name='Completed',exact=True).wait_for()
                    # Collect a third pass into an existing population.
                    page.goto(url+'/containers')
                    page.locator('.container-create > summary').click()
                    page.locator('[name="label"]').fill('Occupied collection misses')
                    place=page.locator('[name="location_id"] option').nth(1).get_attribute('value')
                    page.locator('[name="location_id"]').select_option(place);page.locator('[name="capacity"]').fill('30');page.locator('[name="tare_mass"]').fill('0.5')
                    page.get_by_role('button',name='Create container',exact=True).click()
                    assert page.url == url+'/containers'
                    empty_collection=page.locator('tr[data-container-id]').filter(has_text='Occupied collection misses').get_attribute('data-container-id')
                    page.goto(url+'/jobs/new?recipe_id='+selected_recipe)
                    source_option=page.locator('select[name="portion_id"] option').filter(has_text='Second pass Selected')
                    source_container=source_option.get_attribute('data-source-container');page.locator('[name="portion_id"]').select_option(source_option.get_attribute('value'))
                    selected_picker=page.locator('[name="selected_id"]');pass_picker=page.locator('[name="pass_through_id"]')
                    occupied=selected_picker.locator('option').filter(has_text='Ready Pass-through').get_attribute('value')
                    selected_picker.select_option(occupied)
                    assert pass_picker.locator('option[value="'+occupied+'"]').evaluate('(option) => option.disabled')
                    assert pass_picker.locator('option[value="'+occupied+'"]').get_attribute('hidden') is not None
                    selected_picker.select_option('');assert not pass_picker.locator('option[value="'+occupied+'"]').evaluate('(option) => option.disabled')
                    pass_picker.select_option(empty_collection);assert selected_picker.locator('option[value="'+empty_collection+'"]').evaluate('(option) => option.disabled')
                    selected_picker.select_option(occupied)
                    assert selected_picker.locator('option[value="'+source_container+'"]').evaluate('(option) => option.disabled')
                    page.locator('[name="ready"]').check();page.get_by_role('button',name='Validate & reserve',exact=True).click()
                    page.locator('[name="count"]').fill('300');page.get_by_role('button',name='Start simulation workflow',exact=True).click()
                    page.get_by_role('button',name='Generate all remaining particles',exact=True).click()
                    page.wait_for_function("document.querySelector('main').textContent.includes('300 particle records received')")
                    page.get_by_role('button',name='Record transition',exact=True).click()
                    assert page.url.endswith('/weigh')
                    assert 'Starting contents: 18.9 kg' in page.inner_text('main')
                    assert float(page.locator('[name="gross_selected"]').input_value())>19.4
                    for stream in ('selected','pass_through','remainder'):page.locator('[name="weighed_'+stream+'"]').check()
                    page.get_by_role('button',name='Reconcile',exact=True).click();page.locator('[name="accept_review"]').check()
                    page.get_by_role('button',name='Accept reconciliation & release outputs',exact=True).click();page.get_by_role('heading',name='Completed',exact=True).wait_for()
                    page.goto(url+'/containers/'+occupied);page.get_by_role('link',name='Population charts & bean photos',exact=True).click()
                    page.get_by_role('heading',name='Combined container population',exact=True).wait_for()
                    combined=json.loads(page.locator('#population-data').text_content());assert combined['combination'] is True
                    for feature,bins in combined['populations']['expected_selected']['histograms'].items():
                        before=combined['populations']['input']['histograms'][feature];added=combined['populations']['expected_pass_through']['histograms'][feature]
                        assert all(abs(float(total)-float(old)-float(new))<1e-7 for total,old,new in zip(bins,before,added))
                    for key in ('input','pass_through'):page.locator('[data-population-toggle="'+key+'"]').uncheck()
                    counts=combined['populations']['expected_selected']['histograms']['lab_l'];bucket=max(range(len(counts)),key=lambda i:float(counts[i]))
                    inspected=inspect_chart(page,'lab_l',bucket);assert inspected['collection'] is True and inspected['count_unit']=='estimated beans'
                    page.screenshot(path=str(artifacts/'occupied-combined-desktop.png'),full_page=True)
                    page.set_viewport_size({'width':390,'height':1000});assert not page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
                    page.screenshot(path=str(artifacts/'occupied-combined-390.png'),full_page=True)
                    assert not errors,errors
                    result={'workflow':'reference sorting, further passes, exclusive destination choices, occupied collection, total gross weighing, combined population lineage and joint photo/colour inspection passed',
                            'real_reference':bool(os.environ.get('BEANOSORT_REFERENCE_BUNDLE')),'viewports':[1440,768,390],'page_errors':errors}
                    (artifacts/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result));browser.close()
            finally:
                server.terminate();server.wait(timeout=15)


if __name__=='__main__':main()
