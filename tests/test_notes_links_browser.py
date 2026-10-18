import os
import subprocess
import sys
import time

import httpx
from playwright.sync_api import expect, sync_playwright


def test_note_urls_render_as_safe_links(tmp_path):
    env = {
        **os.environ,
        "GARAGE_DATA_DIR": str(tmp_path),
        "GARAGE_NOTIFY_WORKER": "false",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8772"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = "http://127.0.0.1:8772"
    try:
        for _ in range(70):
            try:
                if httpx.get(url + "/api/status").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        with httpx.Client(base_url=url) as c:
            c.post(
                "/api/setup",
                json={"username": "demo-driver", "password": "demo-password"},
            )
            c.post(
                "/api/notes",
                json={
                    "vehicle_id": 1,
                    "date": "2026-10-18",
                    "body": "Filter at https://rockauto.com/part?x=1&y=2, torque notes javascript:alert(1) <b>not bold</b>",
                },
            )
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 960})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(url)
            page.locator("[name=username]").fill("demo-driver")
            page.locator("[name=password]").fill("demo-password")
            page.get_by_role("button", name="Sign in", exact=True).click()
            page.locator(".vehicle-card").first.click()
            page.get_by_role("button", name="Notes", exact=True).click()
            link = page.locator(".entry .e-sub a")
            expect(link).to_have_count(1)
            expect(link).to_have_attribute(
                "href", "https://rockauto.com/part?x=1&y=2"
            )
            expect(link).to_have_attribute("target", "_blank")
            expect(link).to_have_attribute("rel", "noopener")
            # Non-http text and markup stay plain escaped text.
            expect(page.locator(".entry .e-sub")).to_contain_text(
                "javascript:alert(1) <b>not bold</b>"
            )
            expect(page.locator(".entry .e-sub b")).to_have_count(0)
            page.screenshot(
                path=str(tmp_path / "garage-note-link.png"), full_page=False
            )
            assert errors == []
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=5)
