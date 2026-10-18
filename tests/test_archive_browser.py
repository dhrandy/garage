import os
import subprocess
import sys
import time

import httpx
from playwright.sync_api import expect, sync_playwright


def test_archive_flow_desktop_and_mobile(tmp_path):
    env = {
        **os.environ,
        "GARAGE_DATA_DIR": str(tmp_path),
        "GARAGE_NOTIFY_WORKER": "false",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8767"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = "http://127.0.0.1:8767"
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
            c.put(
                "/api/vehicles/1",
                json={
                    "name": "Corvette ZR1X",
                    "year": "2026",
                    "mileage": 12000,
                    "icon": "🏎️",
                },
            )
            for name, year, miles, icon in (
                ("Mustang Dark Horse", "2024", 8400, "🚗"),
                ("F-150 Raptor", "2021", 61000, "🛻"),
                ("WRX Wagon", "2020", 75500, "🚙"),
            ):
                c.post(
                    "/api/vehicles",
                    json={"name": name, "year": year, "mileage": miles, "icon": icon},
                )
            for vid in (1, 2, 3, 4):
                for day, odo, gal in ((1, 10000, 12), (8, 10360, 12), (15, 10720, 12)):
                    c.post(
                        "/api/fuel",
                        json={
                            "vehicle_id": vid,
                            "date": f"2026-09-{day:02}",
                            "odometer": odo + vid * 1000,
                            "gallons": gal,
                            "cost": gal * 3.6,
                        },
                    )
            c.post(
                "/api/reminders",
                json={
                    "vehicle_id": 4,
                    "name": "Oil change",
                    "miles_interval": 5000,
                    "months_interval": None,
                    "due_date": None,
                    "repeats_yearly": False,
                    "last_date": "2026-01-01",
                    "last_mileage": 70000,
                },
            )
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for width, height in ((1280, 960), (390, 844)):
                page = browser.new_page(viewport={"width": width, "height": height})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("dialog", lambda d: d.accept())
                page.goto(url)
                page.locator("[name=username]").fill("demo-driver")
                page.locator("[name=password]").fill("demo-password")
                page.get_by_role("button", name="Sign in", exact=True).click()
                expect(page.locator(".vehicle-card")).to_have_count(4)
                # The WRX has an overdue reminder: it shows in the due summary.
                expect(page.locator(".due-bar")).to_contain_text("1 due")
                # Archiving from the vehicle edit form.
                page.locator(".vehicle-card", has_text="WRX Wagon").click()
                page.get_by_role("button", name="Edit", exact=True).click()
                page.locator("#vehicleForm").get_by_role(
                    "button", name="Archive", exact=True
                ).click()
                # Staying on the car, now marked Archived with an Unarchive button.
                expect(page.locator(".archived-pill")).to_have_text("Archived")
                expect(
                    page.get_by_role("button", name="Edit", exact=True)
                ).to_have_count(0)
                page.screenshot(
                    path=str(tmp_path / f"garage-archived-detail-{width}.png"),
                    full_page=True,
                )
                page.get_by_role("button", name="← Garage", exact=True).click()
                expect(page.locator(".vehicle-card")).to_have_count(3)
                expect(page.locator(".archived-section summary")).to_contain_text(
                    "Archived"
                )
                # Its reminder left the due summary while archived.
                expect(page.locator(".due-bar")).to_have_count(0)
                page.screenshot(
                    path=str(tmp_path / f"garage-archived-collapsed-{width}.png"),
                    full_page=True,
                )
                # Expanded section shows the car read-only with Unarchive.
                page.locator(".archived-section summary").click()
                expect(page.locator(".archived-row")).to_have_count(1)
                expect(page.locator(".archived-row")).to_contain_text("WRX Wagon")
                page.screenshot(
                    path=str(tmp_path / f"garage-archived-open-{width}.png"),
                    full_page=True,
                )
                # Detail of an archived car: Archived pill, no edit buttons.
                page.locator(".archived-open").click()
                expect(page.locator(".archived-pill")).to_have_text("Archived")
                expect(
                    page.get_by_role("button", name="Edit", exact=True)
                ).to_have_count(0)
                expect(
                    page.get_by_role("button", name="+ Log service", exact=True)
                ).to_have_count(0)
                # Unarchive from the detail view.
                page.get_by_role("button", name="Unarchive", exact=True).click()
                expect(page.locator(".archived-pill")).to_have_count(0)
                expect(
                    page.get_by_role("button", name="Edit", exact=True)
                ).to_be_visible()
                # Archived car's reminder did not appear in the due list while archived.
                page.get_by_role("button", name="← Garage", exact=True).click()
                expect(page.locator(".vehicle-card")).to_have_count(4)
                assert errors == []
                page.close()
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=5)
