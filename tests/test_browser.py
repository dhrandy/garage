import os, subprocess, time
import httpx, pytest
from playwright.sync_api import expect, sync_playwright

@pytest.fixture(scope='module')
def app_url(tmp_path_factory):
    env={**os.environ,'GARAGE_DATA_DIR':str(tmp_path_factory.mktemp('browser')),'GARAGE_NOTIFY_WORKER':'false'}
    proc=subprocess.Popen(['uvicorn','app.main:app','--host','127.0.0.1','--port','8765'],env=env)
    for _ in range(50):
        try:
            if httpx.get('http://127.0.0.1:8765/api/status').status_code==200: break
        except httpx.HTTPError: pass
        time.sleep(.1)
    yield 'http://127.0.0.1:8765'
    proc.terminate();proc.wait(timeout=5)

def test_menu_tabs_and_responsive_layout(app_url):
    with sync_playwright() as p:
        browser=p.chromium.launch()
        for width,height in ((1920,1080),(390,844)):
            page=browser.new_page(viewport={'width':width,'height':height});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(app_url);page.wait_for_load_state('networkidle')
            if page.get_by_role('heading',name='Set up Garage').count():
                page.locator('[name=username]').fill('admin-test');page.locator('[name=password]').fill('password-123');page.get_by_role('button',name='Create administrator').click()
            else:
                page.locator('[name=username]').fill('admin-test');page.locator('[name=password]').fill('password-123');page.get_by_role('button',name='Sign in').click()
            expect(page.locator('.vehicle-card').first).to_be_visible()
            page.locator('.vehicle-card').first.click()
            labels=['Maintenance','Reminders','Fuel','Mods','Costs','Notes']
            expect(page.locator('.tab')).to_have_count(6)
            assert page.locator('.tab').all_inner_texts()==labels
            for label in labels:
                page.get_by_role('button',name=label,exact=True).click();expect(page.locator('#tabBody')).to_be_visible()
            page.locator('[data-action=toggle-menu]').click();expect(page.locator('[data-action=settings]')).to_be_visible();page.locator('[data-action=settings]').click()
            expect(page.locator('.detail-meta')).to_have_text('Customize this garage')
            page.locator('[data-action=toggle-menu]').click();expect(page.locator('[data-action=users]')).to_be_visible();page.locator('[data-action=users]').click()
            expect(page.locator('.detail-meta')).to_have_text('Manage access to the shared garage')
            expect(page.get_by_role('button',name='Refresh data')).to_be_visible()
            assert errors==[]
            page.close()
        browser.close()
