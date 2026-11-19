"""Final-product packaging, always with a disposable simulation database."""
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

from browser_material_smoke import BOOTSTRAP, PYTHON, ROOT


def main():
    artifacts = ROOT / 'var/qa/product-packaging-browser'
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='product-browser-', dir=ROOT/'var') as temporary:
        tmp = Path(temporary)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        password = secrets.token_urlsafe(24)
        env = {**os.environ, 'BEANOSORT_DATABASE_URL': 'sqlite:///' + str(tmp/'test.db'),
               'BEANOSORT_ENVIRONMENT': 'simulation', 'BEANOSORT_SECRET': secrets.token_urlsafe(48),
               'BEANOSORT_PUBLIC_URL': url, 'BEANOSORT_ADMIN_PASSWORD': password,
               'MATERIAL_FIXTURE': str(tmp/'fixture.json')}
        bootstrap = BOOTSTRAP + "\nworkflow.set_hold(uid,fixture['D']['portion'],'Review before sale')\n"
        subprocess.run([str(PYTHON), '-c', bootstrap], env=env, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        fixture = json.loads((tmp/'fixture.json').read_text())
        with (artifacts/'server.log').open('w') as log:
            server = subprocess.Popen([str(PYTHON), '-m', 'uvicorn', 'beanosort.web:create_app', '--factory',
                '--host', '127.0.0.1', '--port', str(port)], env=env, cwd=ROOT, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    try:
                        urllib.request.urlopen(url+'/health', timeout=1).close()
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('Disposable server did not start')
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={'width': 1440, 'height': 1050})
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(url+'/login')
                    page.locator('[name=username]').fill('browser')
                    page.locator('[name=password]').fill(password)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.goto(url+'/handling')
                    page.get_by_role('link', name='Package as product', exact=True).click()
                    page.get_by_role('heading', name='Package as product', exact=True).wait_for()
                    assert page.get_by_label('Scale reading reflects').count() == 1
                    assert page.get_by_label('Scale reading', exact=True).get_attribute('placeholder') is None
                    for label in ('D', 'E'):
                        option = page.locator(f'select[name=container_id] option[value="{fixture[label]["container"]}"]')
                        assert option.evaluate('el=>el.disabled')
                    source = fixture['B']['container']
                    page.locator('[name=container_id]').select_option(source)
                    page.locator('[name=weighing_basis]').select_option('net')
                    page.locator('[name=weight]').fill('0.021')
                    assert not page.locator('[name=weight]').evaluate('el=>el.validity.valid')
                    page.locator('[name=weighing_basis]').select_option('gross')
                    page.locator('[name=weight]').fill('0.09')
                    assert not page.locator('[name=weight]').evaluate('el=>el.validity.valid')
                    page.locator('[name=weight]').fill('0.112')
                    assert page.locator('[name=weight]').evaluate('el=>el.validity.valid')
                    assert 'Final product leaving inventory: 0.008 kg' in page.locator('[data-material-preview]').inner_text()
                    for width in (1440, 768, 390):
                        page.set_viewport_size({'width': width, 'height': 1050})
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(artifacts/f'form-{width}.png'), full_page=True)
                    stale = context.new_page()
                    stale.goto(url+'/handling/package?container_id='+source)
                    stale.locator('[name=weighing_basis]').select_option('net')
                    stale.locator('[name=weight]').fill('0.01')
                    page.get_by_role('button', name='Record final product', exact=True).click()
                    page.get_by_role('heading', name='Final product recorded', exact=True).wait_for()
                    assert '0.008 kg packaged as Final product' in page.inner_text('main')
                    partial = page.url
                    stale.get_by_role('button', name='Record final product', exact=True).click()
                    stale.get_by_role('heading', name='Record not changed', exact=True).wait_for()
                    assert 'changed' in stale.inner_text('main')
                    stale.close()
                    page.goto(url+'/handling/package?container_id='+source)
                    page.locator('[name=weighing_basis]').select_option('net')
                    page.locator('[name=weight]').fill('0')
                    assert 'Final product leaving inventory: 0.012 kg' in page.locator('[data-material-preview]').inner_text()
                    page.get_by_role('button', name='Record final product', exact=True).click()
                    page.get_by_role('heading', name='Final product recorded', exact=True).wait_for()
                    assert 'Container recorded empty and available for reuse' in page.inner_text('main')
                    complete = page.url
                    # Directly weigh removed contents without reweighing the source.
                    source_removed = fixture['A']['container']
                    page.goto(url+'/handling/package?container_id='+source_removed)
                    page.get_by_label('Scale reading reflects').select_option(label='Mass of contents removed')
                    page.get_by_label('Scale reading', exact=True).fill('0')
                    assert not page.locator('[name=weight]').evaluate('el=>el.validity.valid')
                    page.get_by_label('Scale reading', exact=True).fill('0.011')
                    assert not page.locator('[name=weight]').evaluate('el=>el.validity.valid')
                    page.get_by_label('Scale reading', exact=True).fill('0.004')
                    assert page.locator('[name=weight]').evaluate('el=>el.validity.valid')
                    assert 'Calculated contents remaining: 0.006 kg' in page.locator('[data-material-preview]').inner_text()
                    assert 'Final product leaving inventory: 0.004 kg' in page.locator('[data-material-preview]').inner_text()
                    for width in (1440, 768, 390):
                        page.set_viewport_size({'width': width, 'height': 1050})
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(artifacts/f'removed-form-{width}.png'), full_page=True)
                    page.get_by_role('button', name='Record final product', exact=True).click()
                    page.get_by_role('heading', name='Final product recorded', exact=True).wait_for()
                    assert '0.004 kg packaged as Final product' in page.inner_text('main')
                    assert 'calculated contents remaining' in page.inner_text('main').lower()
                    removed_partial = page.url
                    page.goto(url+'/handling/package?container_id='+source_removed)
                    page.get_by_label('Scale reading reflects').select_option('removed')
                    page.get_by_label('Scale reading', exact=True).fill('0.006')
                    assert 'The source portion will close' in page.locator('[data-material-preview]').inner_text()
                    page.get_by_role('button', name='Record final product', exact=True).click()
                    page.get_by_role('heading', name='Final product recorded', exact=True).wait_for()
                    assert 'Container recorded empty and available for reuse' in page.inner_text('main')
                    removed_complete = page.url
                    for width in (1440, 768, 390):
                        page.set_viewport_size({'width': width, 'height': 1050})
                        for name, path in (('partial', partial), ('complete', complete),
                                           ('removed-partial', removed_partial), ('removed-complete', removed_complete),
                                           ('history', url+'/handling')):
                            page.goto(path)
                            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                            page.screenshot(path=str(artifacts/f'{name}-{width}.png'), full_page=True)
                    assert not errors, errors
                    browser.close()
                with sqlite3.connect(tmp/'test.db') as db:
                    assert db.execute('SELECT SUM(delta_mg) FROM ledger').fetchone()[0] == 70000
                    assert db.execute('SELECT COUNT(*) FROM product_lots').fetchone()[0] == 0
                    assert db.execute('SELECT COUNT(*) FROM packs').fetchone()[0] == 0
                    assert db.execute("SELECT COUNT(*) FROM operations WHERE kind='product_packaging' AND status='completed'").fetchone()[0] == 4
                    assert db.execute('SELECT COUNT(*) FROM assignments WHERE container_id=? AND active', (source,)).fetchone()[0] == 0
                    assert db.execute('SELECT COUNT(*) FROM assignments WHERE container_id=? AND active', (source_removed,)).fetchone()[0] == 0
                    assert db.execute("SELECT COUNT(*) FROM measurements m JOIN handling_plans p ON p.operation_id=m.operation_id WHERE json_extract(p.data,'$.weighing_basis')='removed' AND m.role!='packaged_net'").fetchone()[0] == 0
                    assert db.execute("SELECT COUNT(*) FROM stations WHERE name='Packaging desk'").fetchone()[0] == 1
                result = {'gross_partial_packaging': 'passed', 'net_complete_packaging': 'passed',
                    'removed_partial_and_complete_packaging': 'passed', 'calculated_remainder_without_remaining_measurement': 'passed',
                    'updated_labels_and_no_placeholder': 'passed',
                    'held_and_empty_sources_disabled': 'passed', 'invalid_remaining_weight_rejected': 'passed',
                    'stale_form_rejected': 'passed', 'packet_records_created': 0, 'exact_remaining_inventory_mg': 70000,
                    'viewports': [1440, 768, 390], 'page_errors': errors}
                (artifacts/'result.json').write_text(json.dumps(result, indent=2)+'\n')
                print(json.dumps(result))
            finally:
                server.terminate()
                server.wait(timeout=20)


if __name__ == '__main__':
    main()
