import os
import subprocess
import sys
import time

import httpx
from playwright.sync_api import expect, sync_playwright


def test_towing_desktop_and_mobile(tmp_path):
    env = {**os.environ, "GARAGE_DATA_DIR": str(tmp_path), "GARAGE_NOTIFY_WORKER": "false"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8766"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = "http://127.0.0.1:8766"
    try:
        for _ in range(70):
            try:
                if httpx.get(url + '/api/status').status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        with httpx.Client(base_url=url) as c:
            c.post('/api/setup', json={"username": "demo-driver", "password": "demo-password"})
            c.put('/api/vehicles/1', json={"name": "Demo Tow Truck", "year": "2024", "mileage": 10000, "icon": "🚗"})
            for day, miles, gallons, towing in ((1,10000,15,False),(3,10300,10,False),(5,10500,20,True),(7,10800,10,False),(9,11000,20,True)):
                c.post('/api/fuel', json={"vehicle_id":1,"date":f'2026-09-{day:02}',"odometer":miles,"gallons":gallons,"cost":gallons*3.5,"towing":towing})
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for width, height in ((1280,960),(390,844),(280,800)):
                page=browser.new_page(viewport={"width":width,"height":height})
                errors=[]
                page.on('pageerror',lambda e: errors.append(str(e)))
                page.goto(url)
                page.locator('[name=username]').fill('demo-driver')
                page.locator('[name=password]').fill('demo-password')
                page.get_by_role('button',name='Sign in',exact=True).click()
                page.locator('.vehicle-card').first.click()
                page.get_by_role('button',name='Fuel',exact=True).click()
                expect(page.locator('.fuel-stat').nth(0)).to_contain_text('30.0 mpg')
                expect(page.locator('.fuel-stat').nth(1)).to_contain_text('10.0 mpg')
                expect(page.locator('.fuel-stat')).to_have_count(4)
                expect(page.locator('.fuel-stat').nth(2)).to_contain_text('$0.117')
                expect(page.locator('.fuel-stat').nth(3)).to_contain_text('$0.350')
                expect(page.locator('.fill-efficiency').first).to_have_text('10.0 mpg')
                expect(page.locator('.fill-efficiency').last).to_have_text('MPG unavailable')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(tmp_path / f'garage-towing-fuel-{width}.png'),full_page=True)
                page.get_by_role('button',name='+ Log fill-up',exact=True).click()
                page.locator('#fuelForm [name=date]').fill('2026-09-11')
                page.locator('#fuelForm [name=odometer]').fill('11200')
                page.locator('#fuelForm [name=gallons]').fill('20')
                page.locator('#fuelForm [name=cost]').fill('70')
                page.locator('#fuelForm [name=towing]').check()
                expect(page.locator('#fuelForm [name=towing]')).to_be_checked()
                assert page.locator('.towing-toggle').bounding_box()['height'] >= 64
                page.screenshot(path=str(tmp_path / f'garage-towing-form-{width}.png'),full_page=False)
                page.locator('#fuelForm').get_by_role('button',name='Save',exact=True).click()
                expect(page.locator('.towing-badge').first).to_be_visible()
                # Edit preservation and toggling are checked through the actual form.
                page.locator('[data-action=edit-fuel]').first.click()
                expect(page.locator('#fuelForm [name=towing]')).to_be_checked()
                page.locator('#fuelForm [name=towing]').uncheck()
                page.locator('#fuelForm').get_by_role('button',name='Save',exact=True).click()
                expect(page.locator('.entry').first.locator('.towing-badge')).to_have_count(0)
                page.get_by_role('button',name='Open menu').click()
                page.get_by_role('button',name='Settings',exact=True).click()
                page.locator('[name=use_kilometers]').check()
                page.locator('#settingsForm').get_by_role('button',name='Save settings',exact=True).click()
                page.get_by_role('button',name='← Garage',exact=True).click()
                page.locator('.vehicle-card').first.click()
                page.get_by_role('button',name='Fuel',exact=True).click()
                expect(page.locator('.fuel-stat').nth(1)).to_contain_text('L/100km')
                assert errors == []
                page.close()
                # Restore shared seeded state for next viewport.
                with httpx.Client(base_url=url) as c:
                    c.post('/api/login',json={"username":"demo-driver","password":"demo-password"})
                    for row in c.get('/api/fuel').json():
                        if row['date']=='2026-09-11': c.delete(f"/api/fuel/{row['id']}")
                    c.put('/api/settings',json={"use_kilometers":False})
            browser.close()
    finally:
        proc.terminate();proc.wait(timeout=5)
