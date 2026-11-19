"""Exercise manual controls against a fake HTTP machine on a disposable database."""
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

from playwright.sync_api import expect, sync_playwright
from fake_sorter import FakeMachine, machine_server

ROOT = Path(__file__).resolve().parents[1]


def main():
    artifacts = ROOT / 'var/qa/sorter-controls'; artifacts.mkdir(parents=True, exist_ok=True)
    machine = FakeMachine()
    with machine_server(machine) as machine_port, tempfile.TemporaryDirectory(dir=ROOT/'var', prefix='sorter-browser-') as directory:
        tmp = Path(directory)
        with socket.socket() as sock: sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        url = f'http://127.0.0.1:{port}'; password = secrets.token_urlsafe(24)
        env = {**os.environ, 'BEANOSORT_DATABASE_URL': 'sqlite:///'+str(tmp/'test.db'),
            'BEANOSORT_ENVIRONMENT': 'simulation', 'BEANOSORT_SECRET': secrets.token_urlsafe(48),
            'BEANOSORT_ADMIN_PASSWORD': password, 'BEANOSORT_SORTER_HOST': '127.0.0.1',
            'BEANOSORT_SORTER_PORT': str(machine_port)}
        subprocess.run([str(ROOT/'.venv/bin/python'), '-m', 'beanosort.cli', 'init', '--username', 'browser'],
            cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
        with (artifacts/'server.log').open('w') as log:
            server = subprocess.Popen([str(ROOT/'.venv/bin/python'), '-m', 'uvicorn', 'beanosort.web:create_app',
                '--factory', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    try: urllib.request.urlopen(url+'/health', timeout=1).close(); break
                    except OSError: time.sleep(.1)
                else: raise RuntimeError('Preview did not start')
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={'width':1440, 'height':1050})
                    page = context.new_page(); errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    def login(target):
                        target.goto(url+'/login')
                        target.get_by_label('Username', exact=True).fill('browser')
                        target.get_by_label('Password', exact=True).fill(password)
                        target.get_by_role('button', name='Sign in', exact=True).click()
                        target.get_by_role('link', name='Sorter Controls', exact=True).click()
                    login(page)
                    panel = page.frame_locator('[data-sorter-frame]')
                    expect(panel.locator('#connection-label')).to_have_text('ESP32 CONNECTED')
                    assert all(body is None for _,body in machine.calls), 'Viewing controls sent a command'
                    expect(panel.locator('#toggle-conveyor')).to_be_disabled()
                    page.get_by_role('button', name='Save connection', exact=True).click()
                    expect(page.locator('[name=port]')).to_have_value(str(machine_port))
                    expect(panel.locator('#connection-label')).to_have_text('ESP32 CONNECTED')
                    panel.get_by_role('button', name='Load Candidate 3', exact=True).click()
                    expect(panel.locator('#vibration-duty')).to_have_value('38')
                    expect(panel.locator('#conveyor-speed')).to_have_value('20')
                    expect(panel.locator('#trigger-frequency')).to_have_value('59.5')
                    panel.locator('#vibration-duty').fill('37'); panel.locator('#apply-vibration').focus()
                    machine.state['vibration_motor']['enabled'] = True
                    expect(panel.locator('#vibration-state')).to_have_text('ON')
                    expect(panel.locator('#vibration-duty')).to_have_value('37')
                    machine.state['vibration_motor']['enabled'] = False
                    panel.get_by_role('button', name='Take control', exact=True).click()
                    expect(panel.locator('#control-message')).to_contain_text('You have control')
                    for button in ('#apply-trigger', '#apply-strobe', '#toggle-trigger', '#toggle-strobe', '#toggle-vibration', '#toggle-conveyor'):
                        panel.locator(button).click()
                    expect(panel.locator('#vibration-state')).to_have_text('ON')
                    assert machine.state['vibration_motor']['duty_percent'] == 37
                    assert machine.state['conveyor']['enabled'] and machine.state['conveyor']['direction'] == 'reverse'
                    assert machine.state['triggers']['enabled'] and machine.state['strobe']['enabled']
                    panel.locator('#test-valves').click()
                    panel.locator('#stop-valves').click()
                    panel.locator('#stop-all').click()
                    expect(panel.locator('#trigger-state')).to_have_text('OFF')
                    expect(panel.locator('#vibration-state')).to_have_text('OFF')
                    panel.locator('#pulse-trigger').click()
                    panel.locator('#pulse-strobe').click()
                    panel.locator('#apply-high-speed').click()
                    panel.locator('#toggle-high-speed').click()
                    expect(panel.locator('#high-speed-state')).to_have_text('ON')
                    panel.locator('#toggle-high-speed').click()
                    expect(panel.locator('#high-speed-state')).to_have_text('OFF')
                    second = browser.new_context(); other = second.new_page(); login(other)
                    other_panel = other.frame_locator('[data-sorter-frame]')
                    expect(other_panel.locator('#control-message')).to_contain_text('exclusive control')
                    expect(other_panel.locator('#take-control')).to_be_disabled()
                    other_panel.locator('#stop-all').click()
                    assert machine.state['lease']['active']
                    second.close()
                    panel.locator('#release-control').click()
                    expect(panel.locator('#take-control')).to_be_enabled()
                    machine.offline = True
                    expect(panel.locator('#connection-label')).to_have_text('MACHINE UNREACHABLE')
                    expect(panel.locator('#toggle-conveyor')).to_be_disabled()
                    expect(panel.locator('#stop-all')).to_be_enabled()
                    machine.offline = False
                    expect(panel.locator('#connection-label')).to_have_text('ESP32 CONNECTED')
                    for width in (1440,768,390):
                        page.set_viewport_size({'width':width,'height':1050})
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                        assert panel.locator('html').evaluate('el => el.scrollWidth <= window.innerWidth + 1')
                        page.screenshot(path=str(artifacts/f'controls-{width}.png'), full_page=True)
                    panel.locator('#take-control').click()
                    expect(panel.locator('#control-message')).to_contain_text('You have control')
                    page.get_by_role('link',name='Containers',exact=True).click()
                    for _ in range(50):
                        if not machine.state['lease']['active']: break
                        time.sleep(.1)
                    assert not machine.state['lease']['active'], 'Navigation did not release control'
                    assert not errors, errors
                    browser.close()
                with sqlite3.connect(tmp/'test.db') as connection:
                    assert connection.execute('SELECT COUNT(*) FROM ledger').fetchone()[0] == 0
                    assert connection.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == 0
                    assert connection.execute("SELECT COUNT(*) FROM audit WHERE action='sorter_controls_connection'").fetchone()[0] == 1
                result = {'fake_machine_only':True,'view_does_not_actuate':True,'all_manual_controls':True,
                    'candidate_and_drafts_preserved':True,'exclusive_control':True,'stop_without_lease':True,
                    'offline_recovery':True,'navigation_releases_control':True,'stock_unchanged':True,
                    'viewports':[1440,768,390],'page_errors':errors}
                (artifacts/'result.json').write_text(json.dumps(result,indent=2)+'\n'); print(json.dumps(result))
            finally: server.terminate();server.wait(timeout=20)


if __name__ == '__main__': main()
