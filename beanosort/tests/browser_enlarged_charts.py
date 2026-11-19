"""GET-only browser check against a supplied evidence URL; no machine commands.

QA_URL, QA_EVIDENCE_PATH and QA_PASSWORD select an existing/read-only preview.
"""
import json
import os
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from playwright.sync_api import sync_playwright,expect


def main():
    root=Path(__file__).resolve().parents[1];out=Path(os.environ.get('QA_CHART_ARTIFACTS',root/'var/qa/enlarged-charts'));out.mkdir(parents=True,exist_ok=True)
    url=os.environ['QA_URL'];path=os.environ['QA_EVIDENCE_PATH'];requests=[];errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050})
        page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:requests.append((r.method,r.url)))
        page.goto(url+'/login');page.get_by_label('Username',exact=True).fill(os.environ.get('QA_USERNAME','admin'))
        page.get_by_label('Password',exact=True).fill(os.environ['QA_PASSWORD']);page.get_by_role('button',name='Sign in',exact=True).click()
        page.goto(url+path);page.wait_for_selector('[data-population-chart] path')
        original_url=page.url;original=json.loads(page.locator('#population-data').text_content())
        original_charts=page.locator('[data-population-chart]').count();feature='lab_l'
        for t in page.locator('[data-population-toggle]').all():
            t.set_checked(t.get_attribute('data-population-toggle')=='pass_through')
        page.locator('[data-population-scale]').select_option('count')
        basis=page.locator('[data-population-basis]').input_value()
        pop=original['populations'][basis+'_pass_through']
        edges=original['definitions'][feature]['edges'];value=float(os.environ.get('QA_FEATURE_VALUE','17.5'))
        bucket=next(i for i,(a,b) in enumerate(zip(edges,edges[1:])) if a<=value<b)
        chart=page.locator('[data-population-chart=lab_l]');chart.focus()
        for _ in range(bucket):chart.press('ArrowRight')
        with page.expect_response(lambda r:'/inspect?' in r.url and r.request.resource_type=='fetch') as response:chart.press('Enter')
        inspected=response.value.json();assert inspected['items']
        page.wait_for_function("document.querySelector('[data-population-inspector]').getAttribute('aria-busy')==='false'")
        original_width=chart.bounding_box()['width'];original_inspector=page.locator('[data-population-inspector]').bounding_box()['width']
        link=page.locator('[data-chart-link=lab_l]');assert 'view_bucket='+str(bucket) in link.get_attribute('href')
        link.click();page.wait_for_selector('main.population-focus-page')
        expect(page.locator('[data-population-chart]')).to_have_count(1)
        assert page.locator('[data-population-chart]').get_attribute('data-population-chart')=='lab_l'
        expect(page.locator('[data-population-scale]')).to_have_value('count')
        expect(page.locator('[data-population-toggle=input]')).not_to_be_checked()
        page.wait_for_function("document.querySelector('[data-population-inspector]').getAttribute('aria-busy')==='false'")
        assert json.loads(page.locator('#population-data').text_content())==original
        expect(page.locator('.inspection-beans article').first).to_be_visible()
        assert page.locator('.bean-colour-swatch').count()>0
        page.wait_for_function("[...document.querySelectorAll('.inspection-beans img')].some(i=>i.complete&&i.naturalWidth>0)")
        chart=page.locator('[data-population-chart=lab_l]')
        assert chart.bounding_box()['width']>original_width*1.5
        assert page.locator('[data-population-inspector]').bounding_box()['width']>original_inspector
        widths=[]
        for width,height in ((1440,1050),(1920,1080),(768,1024),(390,844)):
            page.set_viewport_size({'width':width,'height':height});page.wait_for_timeout(150)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
            chart.focus()
            with page.expect_response(lambda r:'/inspect?' in r.url and r.request.resource_type=='fetch') as selected:chart.press('Enter')
            assert selected.value.json()['matching_count']==inspected['matching_count']
            page.wait_for_function("document.querySelector('[data-population-inspector]').getAttribute('aria-busy')==='false'")
            plot=page.locator('.population-charts').bounding_box();photos=page.locator('[data-population-inspector]').bounding_box()
            available=page.locator('[data-population-explorer]').bounding_box()['width']
            assert abs(plot['width']-available)<2 and abs(photos['width']-available)<2
            assert photos['y']>=plot['y']+plot['height'], 'Photos must be beneath the full-width chart'
            assert page.locator('[data-population-explorer]').evaluate("e=>e.firstElementChild.classList.contains('population-charts')")
            if width>=1440 and page.locator('.inspection-beans article').count()>1:
                cards=page.locator('.inspection-beans article');a=cards.nth(0).bounding_box();b=cards.nth(1).bounding_box()
                assert abs(a['y']-b['y'])<2 and b['x']>a['x'], 'Desktop photos should use a row of cards'
            widths.append({'viewport':width,'chart_width':round(chart.bounding_box()['width']),
                'chart_height':round(chart.bounding_box()['height']),'photos_below_chart':True,'full_width':True})
            page.screenshot(path=str(out/f'chart-{width}.png'),full_page=True)
        page.set_viewport_size({'width':1440,'height':1050})
        page.get_by_role('button',name='Clear bean inspection',exact=True).click()
        expect(page.locator('[data-population-inspector]')).to_be_visible()
        expect(page.locator('.inspection-beans article')).to_have_count(0)
        chart.focus();chart.press('Enter')
        page.wait_for_function("document.querySelector('[data-population-inspector]').getAttribute('aria-busy')==='false'")
        page.get_by_role('link',name='← Back to population evidence',exact=True).click()
        expect(page.locator('[data-population-chart]')).to_have_count(original_charts)
        expect(page.locator('main.population-focus-page')).to_have_count(0)
        assert 'chart' not in parse_qs(urlsplit(page.url).query)
        for key,values in parse_qs(urlsplit(original_url).query).items():assert parse_qs(urlsplit(page.url).query)[key]==values
        expect(page.locator('[data-population-scale]')).to_have_value('count')
        expect(page.locator('[data-population-toggle=input]')).not_to_be_checked()
        page.wait_for_function("document.querySelector('[data-population-inspector]').getAttribute('aria-busy')==='false'")
        assert json.loads(page.locator('#population-data').text_content())==original
        assert not errors,errors
        assert [(m,u) for m,u in requests if m!='GET']==[('POST',url+'/login')]
        browser.close()
    (out/'browser-result.json').write_text(json.dumps({'passed':True,'charts_on_overview':original_charts,'focused_charts':1,
        'same_report_and_evidence_scope':True,'photos_and_colours_loaded':True,'view_choices_and_interval_restored_on_return':True,
        'sizes':widths,'javascript_errors':errors,'operational_posts':0,'hardware_commands':0},indent=2)+'\n')
    print('Enlarged-chart browser checks passed (login and GET requests only)')


if __name__=='__main__':main()
