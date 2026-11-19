"""Optional browser acceptance, always on a disposable simulation database.

Run with an interpreter containing Playwright; the application uses .venv/bin/python.
No existing database, controller or user credentials are accessed.
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


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/bin/python"


def main():
    artifacts = ROOT / "var/qa"
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="browser-", dir=ROOT / "var") as temporary:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        password = secrets.token_urlsafe(24)
        env = {**os.environ, "BEANOSORT_DATABASE_URL": "sqlite:///" + str(Path(temporary) / "test.db"),
               "BEANOSORT_ENVIRONMENT": "simulation", "BEANOSORT_SECRET": secrets.token_urlsafe(48),
               "BEANOSORT_ADMIN_PASSWORD": password, "BEANOSORT_PUBLIC_URL": url}
        for command in ("init", "demo"):
            subprocess.run([str(PYTHON), "-m", "beanosort.cli", command, "--username", "browser"],
                           env=env, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        with (artifacts / "server.log").open("w") as log:
            server = subprocess.Popen([str(PYTHON), "-m", "uvicorn", "beanosort.web:create_app", "--factory",
                                       "--host", "127.0.0.1", "--port", str(port)], env=env, cwd=ROOT, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    try:
                        with urllib.request.urlopen(url + "/health", timeout=1):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Preview did not start; inspect var/qa/server.log")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1440, "height": 1050})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(url + "/login")
                    page.get_by_label("Username", exact=True).fill("browser")
                    page.get_by_label("Password", exact=True).fill(password)
                    page.get_by_role("button", name="Sign in", exact=True).click()
                    page.get_by_role("heading", name="Stock & sorting jobs").wait_for()
                    page.screenshot(path=str(artifacts / "stock-desktop.png"), full_page=True)
                    page.get_by_role("link", name="Simulation Settings", exact=True).click()
                    assert page.locator('[name="mean_mass_g"]').input_value() == "0.15"
                    assert page.locator('[name="capture_percent"]').input_value() == "99.00"
                    for mean in ("0.2", "0.15"):
                        page.locator('[name="mean_mass_g"]').fill(mean)
                        page.locator('[name="reason"]').fill("Browser test of versioned assumptions")
                        page.get_by_role("button", name="Save new settings version").click()
                    assert "Current defaults · version 3" in page.inner_text("main")
                    page.screenshot(path=str(artifacts / "simulation-settings-desktop.png"), full_page=True)
                    page.get_by_role("link", name="Prepare sorting", exact=True).click()
                    source = page.locator('select[name="portion_id"] option').filter(has_text="Ready source").get_attribute("value")
                    page.locator('[name="portion_id"]').select_option(source)
                    assert page.locator('[name="planned_mass"]').input_value() == '20.000'
                    assert "133,333" in page.locator('[data-count-preview]').inner_text()
                    page.locator('[name="planned_mass"]').fill('1.000')
                    alternative = page.locator(f'select[name="portion_id"] option:not([value=""]):not([value="{source}"])').first
                    alternative_id = alternative.get_attribute('value')
                    alternative_mass = alternative.get_attribute('data-available-mass')
                    page.locator('[name="portion_id"]').select_option(alternative_id)
                    assert page.locator('[name="planned_mass"]').input_value() == alternative_mass
                    page.locator('[name="portion_id"]').select_option(source)
                    assert page.locator('[name="planned_mass"]').input_value() == '20.000'
                    page.locator('[name="planned_mass"]').fill("20")
                    assert "133,333" in page.locator('[data-count-preview]').inner_text()
                    original_destinations = {}
                    for name, label in (("selected_id", "Ready Selected"), ("pass_through_id", "Ready Pass-through")):
                        value = page.locator(f'select[name="{name}"] option').filter(has_text=label).get_attribute("value")
                        original_destinations[name] = value
                        page.locator(f'select[name="{name}"]').select_option(value)
                    page.locator('[name="ready"]').check()
                    page.get_by_role("button", name="Validate & reserve").click()
                    original_job_path = page.url
                    page.get_by_role("link", name="Edit prepared job", exact=True).click()
                    assert page.locator('[name="portion_id"]').input_value() == source
                    assert page.locator('[name="selected_id"]').input_value() == original_destinations['selected_id']
                    assert page.locator('[name="planned_mass"]').input_value() == '20'
                    prepared_viewports = []
                    for width in (768, 390):
                        page.set_viewport_size({"width": width, "height": 1000})
                        overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                        assert not overflow
                        prepared_viewports.append({"page": "prepared-job-edit", "width": width, "horizontal_overflow": overflow})
                        page.screenshot(path=str(artifacts / f"prepared-job-edit-{width}.png"), full_page=True)
                    page.set_viewport_size({"width": 1440, "height": 1050})
                    page.locator('[name="planned_mass"]').fill('10')
                    page.locator('[name="selected_id"]').select_option(original_destinations['pass_through_id'])
                    page.locator('[name="pass_through_id"]').select_option(original_destinations['selected_id'])
                    page.locator('[name="ready"]').check()
                    page.locator('[name="confirm_unstarted"]').check()
                    page.get_by_role("button", name="Save job changes", exact=True).click()
                    corrected_job_path = page.url
                    page.get_by_role('link', name='Edit prepared job', exact=True).click()
                    assert page.locator('[name="planned_mass"]').input_value() == '10'
                    page.goto(corrected_job_path)
                    assert corrected_job_path != original_job_path
                    assert '10 kg intended for processing' in page.inner_text('main')
                    page.goto(original_job_path)
                    page.get_by_role('link', name='Open corrected job', exact=True).click()
                    assert page.url == corrected_job_path
                    page.locator('summary').filter(has_text='Delete this prepared job').click()
                    page.locator('[name="confirm_unstarted"]').check()
                    page.get_by_role('button', name='Delete prepared job & release containers', exact=True).click()
                    page.get_by_role('link', name='Prepare sorting', exact=True).click()
                    # The same source and bins are available immediately after deletion.
                    page.locator('[name="portion_id"]').select_option(source)
                    page.locator('[name="planned_mass"]').fill('20')
                    for name, value in original_destinations.items():
                        page.locator(f'[name="{name}"]').select_option(value)
                    page.locator('[name="ready"]').check()
                    page.get_by_role('button', name='Validate & reserve', exact=True).click()
                    assert page.locator('[name="count"]').get_attribute("placeholder") == "133333"
                    page.locator('[name="count"]').fill("500")
                    page.get_by_role("button", name="Start simulation workflow").click()
                    assert page.get_by_role('link', name='Edit prepared job', exact=True).count() == 0
                    page.get_by_role("button", name="Generate all remaining particles").click()
                    page.wait_for_function("document.querySelector('main').textContent.includes('500 particle records received')")
                    assert "99.00" in page.inner_text("main")
                    distributions = page.locator('details[data-lazy-details]')
                    assert distributions.locator('table').count() == 0
                    with page.expect_response(lambda response: response.url.endswith('/distributions')) as details_response:
                        distributions.locator('summary').click()
                    assert details_response.value.status == 200
                    distributions.locator('table').first.wait_for()
                    assert '500 committed particle records' in distributions.inner_text()
                    distributions.locator('summary').click()
                    page.locator('[data-received-age]').evaluate_all("nodes => nodes.forEach(n => n.dataset.receivedAge = '9999')")
                    page.wait_for_function("[...document.querySelectorAll('[data-received-age]')].every(n => n.textContent === 'Stale / no connection confirmed')")
                    page.screenshot(path=str(artifacts / "job-desktop.png"), full_page=True)
                    page.get_by_role("button", name="Record transition").click()
                    assert page.url.endswith('/weigh')
                    for stream,gross in (("selected","4.5"),("pass_through","16.5"),("remainder","0.5")):
                        page.locator('[name="gross_'+stream+'"]').fill(gross)
                        page.locator('[name="weighed_'+stream+'"]').check()
                    page.get_by_role('button',name='Reconcile',exact=True).click()
                    page.locator('[name="accept_review"]').check()
                    page.get_by_role('button',name='Accept reconciliation & release outputs',exact=True).click()
                    page.get_by_role("heading", name="Completed", exact=True).wait_for()
                    page.get_by_role("link", name="Ready Selected", exact=True).first.click()
                    assert "4 kg" in page.inner_text("main")
                    page.get_by_role("link", name="Print permanent label").click()
                    page.wait_for_function("document.querySelector('img')?.complete && document.querySelector('img').naturalWidth > 0")
                    page.emulate_media(media="print")
                    page.screenshot(path=str(artifacts / "label-print.png"), full_page=True)
                    page.emulate_media(media="screen")

                    def create_bin(label, capacity="25"):
                        page.goto(url + "/containers")
                        page.locator(".container-create > summary").click()
                        form = page.locator('form[action="/containers"]')
                        form.locator('[name="label"]').fill(label)
                        form.locator('[name="location_id"]').select_option(bench_location)
                        assert form.locator('[name="tare_mass"]').evaluate("el => el.required && !el.validity.valid")
                        assert form.locator('[name="capacity"]').evaluate("el => el.required && !el.validity.valid")
                        form.locator('[name="tare_mass"]').fill("0.5")
                        form.locator('[name="capacity"]').fill(capacity)
                        form.get_by_role("button", name="Create container").click()
                        assert page.url == url + "/containers"
                        row = page.locator('tr[data-container-id]').filter(has_text=label)
                        cid = row.get_attribute('data-container-id')
                        row.get_by_role('link', name=label, exact=True).click()
                        assert "0.5 kg" in page.inner_text("main")
                        assert capacity + " kg net material" in page.inner_text("main")
                        return cid

                    def add_location(name):
                        page.goto(url + "/locations")
                        form = page.locator('form[action="/locations"]')
                        form.locator('[name="name"]').fill(name)
                        form.get_by_role("button", name="Add location", exact=True).click()
                        page.get_by_role("heading", name=name, exact=True).wait_for()
                        return page.url.rsplit("/", 1)[-1]

                    bench_location = add_location("Training bench")
                    shelf_location = add_location("Training shelf C")
                    new_beans_location = add_location("Shelf --- NewBeans")
                    temporary_location = add_location("Temporary location")
                    page.get_by_role("link", name="Edit location", exact=True).click()
                    form = page.locator('form[action$="/edit"]')
                    form.locator('[name="name"]').fill("Temporary location renamed")
                    form.get_by_role("button", name="Save location changes").click()
                    actions = page.locator('.actions').filter(has=page.get_by_role('link', name='Edit location', exact=True))
                    assert actions.get_by_role('link', name='Delete location', exact=True).count() == 1
                    page.get_by_role("link", name="Delete location", exact=True).click()
                    page.screenshot(path=str(artifacts / "location-delete-desktop.png"), full_page=True)
                    page.locator('[name="confirmed"]').check()
                    page.get_by_role("button", name="Delete location", exact=True).click()
                    response = page.goto(url + "/locations/" + temporary_location)
                    assert response.status == 410 and "deleted location" in page.inner_text("main").lower()
                    page.goto(url + "/locations")
                    page.screenshot(path=str(artifacts / "locations-desktop.png"), full_page=True)

                    unused = create_bin("Browser unused container")
                    page.get_by_role("button", name="Archive container", exact=True).click()
                    assert "Archived container" in page.inner_text("main")
                    page.get_by_role("button", name="Restore container", exact=True).click()
                    page.goto(url + "/containers")
                    assert page.locator('a[href$="/delete"]').count() == 0
                    page.locator(f'a[href="/containers/{unused}"]').click()
                    actions = page.locator('.actions').filter(has=page.get_by_role('link', name='Edit container', exact=True))
                    assert actions.get_by_role('link', name='Delete container', exact=True).count() == 1
                    page.get_by_role('link', name='Delete container', exact=True).click()
                    page.screenshot(path=str(artifacts / "container-delete-desktop.png"), full_page=True)
                    page.locator('[name="confirmed"]').check()
                    page.get_by_role("button", name="Delete container", exact=True).click()
                    response = page.goto(url + "/scan?code=" + unused)
                    assert response.status == 410
                    assert "deleted container" in page.inner_text("main").lower()
                    deleted_path = page.url
                    page.screenshot(path=str(artifacts / "deleted-container-desktop.png"), full_page=True)

                    page.goto(url + "/scan?code=Demo%20raw")
                    used_container = page.url.rsplit("/", 1)[-1]
                    page.get_by_role("link", name="Delete container", exact=True).click()
                    page.locator('[name="confirmed"]').check()
                    page.get_by_role("button", name="Delete container", exact=True).click()
                    response = page.goto(url + "/containers/" + used_container)
                    assert response.status == 410
                    assert page.locator('a[href^="/portions/"]').count() > 0

                    coffee = create_bin("Browser green coffee", capacity="1")
                    page.screenshot(path=str(artifacts / "container-tare-desktop.png"), full_page=True)
                    second_coffee = create_bin("Browser second coffee container")
                    page.goto(url + "/receive")
                    form = page.locator('form[action="/receive"]')
                    form.locator('[name="supplier"]').fill("Browser coffee supplier")
                    form.locator('[name="supplier_lot"]').fill("BROWSER-SUPPLIER-60")
                    page.screenshot(path=str(artifacts / "receiving-source-desktop.png"), full_page=True)
                    form.get_by_role("button", name="Next: receiving containers").click()
                    form.locator('[name="receiving_container_id"]').first.select_option(coffee)
                    form.get_by_role("button", name="Add another container").click()
                    first_select=form.locator('[name="receiving_container_id"]').first
                    second_select=form.locator('[name="receiving_container_id"]').nth(1)
                    assert second_select.locator(f'option[value="{coffee}"]').evaluate('el => el.disabled && el.hidden')
                    form.locator('[name="receiving_container_id"]').nth(1).select_option(second_coffee)
                    assert first_select.locator(f'option[value="{second_coffee}"]').evaluate('el => el.disabled && el.hidden')
                    first_select.select_option('')
                    assert second_select.locator(f'option[value="{coffee}"]').evaluate('el => !el.disabled && !el.hidden')
                    first_select.select_option(coffee)
                    form.locator('[data-container-rows] [data-load]').nth(1).get_by_role('button',name='Remove this container',exact=True).click()
                    assert first_select.locator(f'option[value="{second_coffee}"]').evaluate('el => !el.disabled && !el.hidden')
                    form.get_by_role('button',name='Add another container',exact=True).click()
                    form.locator('[name="receiving_container_id"]').nth(1).select_option(second_coffee)
                    form.locator('[name="ready"]').check()
                    form.get_by_role("button", name="Next: weigh beans").click()
                    form.locator('[name="receiving_net_mass"]').first.fill("1.201")
                    form.locator('[name="receiving_net_mass"]').nth(1).fill("0.500")
                    for check in form.locator('[name="tared"]').all():
                        check.check()
                    assert form.locator('[name="receiving_net_mass"]').first.get_attribute('max') == '1.2'
                    assert form.locator('[name="receiving_net_mass"]').first.evaluate('el => el.validity.rangeOverflow')
                    form.get_by_role("button", name="Next: storage location").click()
                    assert page.locator('[data-receiving-step="2"]').is_visible()
                    form.locator('[name="receiving_net_mass"]').first.fill("1.200")
                    assert form.locator('[name="estimate_particles"]').count() == 0
                    assert form.locator('[name="device"]').count() == 0
                    assert form.locator('[name="precision_mg"]').count() == 0
                    page.screenshot(path=str(artifacts / "receiving-weights-desktop.png"), full_page=True)
                    form.get_by_role("button", name="Next: storage location").click()
                    form.locator('[name="receiving_location_id"]').first.select_option(new_beans_location)
                    form.locator('[name="receiving_location_id"]').nth(1).select_option(bench_location)
                    form.locator('[name="placed"]').check()
                    form.get_by_role("button", name="Receive into stock").click()
                    assert "1.700 kg" in page.inner_text("main") and "1.200 kg" in page.inner_text("main") and "0.500 kg" in page.inner_text("main")
                    assert "remain unknown" in page.inner_text("main")
                    coffee_path = page.url
                    page.screenshot(path=str(artifacts / "receiving-receipt-desktop.png"), full_page=True)
                    page.goto(url + "/containers/" + coffee)
                    page.get_by_role("link", name="Edit container", exact=True).click()
                    edit_path = page.url
                    page.screenshot(path=str(artifacts / "container-edit-desktop.png"), full_page=True)
                    form = page.locator('form[action$="/edit"]')
                    for name, value in (("label", "Browser green coffee renamed"), ("tare_mass", "0.75"),
                                        ("capacity", "10")):
                        form.locator(f'[name="{name}"]').fill(value)
                    form.locator('[name="location_id"]').select_option(shelf_location)
                    form.get_by_role("button", name="Save container changes").click()
                    page.get_by_role("heading", name="Browser green coffee renamed", exact=True).wait_for()
                    assert "0.75 kg" in page.inner_text("main") and "1.2 kg" in page.inner_text("main")
                    assert "Estimated input particles" not in page.inner_text("main")
                    page.get_by_role("link", name="Material handling", exact=True).click()
                    page.get_by_role("link", name="Move container", exact=True).click()
                    form = page.locator('form[action="/handling/move"][method="get"]')
                    form.locator('[name="container_id"]').select_option(coffee)
                    page.wait_for_url(url + "/handling/move?container_id=" + coffee)
                    assert "1.200 kg" in page.inner_text("main")
                    page.screenshot(path=str(artifacts / "move-container-desktop.png"), full_page=True)
                    form = page.locator('form[action="/handling/move"][method="post"]')
                    form.locator('[name="location_id"]').select_option(new_beans_location)
                    form.locator('[name="physical_confirmation"]').check()
                    form.get_by_role("button", name="Record container move").click()
                    assert page.url == url + "/handling/move"
                    assert page.locator('[data-move-container-picker]').input_value() == ''
                    assert page.locator('form[action="/handling/move"][method="post"]').count() == 0
                    assert 'Container move recorded' in page.inner_text('main')
                    # Move another vessel to the first vessel's former location.
                    page.locator('[data-move-container-picker]').select_option(second_coffee)
                    page.wait_for_url(url + "/handling/move?container_id=" + second_coffee)
                    form = page.locator('form[action="/handling/move"][method="post"]')
                    assert form.locator(f'[name="location_id"] option[value="{shelf_location}"]').count() == 1
                    form.locator('[name="location_id"]').select_option(shelf_location)
                    form.locator('[name="physical_confirmation"]').check()
                    form.get_by_role('button',name='Record container move',exact=True).click()
                    assert page.url == url + '/handling/move' and page.locator('[data-move-container-picker]').input_value() == ''
                    page.locator('[data-move-container-picker]').select_option(coffee)
                    page.wait_for_url(url + '/handling/move?container_id=' + coffee)
                    form = page.locator('form[action="/handling/move"][method="post"]')
                    assert form.locator(f'[name="location_id"] option[value="{shelf_location}"]').count() == 1
                    assert form.locator(f'[name="location_id"] option[value="{new_beans_location}"]').count() == 0
                    assert not form.locator('[name="physical_confirmation"]').is_checked()
                    assert "1.200 kg" in page.inner_text("main")
                    page.get_by_role("link", name="Browser green coffee renamed", exact=True).click()
                    assert "1.2 kg" in page.inner_text("main")
                    page.get_by_role("link", name="Shelf --- NewBeans", exact=True).first.click()
                    assert "Browser green coffee renamed" in page.inner_text("main")
                    location_path = page.url
                    page.screenshot(path=str(artifacts / "location-members-desktop.png"), full_page=True)
                    page.get_by_role("link", name="Delete location", exact=True).click()
                    assert "still contains" in page.inner_text("main")
                    assert page.get_by_role("button", name="Delete location", exact=True).count() == 0
                    page.get_by_role("link", name="Back to location", exact=True).click()
                    page.get_by_role("link", name="Edit location", exact=True).click()
                    location_edit_path = page.url
                    page.screenshot(path=str(artifacts / "location-edit-desktop.png"), full_page=True)
                    form = page.locator('form[action$="/edit"]')
                    form.locator('[name="name"]').fill("Shelf --- Fresh Beans")
                    form.get_by_role("button", name="Save location changes").click()
                    page.goto(url + "/containers/" + coffee)
                    assert "Shelf --- Fresh Beans" in page.inner_text("main")

                    def move_contents(source_label, destination, amount, whole=False):
                        page.goto(url + '/handling/contents')
                        source=page.locator('[name="source_id"] option').filter(has_text=source_label).get_attribute('value')
                        page.locator('[name="source_id"]').select_option(source)
                        page.locator('[name="destination_id"]').select_option(destination)
                        page.locator('[name="moved_mass"]').fill(amount)
                        if whole:page.locator('[name="empty_source"]').check()
                        page.get_by_role('button',name='Record movement',exact=True).click()
                        page.get_by_role('heading',name='Movement recorded',exact=True).wait_for()
                        return page.url

                    withdrawal = create_bin('Browser withdrawal')
                    move_contents('Ready Selected',withdrawal,'1')
                    blend = create_bin('Browser blend')
                    move_contents('Browser withdrawal',blend,'1',whole=True)
                    blend_path=move_contents('Demo Selected',blend,'4',whole=True)
                    page.goto(url+'/handling/reconcile?container_id='+blend)
                    page.locator('[name="weight"]').fill('4.5')
                    page.locator('[name="reason"]').fill('Samples removed')
                    page.get_by_role('button',name='Record weighing',exact=True).click()
                    page.get_by_role('heading',name='Weighing recorded',exact=True).wait_for()
                    reconcile_path=page.url
                    package_path=url+'/handling/package'
                    page.goto(package_path)
                    assert 'Package as product' in page.inner_text('main')
                    widths = prepared_viewports.copy()
                    for width in (768, 390):
                        page.set_viewport_size({"width": width, "height": 1000})
                        for name, target in (("stock", url), ("combined-contents", blend_path), ("reconciliation", reconcile_path), ("package-as-product", package_path), ("move-contents", url+"/handling/contents"),
                                             ("simulation-settings", url + "/simulation-settings"), ("receiving-receipt", coffee_path),
                                             ("container-register", url + "/containers"), ("container-tare", url + "/containers/" + coffee),
                                             ("container-edit", edit_path), ("container-deleted", deleted_path),
                                             ("locations", url + "/locations"), ("location-members", location_path),
                                             ("receiving-form", url + "/receive"), ("location-edit", location_edit_path),
                                             ("move-container", url + "/handling/move?container_id=" + coffee)):
                            page.goto(target)
                            overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                            widths.append({"page": name, "width": width, "horizontal_overflow": overflow})
                            page.screenshot(path=str(artifacts / f"{name}-{width}.png"), full_page=True)
                            assert not overflow, f"{name} page overflows at {width}px"
                    assert not errors, errors
                    browser.close()
                    result = {"workflow": "prepared-job edit, deletion, claim reuse and start guards; locations, receiving, container lifecycle, central container move, simulation, sort, immediate contents movements, weighing adjustments and product packaging form passed", "viewport_checks": widths, "page_errors": errors}
                    (artifacts / "browser-result.json").write_text(json.dumps(result, indent=2))
                    print(json.dumps(result))
            finally:
                server.terminate()
                server.wait(timeout=10)


if __name__ == "__main__":
    main()
