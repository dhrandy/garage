import os, subprocess, time
import httpx, pytest
from playwright.sync_api import expect, sync_playwright


@pytest.fixture(scope="module")
def app_url(tmp_path_factory):
    env = {
        **os.environ,
        "GARAGE_DATA_DIR": str(tmp_path_factory.mktemp("browser")),
        "GARAGE_NOTIFY_WORKER": "false",
    }
    proc = subprocess.Popen(
        ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8765"], env=env
    )
    for _ in range(50):
        try:
            if httpx.get("http://127.0.0.1:8765/api/status").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    yield "http://127.0.0.1:8765"
    proc.terminate()
    proc.wait(timeout=5)


def test_menu_tabs_and_responsive_layout(app_url):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width, height in ((1920, 1080), (390, 844)):
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(app_url)
            page.wait_for_load_state("networkidle")
            if page.get_by_role("heading", name="Set up Garage").count():
                page.locator("[name=username]").fill("admin-test")
                page.locator("[name=password]").fill("password-123")
                page.get_by_role("button", name="Create administrator").click()
            else:
                page.locator("[name=username]").fill("admin-test")
                page.locator("[name=password]").fill("password-123")
                page.get_by_role("button", name="Sign in").click()
            expect(page.locator(".vehicle-card").first).to_be_visible()
            page.evaluate("""async () => {
                const vehicles=await fetch('/api/vehicles').then(r=>r.json());
                const vehicle=vehicles[0];
                const response=await fetch(`/api/vehicles/${vehicle.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({...vehicle,name:'Hyundai Tucson',year:'2023',mileage:46762,fuel_type:'Regular gasoline',tire_size:'235/65R17',oil_spec:'0W-20, 5.3 qt'})});
                if (!response.ok) throw new Error(`vehicle setup failed: ${response.status}`);
            }""")
            page.reload()
            page.wait_for_load_state("networkidle")
            page.locator(".vehicle-card").first.click()
            labels = [
                "Specs",
                "Maintenance",
                "Reminders",
                "Fuel",
                "Mods",
                "Costs",
                "Notes",
            ]
            tabs = page.locator(".tab")
            expect(tabs).to_have_count(7)
            tabs_box = page.locator(".tabs").bounding_box()
            tab_boxes = [tabs.nth(i).bounding_box() for i in range(tabs.count())]
            assert tabs_box is not None and all(box is not None for box in tab_boxes)
            assert all(
                box["x"] >= tabs_box["x"] - 1
                and box["x"] + box["width"] <= tabs_box["x"] + tabs_box["width"] + 1
                for box in tab_boxes
            )
            if width == 390:
                assert len({round(box["y"]) for box in tab_boxes}) == 2
                chips = page.locator(".vehicle-specs>div")
                expect(chips).to_have_count(3)
                chip_boxes = [chips.nth(i).bounding_box() for i in range(chips.count())]
                assert all(
                    box is not None and box["width"] < tabs_box["width"] * 0.75
                    for box in chip_boxes
                )
            page.get_by_role("button", name="Maintenance", exact=True).click()
            page.get_by_role("button", name="+ Log service").click()
            service_date = page.locator("#serviceForm [name=date]")
            assert service_date.get_attribute("required") is None
            service_date.fill("")
            expect(page.locator("#serviceForm [name=reminder_id]")).to_be_disabled()
            page.locator("#serviceForm [name=type]").fill("Undated browser service")
            page.locator("#serviceForm").get_by_role("button", name="Save").click()
            expect(page.locator("#tabBody")).to_contain_text("Date not set")
            page.get_by_role("button", name="Specs", exact=True).click()
            page.get_by_role("button", name="Edit specs").click()
            fields = page.locator("#specsForm .field")
            expect(fields).to_have_count(29)
            boxes = [fields.nth(i).bounding_box() for i in range(fields.count())]
            assert all(boxes[i]["y"] < boxes[i + 1]["y"] for i in range(len(boxes) - 1))
            assert all(b["x"] + b["width"] <= width for b in boxes)
            realistic_specs = {
                "engine": "5.0L Ti-VCT V8",
                "displacement": "5.0L / 302 cu in",
                "transmission": "10-speed SelectShift automatic",
                "drivetrain": "4x2; electronic-locking rear differential",
                "body_style": "SuperCrew 4-door pickup, Lariat",
                "exterior_color": "Antimatter Blue Metallic",
                "vin": "1FTFW1E50MFA12345",
                "horsepower": "400 hp @ 6,000 rpm",
                "torque": "410 lb-ft @ 4,250 rpm",
                "curb_weight": "4,705 lb curb weight",
                "wheelbase": "145.4 in",
                "dimensions": "L 231.7 in, W 79.9 in excl mirrors, H 75.6 in, ground clearance 8.5 in",
                "fuel_type": "Regular gasoline",
                "fuel_capacity": "26 gal",
                "towing_capacity": "13,000 lb with Max Trailer Tow Package",
                "payload": "2,445 lb maximum payload",
                "mpg_city": "17 mpg EPA city",
                "mpg_highway": "23 mpg EPA highway",
                "oil_type": "SAE 5W-30 full synthetic",
                "oil_capacity": "7.7 qt with filter",
                "battery_group": "H7 AGM / 94R",
                "spark_plugs": "Motorcraft SP-594 / 0.049 in gap",
                "wiper_sizes": "22 in driver / 22 in passenger",
                "coolant_type": "Motorcraft Yellow prediluted coolant",
                "brake_fluid": "DOT 4 LV high performance",
                "air_filter_part_number": "Motorcraft FA-1883",
                "wheel_lug_torque": "150 lb-ft",
                "wheel_size": "18 in machined aluminum",
                "tire_size": "265/60R18 all-terrain",
            }
            assert len(realistic_specs) == 29
            for name, value in realistic_specs.items():
                page.locator(f"#specsForm [name={name}]").fill(value)
            expect(fields.nth(28).locator("label")).to_have_text("Tire size")
            expect(fields.nth(27).locator("label")).to_have_text("Wheel size")
            save = page.locator("#specsForm").get_by_role("button", name="Save")
            save.scroll_into_view_if_needed()
            with page.expect_response(
                lambda r: r.request.method == "PUT" and r.url.endswith("/specs")
            ) as saved:
                save.click()
            assert saved.value.ok
            expect(page.locator("#specsForm")).to_have_count(0)
            grid = page.locator(".spec-sections")
            expect(grid).to_be_visible()
            wheels = grid.locator(".spec-section", has_text="Wheels and Tires")
            expect(wheels).to_be_visible()
            expect(wheels.locator("dt")).to_have_text(
                ["Wheel size", "Tire size", "Lug torque"]
            )
            expect(wheels.locator("dd")).to_have_text(
                ["18 in machined aluminum", "265/60R18 all-terrain", "150 lb-ft"]
            )
            expect(
                grid.locator(".spec-section", has_text="Fuel and Economy")
                .locator("dt")
                .first
            ).to_have_text("Fuel type")
            # header chips read from the saved specs without a reload
            expect(page.locator(".vehicle-specs dd")).to_have_text(
                [
                    "Regular gasoline",
                    "265/60R18 all-terrain",
                    "SAE 5W-30 full synthetic, 7.7 qt with filter",
                ]
            )
            if os.environ.get("GARAGE_SCREENSHOT_DIR"):
                wheels.scroll_into_view_if_needed()
                page.screenshot(
                    path=os.path.join(
                        os.environ["GARAGE_SCREENSHOT_DIR"],
                        f"wheels-and-tires-{width}.png",
                    ),
                    full_page=(width == 390),
                )
            grid_box = grid.bounding_box()
            assert (
                grid_box is not None
                and grid_box["x"] >= 0
                and grid_box["x"] + grid_box["width"] <= width
            )
            sections = page.locator(".spec-section")
            expect(sections).to_have_count(6)
            section_boxes = [
                sections.nth(i).bounding_box() for i in range(sections.count())
            ]
            assert all(box is not None for box in section_boxes)
            columns = {}
            for box in section_boxes:
                columns.setdefault(round(box["x"]), []).append(box)
            assert len(columns) == (2 if width == 1920 else 1)
            for boxes in columns.values():
                boxes.sort(key=lambda box: box["y"])
                assert all(
                    abs(boxes[i + 1]["y"] - (boxes[i]["y"] + boxes[i]["height"] + 14))
                    < 2
                    for i in range(len(boxes) - 1)
                )
            row = page.locator(".spec-row").first
            row_box = row.bounding_box()
            label_box = row.locator("dt").bounding_box()
            value_box = row.locator("dd").bounding_box()
            assert (
                row_box is not None and label_box is not None and value_box is not None
            )
            assert (
                label_box["x"] < value_box["x"]
                and abs(label_box["y"] - value_box["y"]) < 2
            )
            assert page.locator(".tab").all_inner_texts() == labels
            page.evaluate("""async () => {
                const vehicles=await fetch('/api/vehicles').then(r=>r.json());
                const vehicle_id=vehicles[0].id;
                for (const name of ['Numbered mod one','Numbered mod two']) {
                    const response=await fetch('/api/mods',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vehicle_id,name,price:0})});
                    if (!response.ok) throw new Error(`mod setup failed: ${response.status}`);
                }
            }""")
            page.reload()
            page.wait_for_load_state("networkidle")
            page.locator(".vehicle-card").first.click()
            page.get_by_role("button", name="Mods", exact=True).click()
            cards = page.locator(".mod-card")
            expect(cards).to_have_count(2 if width == 1920 else 4)
            numbers = cards.locator(".entry-number").all_inner_texts()
            assert numbers == [str(i) for i in range(1, len(numbers) + 1)]
            first_box = cards.nth(0).bounding_box()
            second_box = cards.nth(1).bounding_box()
            assert first_box is not None and second_box is not None
            if width == 1920:
                assert second_box["x"] > first_box["x"]
                assert abs(second_box["y"] - first_box["y"]) < 2
            else:
                assert abs(second_box["x"] - first_box["x"]) < 2
                assert second_box["y"] > first_box["y"]
            for label in labels:
                page.get_by_role("button", name=label, exact=True).click()
                expect(page.locator("#tabBody")).to_be_visible()
            page.locator("[data-action=toggle-menu]").click()
            expect(page.locator("[data-action=settings]")).to_be_visible()
            for action in ("settings", "users", "export", "import", "logout"):
                after = page.evaluate(
                    "a=>getComputedStyle(document.querySelector(`.menu-panel [data-action=${a}]`),'::after').content",
                    action,
                )
                assert after in (
                    "none",
                    "",
                ), f"menu label duplicated for {action}: {after}"
            assert (
                page.locator(".menu-panel [data-action=export]").inner_text()
                == "Export"
            )
            page.locator("[data-action=settings]").click()
            expect(page.locator(".detail-meta")).to_have_text("Customize this garage")
            page.locator("[data-action=toggle-menu]").click()
            expect(page.locator("[data-action=users]")).to_be_visible()
            page.locator("[data-action=users]").click()
            expect(page.locator(".detail-meta")).to_have_text(
                "Manage access to the garage"
            )
            expect(page.get_by_role("button", name="Refresh data")).to_be_visible()
            assert errors == []
            page.locator("[data-action=toggle-menu]").click()
            expect(page.locator("[data-action=logout]")).to_be_visible()
            page.locator("[data-action=logout]").click()
            expect(page.get_by_role("heading", name="Welcome back")).to_be_visible()
            expect(page.locator(".vehicle-specs")).to_have_count(0)
            expect(page.locator(".tab")).to_have_count(0)
            assert "Ford Mustang" not in page.locator("body").inner_text()
            page.close()
        browser.close()
