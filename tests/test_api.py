import os
import json
from pathlib import Path

os.environ["GARAGE_DATA_DIR"] = "/tmp/garage-pytest-data"
os.environ["GARAGE_NOTIFY_WORKER"] = "false"
from fastapi.testclient import TestClient
from app import main


def test_shared_garage_and_permissions(tmp_path):
    main.DB_PATH = tmp_path / "garage.db"
    main.init_db()
    with TestClient(main.app) as admin:
        assert (
            admin.post(
                "/api/setup",
                json={"username": "admin-test", "password": "password-123"},
            ).status_code
            == 200
        )
        vehicle = admin.get("/api/vehicles").json()[0]
        assert {
            k: vehicle[k] for k in ("id", "name", "year", "mileage", "icon", "added_by")
        } == {
            "id": 1,
            "name": "Ford Mustang",
            "year": "1969",
            "mileage": 0,
            "icon": "🚗",
            "added_by": "System",
        }
        assert (
            admin.post(
                "/api/users",
                json={
                    "username": "member-test",
                    "password": "password-456",
                    "is_admin": False,
                },
            ).status_code
            == 200
        )
        service = {
            "vehicle_id": 1,
            "date": "2026-09-20",
            "mileage": 24000,
            "type": "Oil change",
            "cost": 55,
            "provider": "DIY",
            "notes": "",
        }
        assert admin.post("/api/services", json=service).status_code == 200
        reminder = {
            "vehicle_id": 1,
            "name": "Oil change",
            "miles_interval": 5000,
            "months_interval": 6,
            "last_date": "2026-03-01",
            "last_mileage": 20000,
        }
        assert admin.post("/api/reminders", json=reminder).status_code == 200
    with TestClient(main.app) as member:
        assert (
            member.post(
                "/api/login",
                json={"username": "member-test", "password": "password-456"},
            ).status_code
            == 200
        )
        # members only see vehicles they own: the admin-owned Mustang is invisible
        assert member.get("/api/vehicles").json() == []
        assert member.get("/api/services?vehicle_id=1").json() == []
        assert member.get("/api/services").json() == []
        assert member.get("/api/users").status_code == 403


def test_security_headers_and_login_rate_limit(tmp_path):
    main.DB_PATH = tmp_path / "security.db"
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as client:
        response = client.get("/api/status")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "default-src 'self'" in response.headers["content-security-policy"]
        client.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
    main._login_failures.clear()
    with TestClient(main.app) as client:
        for _ in range(main.LOGIN_LIMIT):
            response = client.post(
                "/api/login",
                json={"username": "admin-test", "password": "wrong-password"},
            )
            assert response.status_code == 401
            assert response.json()["detail"] == "Invalid username or password"
        response = client.post(
            "/api/login", json={"username": "admin-test", "password": "wrong-password"}
        )
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0
        assert response.json()["detail"] == "Too many login attempts. Try again later."


def test_admin_can_rename_garage_and_member_cannot(tmp_path):
    main.DB_PATH = tmp_path / "settings.db"
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        assert admin.get("/api/settings").json() == {
            "garage_name": "Your Garage",
            "hide_maintenance": False,
            "hide_costs": False,
            "hide_fuel": False,
            "hide_notes": False,
            "use_vehicle_photos": False,
            "use_kilometers": False,
        }
        assert (
            admin.put("/api/settings", json={"garage_name": "Test Garage"}).json()[
                "garage_name"
            ]
            == "Test Garage"
        )
        admin.post(
            "/api/users",
            json={
                "username": "member-test",
                "password": "password-456",
                "is_admin": False,
            },
        )
    with TestClient(main.app) as member:
        member.post(
            "/api/login", json={"username": "member-test", "password": "password-456"}
        )
        assert member.get("/api/settings").json()["garage_name"] == "Test Garage"
        assert (
            member.put("/api/settings", json={"garage_name": "Nope"}).status_code == 403
        )


def test_admin_can_hide_vehicle_sections(tmp_path):
    main.DB_PATH = tmp_path / "sections.db"
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        result = admin.put(
            "/api/settings", json={"hide_fuel": True, "hide_costs": True}
        ).json()
        assert result["hide_fuel"] is True and result["hide_costs"] is True
        assert result["hide_maintenance"] is False
        assert result["garage_name"] == "Your Garage"
        admin.post(
            "/api/users",
            json={
                "username": "member-test",
                "password": "password-456",
                "is_admin": False,
            },
        )
    with TestClient(main.app) as member:
        member.post(
            "/api/login", json={"username": "member-test", "password": "password-456"}
        )
        settings = member.get("/api/settings").json()
        assert settings["hide_fuel"] is True and settings["hide_costs"] is True
        assert member.put("/api/settings", json={"hide_fuel": False}).status_code == 403


def test_fuel_log_computes_mpg_and_bumps_mileage(tmp_path):
    main.DB_PATH = tmp_path / "fuel.db"
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        first = admin.post(
            "/api/fuel",
            json={
                "vehicle_id": 1,
                "date": "2026-09-01",
                "odometer": 20000,
                "gallons": 10,
                "cost": 35,
            },
        )
        assert first.status_code == 200 and first.json()["mpg"] is None
        second = admin.post(
            "/api/fuel",
            json={
                "vehicle_id": 1,
                "date": "2026-09-15",
                "odometer": 20300,
                "gallons": 10,
                "cost": 37,
            },
        ).json()
        assert second["mpg"] == 30.0
        rows = admin.get("/api/fuel?vehicle_id=1").json()
        assert [r["odometer"] for r in rows] == [20300, 20000]
        assert admin.get("/api/vehicles").json()[0]["mileage"] == 20300
        admin.post(
            "/api/users",
            json={
                "username": "member-test",
                "password": "password-456",
                "is_admin": False,
            },
        )
    with TestClient(main.app) as member:
        member.post(
            "/api/login", json={"username": "member-test", "password": "password-456"}
        )
        assert (
            member.put(
                "/api/fuel/%s" % second["id"],
                json={
                    "vehicle_id": 1,
                    "date": "2026-09-15",
                    "odometer": 20310,
                    "gallons": 10,
                    "cost": 37,
                },
            ).status_code
            == 404
        )
        assert member.delete("/api/fuel/%s" % second["id"]).status_code == 404
        assert member.get("/api/fuel?vehicle_id=1").json() == []
    with TestClient(main.app) as admin_again:
        admin_again.post(
            "/api/login", json={"username": "admin-test", "password": "password-123"}
        )
        assert admin_again.delete("/api/fuel/%s" % second["id"]).status_code == 200
        assert len(admin_again.get("/api/fuel?vehicle_id=1").json()) == 1


PNG_BYTES = __import__("base64").b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_receipt_upload_view_delete(tmp_path):
    main.DB_PATH = tmp_path / "receipts.db"
    main.RECEIPTS_DIR = tmp_path / "receipts"
    main.RECEIPTS_DIR.mkdir(exist_ok=True)
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        admin.post(
            "/api/services",
            json={
                "vehicle_id": 1,
                "date": "2026-09-20",
                "mileage": 24000,
                "type": "Oil change",
                "cost": 55,
            },
        )
        admin.post(
            "/api/fuel",
            json={
                "vehicle_id": 1,
                "date": "2026-09-20",
                "odometer": 24000,
                "gallons": 10,
                "cost": 35,
            },
        )
        created = admin.post(
            "/api/receipts",
            data={"kind": "service", "entry_id": "1"},
            files={"file": ("receipt.png", PNG_BYTES, "image/png")},
        )
        assert created.status_code == 201
        rid = created.json()["id"]
        assert (
            admin.post(
                "/api/receipts",
                data={"kind": "fuel", "entry_id": "1"},
                files={"file": ("fuel.png", PNG_BYTES, "image/png")},
            ).status_code
            == 201
        )
        assert (
            admin.post(
                "/api/receipts",
                data={"kind": "service", "entry_id": "1"},
                files={"file": ("notes.txt", b"nope", "text/plain")},
            ).status_code
            == 400
        )
        assert len(admin.get("/api/receipts?kind=service&entry_id=1").json()) == 1
        served = admin.get(f"/api/receipts/{rid}")
        assert served.status_code == 200 and served.content == PNG_BYTES
        assert admin.delete("/api/services/1").status_code == 200
        assert admin.get("/api/receipts?kind=service&entry_id=1").json() == []
        assert admin.get(f"/api/receipts/{rid}").status_code == 404
        remaining = admin.get("/api/receipts?kind=fuel&entry_id=1").json()
        assert admin.delete(f"/api/receipts/{remaining[0]['id']}").status_code == 200
    with TestClient(main.app) as anon:
        assert anon.get("/api/receipts").status_code == 401


def test_api_tokens_and_v1_endpoints(tmp_path):
    main.DB_PATH = tmp_path / "tokens.db"
    main.RECEIPTS_DIR = tmp_path / "receipts"
    main.RECEIPTS_DIR.mkdir(exist_ok=True)
    main._login_failures.clear()
    main._api_calls.clear()
    main._api_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        created = admin.post("/api/tokens", json={"name": "script"})
        assert created.status_code == 201
        token = created.json()["token"]
        assert token.startswith("gar_") and created.json()["prefix"] == token[:11]
        assert "token" not in admin.get("/api/tokens").json()[0]
        admin.post(
            "/api/users",
            json={
                "username": "member-test",
                "password": "password-456",
                "is_admin": False,
            },
        )
        auth = {"Authorization": f"Bearer {token}"}
        assert (
            admin.get("/api/v1/vehicles", headers=auth).json()[0]["name"]
            == "Ford Mustang"
        )
        service = admin.post(
            "/api/v1/vehicles/1/services",
            headers=auth,
            json={
                "date": "2026-09-20",
                "mileage": 24000,
                "type": "Oil change",
                "cost": 55,
            },
        )
        assert (
            service.status_code == 201 and service.json()["logged_by"] == "admin-test"
        )
        assert len(admin.get("/api/v1/vehicles/1/services", headers=auth).json()) == 1
        fuel = admin.post(
            "/api/v1/vehicles/1/fuel",
            headers=auth,
            data={
                "date": "2026-09-20",
                "odometer": "24000",
                "gallons": "10",
                "cost": "35",
            },
            files={"file": ("receipt.png", PNG_BYTES, "image/png")},
        )
        assert fuel.status_code == 201 and fuel.json()["receipt"]["mime"] == "image/png"
        assert len(admin.get("/api/v1/vehicles/1/fuel", headers=auth).json()) == 1
        admin.post(
            "/api/reminders",
            json={
                "vehicle_id": 1,
                "name": "Oil change",
                "miles_interval": 5000,
                "months_interval": None,
                "last_date": "2026-09-01",
                "last_mileage": 20000,
            },
        )
        status = admin.get("/api/v1/vehicles/1/maintenance", headers=auth).json()
        assert (
            status[0]["status"] == "soon" and status[0]["label"] == "1,000 mi remaining"
        )
        assert (
            admin.get(
                "/api/v1/vehicles", headers={"Authorization": "Bearer gar_wrong"}
            ).status_code
            == 401
        )
        tid = created.json()["id"]
        assert admin.delete(f"/api/tokens/{tid}").status_code == 200
        assert admin.get("/api/v1/vehicles", headers=auth).status_code == 401
    with TestClient(main.app) as member:
        member.post(
            "/api/login", json={"username": "member-test", "password": "password-456"}
        )
        assert member.get("/api/tokens").json() == []
        assert member.post("/api/tokens", json={"name": "x"}).status_code == 201


def test_member_tokens_are_self_managed_and_vehicle_scoped(tmp_path):
    main.DB_PATH = tmp_path / "member-tokens.db"
    main.RECEIPTS_DIR = tmp_path / "receipts"
    main.RECEIPTS_DIR.mkdir(exist_ok=True)
    main._login_failures.clear()
    main._api_calls.clear()
    main._api_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        admin_token = admin.post("/api/tokens", json={"name": "admin script"}).json()
        member_user = admin.post(
            "/api/users",
            json={
                "username": "member-test",
                "password": "password-456",
                "is_admin": False,
            },
        ).json()
        other_vehicle_id = admin.get("/api/vehicles").json()[0]["id"]
    with TestClient(main.app) as member:
        assert (
            member.post(
                "/api/login",
                json={"username": "member-test", "password": "password-456"},
            ).status_code
            == 200
        )
        own_vehicle = member.post(
            "/api/vehicles",
            json={"name": "Member car", "year": "2020", "mileage": 1000},
        ).json()
        created = member.post("/api/tokens", json={"name": "member script"})
        assert created.status_code == 201
        member_token = created.json()
        listed = member.get("/api/tokens").json()
        assert [item["id"] for item in listed] == [member_token["id"]]
        assert admin_token["id"] not in {item["id"] for item in listed}
        assert member.delete(f"/api/tokens/{admin_token['id']}").status_code == 404
        auth = {"Authorization": f"Bearer {member_token['token']}"}
        vehicles = member.get("/api/v1/vehicles", headers=auth).json()
        assert [vehicle["id"] for vehicle in vehicles] == [own_vehicle["id"]]
        service = member.post(
            f"/api/v1/vehicles/{own_vehicle['id']}/services",
            headers=auth,
            json={
                "date": "2026-09-22",
                "mileage": 1100,
                "type": "Oil change",
                "cost": 40,
            },
        )
        assert (
            service.status_code == 201 and service.json()["logged_by"] == "member-test"
        )
        assert (
            member.get(
                f"/api/v1/vehicles/{own_vehicle['id']}/services", headers=auth
            ).status_code
            == 200
        )
        assert (
            member.get(
                f"/api/v1/vehicles/{other_vehicle_id}/services", headers=auth
            ).status_code
            == 404
        )
        assert (
            member.post(
                f"/api/v1/vehicles/{other_vehicle_id}/services",
                headers=auth,
                json={"date": "2026-09-22", "mileage": 1100, "type": "Nope", "cost": 0},
            ).status_code
            == 404
        )
        foreign_routes = [
            ("get", f"/api/v1/vehicles/{other_vehicle_id}/specs", None),
            ("put", f"/api/v1/vehicles/{other_vehicle_id}/specs", {}),
            ("get", f"/api/v1/vehicles/{other_vehicle_id}/maintenance", None),
            ("get", f"/api/v1/vehicles/{other_vehicle_id}/mods", None),
            ("post", f"/api/v1/vehicles/{other_vehicle_id}/mods", {"name": "Nope"}),
            ("get", f"/api/v1/vehicles/{other_vehicle_id}/fuel", None),
            ("put", f"/api/v1/vehicles/{other_vehicle_id}/mileage", {"mileage": 1200}),
            ("get", f"/api/v1/vehicles/{other_vehicle_id}/notes", None),
            (
                "post",
                f"/api/v1/vehicles/{other_vehicle_id}/notes",
                {"date": "2026-09-22", "body": "Nope"},
            ),
        ]
        for method, path, payload in foreign_routes:
            response = getattr(member, method)(
                path, headers=auth, **({"json": payload} if payload is not None else {})
            )
            assert response.status_code == 404, (method, path, response.text)
        assert (
            member.post(
                f"/api/v1/vehicles/{other_vehicle_id}/fuel",
                headers=auth,
                data={
                    "date": "2026-09-22",
                    "odometer": "1200",
                    "gallons": "10",
                    "cost": "30",
                },
            ).status_code
            == 404
        )
    with TestClient(main.app) as admin_again:
        admin_again.post(
            "/api/login", json={"username": "admin-test", "password": "password-123"}
        )
        ids = {item["id"] for item in admin_again.get("/api/tokens").json()}
        assert ids == {admin_token["id"], member_token["id"]}
        assert (
            admin_again.delete(f"/api/tokens/{member_token['id']}").status_code == 200
        )
    with TestClient(main.app) as member_again:
        member_again.post(
            "/api/login", json={"username": "member-test", "password": "password-456"}
        )
        assert member_again.get("/api/tokens").json() == []


def test_api_invalid_token_rate_limit(tmp_path):
    main.DB_PATH = tmp_path / "api-limit.db"
    main._login_failures.clear()
    main._api_calls.clear()
    main._api_failures.clear()
    main.init_db()
    with TestClient(main.app) as client:
        client.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        for _ in range(main.API_FAIL_LIMIT):
            assert (
                client.get(
                    "/api/v1/vehicles", headers={"Authorization": "Bearer gar_wrong"}
                ).status_code
                == 401
            )
        response = client.get(
            "/api/v1/vehicles", headers={"Authorization": "Bearer gar_wrong"}
        )
        assert response.status_code == 429 and int(response.headers["retry-after"]) > 0


def test_service_can_reset_maintenance_item(tmp_path):
    main.DB_PATH = tmp_path / "reset.db"
    main._login_failures.clear()
    main._api_calls.clear()
    main._api_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        reminder = admin.post(
            "/api/reminders",
            json={
                "vehicle_id": 1,
                "name": "Oil change",
                "miles_interval": 5000,
                "months_interval": 6,
                "last_date": "2026-03-01",
                "last_mileage": 20000,
            },
        ).json()
        before = len(admin.get("/api/services?vehicle_id=1").json())
        service = admin.post(
            "/api/services",
            json={
                "vehicle_id": 1,
                "date": "2026-09-20",
                "mileage": 24000,
                "type": "Oil change",
                "cost": 55,
                "reminder_id": reminder["id"],
            },
        )
        assert (
            service.status_code == 200
            and service.json()["reminder_id"] == reminder["id"]
        )
        updated = admin.get("/api/reminders?vehicle_id=1").json()[0]
        assert updated["last_date"] == "2026-09-20" and updated["last_mileage"] == 24000
        assert len(admin.get("/api/services?vehicle_id=1").json()) == before + 1
        assert (
            admin.post(
                "/api/services",
                json={
                    "vehicle_id": 1,
                    "date": "2026-09-20",
                    "mileage": 24000,
                    "type": "X",
                    "reminder_id": 999,
                },
            ).status_code
            == 400
        )
        token = admin.post("/api/tokens", json={"name": "script"}).json()["token"]
        via_api = admin.post(
            "/api/v1/vehicles/1/services",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "date": "2026-09-21",
                "mileage": 24100,
                "type": "Oil change",
                "reminder_id": reminder["id"],
            },
        )
        assert via_api.status_code == 201
        assert (
            admin.get("/api/reminders?vehicle_id=1").json()[0]["last_mileage"] == 24100
        )
        undated = admin.post(
            "/api/services",
            json={
                "vehicle_id": 1,
                "date": None,
                "mileage": 24200,
                "type": "Undated service",
                "reminder_id": reminder["id"],
            },
        )
        assert (
            undated.status_code == 200
            and undated.json()["date"] is None
            and undated.json()["reminder_id"] is None
        )
        assert (
            admin.get("/api/reminders?vehicle_id=1").json()[0]["last_mileage"] == 24100
        )
        undated_v1 = admin.post(
            "/api/v1/vehicles/1/services",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "mileage": 24300,
                "type": "Undated API service",
                "reminder_id": reminder["id"],
            },
        )
        assert (
            undated_v1.status_code == 201
            and undated_v1.json()["date"] is None
            and undated_v1.json()["reminder_id"] is None
        )
        assert (
            admin.get("/api/reminders?vehicle_id=1").json()[0]["last_mileage"] == 24100
        )


def test_estimated_mileage_from_fillups_and_services(tmp_path):
    main.DB_PATH = tmp_path / "estimate.db"
    main._login_failures.clear()
    main._api_calls.clear()
    main._api_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        admin.post(
            "/api/services",
            json={
                "vehicle_id": 1,
                "date": "2026-06-01",
                "mileage": 20000,
                "type": "Tires",
            },
        )
        admin.post(
            "/api/fuel",
            json={
                "vehicle_id": 1,
                "date": "2026-09-01",
                "odometer": 23000,
                "gallons": 10,
                "cost": 35,
            },
        )
        vehicle = admin.get("/api/vehicles").json()[0]
        assert vehicle["mileage"] == 23000
        assert vehicle["miles_per_day"] == 32.6  # 3000 miles over 92 days
        assert vehicle["est_mileage"] >= 23000
        with main.db() as c:
            row = c.execute("SELECT * FROM vehicles WHERE id=1").fetchone()
            est = main.mileage_estimate(
                c, 1, today=__import__("datetime").date(2026, 10, 1)
            )
            assert est["est_mileage"] == 23000 + round(3000 / 92 * 30)
            assert (
                main.effective_mileage(c, row)
                == main.mileage_estimate(c, 1)["est_mileage"]
            )
            status = main.reminder_status(
                est["est_mileage"],
                {
                    "miles_interval": 3400,
                    "months_interval": None,
                    "last_mileage": 20000,
                    "last_date": "2026-06-01",
                    "due_date": None,
                    "repeats_yearly": False,
                },
                today=__import__("datetime").date(2026, 10, 1),
            )
            assert status["state"] == "overdue"
            fresh = main.reminder_status(
                20000,
                {
                    "miles_interval": 3400,
                    "months_interval": None,
                    "last_mileage": 20000,
                    "last_date": "2026-06-01",
                    "due_date": None,
                    "repeats_yearly": False,
                },
                today=__import__("datetime").date(2026, 10, 1),
            )
            assert fresh["state"] == "ok"


def test_vehicle_photos(tmp_path):
    main.DB_PATH = tmp_path / "photos.db"
    main.RECEIPTS_DIR = tmp_path / "receipts"
    main.RECEIPTS_DIR.mkdir(exist_ok=True)
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        photo = admin.post(
            "/api/vehicles/1/photo", files={"file": ("car.png", PNG_BYTES, "image/png")}
        )
        assert photo.status_code == 201
        vehicle = admin.get("/api/vehicles").json()[0]
        assert vehicle["photo_receipt_id"] == photo.json()["id"]
        assert vehicle["photo_url"] == f"/api/receipts/{photo.json()['id']}"
        assert admin.get(vehicle["photo_url"]).content == PNG_BYTES
        assert (
            admin.put("/api/settings", json={"use_vehicle_photos": True}).json()[
                "use_vehicle_photos"
            ]
            is True
        )
        replacement = admin.post(
            "/api/vehicles/1/photo",
            files={"file": ("car2.png", PNG_BYTES, "image/png")},
        ).json()
        assert admin.get(f"/api/receipts/{photo.json()['id']}").status_code == 404
        assert (
            admin.get("/api/vehicles").json()[0]["photo_receipt_id"]
            == replacement["id"]
        )
        assert admin.delete(f"/api/receipts/{replacement['id']}").status_code == 200
        assert admin.get("/api/vehicles").json()[0]["photo_receipt_id"] is None
        assert (
            admin.post(
                "/api/vehicles/1/photo", files={"file": ("x.txt", b"no", "text/plain")}
            ).status_code
            == 400
        )


def test_notification_settings_and_test_button(tmp_path, monkeypatch):
    main.DB_PATH = tmp_path / "notify.db"
    main._login_failures.clear()
    main.init_db()
    sent = []
    monkeypatch.setattr(
        main,
        "send_notification",
        lambda urls, title, body: (sent.append((urls, title, body)) or (True, "")),
    )
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        assert admin.get("/api/notifications").json() == {"apprise_urls": ""}
        assert (
            admin.put(
                "/api/notifications", json={"apprise_urls": "not-a-url"}
            ).status_code
            == 400
        )
        assert admin.put(
            "/api/notifications", json={"apprise_urls": "tgram://token/chat"}
        ).json() == {"apprise_urls": "tgram://token/chat"}
        assert admin.post("/api/notifications/test").status_code == 200
        assert sent and sent[0][0] == "tgram://token/chat"
        admin.post(
            "/api/users",
            json={
                "username": "member-test",
                "password": "password-456",
                "is_admin": False,
            },
        )
    with TestClient(main.app) as member:
        member.post(
            "/api/login", json={"username": "member-test", "password": "password-456"}
        )
        assert member.get("/api/notifications").status_code == 403
        assert (
            member.put(
                "/api/notifications", json={"apprise_urls": "tgram://x/y"}
            ).status_code
            == 403
        )
        assert member.post("/api/notifications/test").status_code == 403


def test_daily_check_notifies_only_newly_due(tmp_path, monkeypatch):
    main.DB_PATH = tmp_path / "due.db"
    main._login_failures.clear()
    main.init_db()
    sent = []
    monkeypatch.setattr(
        main,
        "send_notification",
        lambda urls, title, body: (sent.append(body) or (True, "")),
    )
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        admin.put(
            "/api/notifications", json={"apprise_urls": "json://example.invalid/hook"}
        )
        admin.post(
            "/api/services",
            json={
                "vehicle_id": 1,
                "date": "2026-09-20",
                "mileage": 20000,
                "type": "Tires",
            },
        )
        admin.post(
            "/api/reminders",
            json={
                "vehicle_id": 1,
                "name": "Oil change",
                "miles_interval": 5000,
                "months_interval": None,
                "last_date": "2026-09-01",
                "last_mileage": 20000,
            },
        )
        assert main.run_notification_check() is False  # 0% used: nothing due
        admin.post(
            "/api/fuel",
            json={
                "vehicle_id": 1,
                "date": "2026-09-20",
                "odometer": 24500,
                "gallons": 10,
                "cost": 35,
            },
        )
        assert main.run_notification_check() is True  # 90% used: newly due soon
        assert len(sent) == 1 and "Oil change" in sent[0] and "Due soon" in sent[0]
        assert main.run_notification_check() is False  # same state: no repeat
        admin.post(
            "/api/fuel",
            json={
                "vehicle_id": 1,
                "date": "2026-09-21",
                "odometer": 25100,
                "gallons": 10,
                "cost": 35,
            },
        )
        assert main.run_notification_check() is True  # escalated to overdue
        assert len(sent) == 2 and "OVERDUE" in sent[1]
        # fixing the item clears state, so a later due item notifies again
        admin.put(
            "/api/reminders/1",
            json={
                "vehicle_id": 1,
                "name": "Oil change",
                "miles_interval": 5000,
                "months_interval": None,
                "last_date": "2026-09-21",
                "last_mileage": 25100,
            },
        )
        assert main.run_notification_check() is False
        admin.post(
            "/api/fuel",
            json={
                "vehicle_id": 1,
                "date": "2026-09-22",
                "odometer": 29650,
                "gallons": 10,
                "cost": 35,
            },
        )
        assert main.run_notification_check() is True and len(sent) == 3


def test_webhook_fallback_posts_json(tmp_path):
    import http.server, threading as th

    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    th.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ok, detail = main.send_notification(
            f"http://127.0.0.1:{server.server_port}/hook", "T", "B"
        )
        assert ok, detail
        assert received and __import__("json").loads(received[0]) == {
            "title": "T",
            "body": "B",
        }
    finally:
        server.shutdown()


def test_vehicle_notes_and_hide_setting(tmp_path):
    main.DB_PATH = tmp_path / "notes.db"
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        note = admin.post(
            "/api/notes",
            json={
                "vehicle_id": 1,
                "date": "2026-09-21",
                "body": "Tire pressure is 32 psi",
            },
        ).json()
        assert (
            note["body"] == "Tire pressure is 32 psi"
            and note["logged_by"] == "admin-test"
        )
        updated = admin.put(
            f"/api/notes/{note['id']}",
            json={
                "vehicle_id": 1,
                "date": "2026-09-21",
                "body": "Tire pressure is 35 psi",
            },
        ).json()
        assert updated["body"] == "Tire pressure is 35 psi"
        rows = admin.get("/api/notes?vehicle_id=1").json()
        assert len(rows) == 1 and rows[0]["body"] == "Tire pressure is 35 psi"
        settings = admin.put("/api/settings", json={"hide_notes": True}).json()
        assert settings["hide_notes"] is True
        token = admin.post("/api/tokens", json={"name": "notes-script"}).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        v1_note = admin.post(
            "/api/v1/vehicles/1/notes",
            json={"date": "2026-09-21", "body": "Check the spare"},
            headers=auth,
        )
        assert v1_note.status_code == 201
        v1_rows = admin.get("/api/v1/vehicles/1/notes", headers=auth).json()
        assert [n["body"] for n in v1_rows] == [
            "Check the spare",
            "Tire pressure is 35 psi",
        ]
        assert admin.get("/api/v1/vehicles/1/notes").status_code == 401
        assert admin.delete(f"/api/notes/{note['id']}").json() == {"ok": True}
        assert admin.delete(f"/api/notes/{note['id']}").status_code == 404


def test_v1_mileage_update(tmp_path):
    main.DB_PATH = tmp_path / "mileage.db"
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        token = admin.post("/api/tokens", json={"name": "mileage-script"}).json()[
            "token"
        ]
        auth = {"Authorization": f"Bearer {token}"}
        assert (
            admin.put(
                "/api/v1/vehicles/1/mileage",
                json={"mileage": 25000, "date": "2026-09-21"},
            ).status_code
            == 401
        )
        updated = admin.put(
            "/api/v1/vehicles/1/mileage",
            json={"mileage": 25000, "date": "2026-09-21"},
            headers=auth,
        ).json()
        assert updated["mileage"] == 25000
        vehicle = admin.get("/api/v1/vehicles", headers=auth).json()[0]
        assert vehicle["mileage"] == 25000
        assert (
            admin.put(
                "/api/v1/vehicles/1/mileage", json={"mileage": -5}, headers=auth
            ).status_code
            == 422
        )
        assert (
            admin.put(
                "/api/v1/vehicles/99/mileage", json={"mileage": 100}, headers=auth
            ).status_code
            == 404
        )


def test_disabled_users_and_private_vehicle_visibility(tmp_path):
    main.DB_PATH = tmp_path / "privacy.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        member = admin.post(
            "/api/users", json={"username": "member", "password": "password-456"}
        ).json()
        other = admin.post(
            "/api/users", json={"username": "other", "password": "password-789"}
        ).json()
        shared = admin.post(
            "/api/vehicles",
            json={"name": "Shared", "year": "2024", "mileage": 1, "icon": "🚗"},
        ).json()
        private = admin.post(
            "/api/vehicles",
            json={
                "name": "Member private",
                "year": "2025",
                "mileage": 2,
                "icon": "🚙",
                "owner_id": member["id"],
                "private": True,
            },
        ).json()
        hidden = admin.post(
            "/api/vehicles",
            json={
                "name": "Other private",
                "year": "2026",
                "mileage": 3,
                "icon": "🛻",
                "owner_id": other["id"],
                "private": True,
            },
        ).json()
        admin.put("/api/me/vehicle-view", json={"show_all": True})
        for vid in (shared["id"], private["id"], hidden["id"]):
            assert (
                admin.post(
                    "/api/services",
                    json={
                        "vehicle_id": vid,
                        "date": "2026-01-01",
                        "mileage": 1,
                        "type": "Check",
                    },
                ).status_code
                == 200
            )
        assert {v["name"] for v in admin.get("/api/vehicles").json()} >= {
            "Shared",
            "Member private",
            "Other private",
        }
        assert (
            admin.put(f"/api/users/{member['id']}", json={"active": False}).status_code
            == 200
        )
        assert (
            admin.put(
                f"/api/users/{admin.get('/api/me').json()['id']}",
                json={"active": False},
            ).status_code
            == 400
        )
    with TestClient(main.app) as disabled:
        assert (
            disabled.post(
                "/api/login", json={"username": "member", "password": "password-456"}
            ).status_code
            == 401
        )
    with TestClient(main.app) as admin:
        admin.post("/api/login", json={"username": "admin", "password": "password-123"})
        assert (
            admin.put(f"/api/users/{member['id']}", json={"active": True}).status_code
            == 200
        )
    with TestClient(main.app) as member_client:
        assert (
            member_client.post(
                "/api/login", json={"username": "member", "password": "password-456"}
            ).status_code
            == 200
        )
        names = {v["name"] for v in member_client.get("/api/vehicles").json()}
        assert names == {"Member private"}
        services = member_client.get("/api/services").json()
        assert {s["vehicle_id"] for s in services} == {private["id"]}
        assert (
            member_client.post(
                "/api/fuel",
                json={
                    "vehicle_id": hidden["id"],
                    "date": "2026-01-01",
                    "odometer": 5,
                    "gallons": 1,
                    "cost": 3,
                },
            ).status_code
            == 404
        )
        assert (
            member_client.post(
                "/api/notes",
                json={"vehicle_id": hidden["id"], "date": "2026-01-01", "body": "no"},
            ).status_code
            == 404
        )
        assert (
            member_client.post(
                "/api/mods", json={"vehicle_id": hidden["id"], "name": "no", "price": 1}
            ).status_code
            == 404
        )
        assert (
            member_client.put(
                f"/api/vehicles/{hidden['id']}",
                json={"name": "No", "year": "2026", "mileage": 3, "icon": "🛻"},
            ).status_code
            == 404
        )


def test_admin_vehicle_view_defaults_filtered_and_persists(tmp_path):
    main.DB_PATH = tmp_path / "admin-view.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        friend = admin.post(
            "/api/users", json={"username": "friend", "password": "password-456"}
        ).json()
        private = admin.post(
            "/api/vehicles",
            json={"name": "Friend private", "owner_id": friend["id"], "private": True},
        ).json()
        assert "Friend private" not in {
            v["name"] for v in admin.get("/api/vehicles").json()
        }
        enabled = admin.put("/api/me/vehicle-view", json={"show_all": True})
        assert (
            enabled.status_code == 200 and enabled.json()["show_all_vehicles"] is True
        )
        assert "Friend private" in {
            v["name"] for v in admin.get("/api/vehicles").json()
        }
    with TestClient(main.app) as admin_again:
        admin_again.post(
            "/api/login", json={"username": "admin", "password": "password-123"}
        )
        assert admin_again.get("/api/me").json()["show_all_vehicles"] is True
        admin_again.put("/api/me/vehicle-view", json={"show_all": False})
        assert "Friend private" not in {
            v["name"] for v in admin_again.get("/api/vehicles").json()
        }
    with TestClient(main.app) as friend_client:
        friend_client.post(
            "/api/login", json={"username": "friend", "password": "password-456"}
        )
        assert (
            friend_client.put(
                "/api/me/vehicle-view", json={"show_all": True}
            ).status_code
            == 403
        )
        assert "Friend private" in {
            v["name"] for v in friend_client.get("/api/vehicles").json()
        }


def test_vehicle_specs_and_mod_install_notes(tmp_path):
    main.DB_PATH = tmp_path / "specs.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        vehicle = admin.post(
            "/api/vehicles",
            json={
                "name": "Test Coupe",
                "fuel_type": "Premium gasoline",
                "tire_size": "215/60R16",
                "oil_spec": "5W-30, 5 qt",
            },
        ).json()
        assert (
            vehicle["fuel_type"] == "Premium gasoline"
            and vehicle["tire_size"] == "215/60R16"
            and vehicle["oil_spec"] == "5W-30, 5 qt"
        )
        vehicle = admin.put(
            f"/api/vehicles/{vehicle['id']}", json={**vehicle, "oil_spec": ""}
        ).json()
        assert vehicle["oil_spec"] == ""
        mod = admin.post(
            "/api/mods",
            json={
                "vehicle_id": vehicle["id"],
                "name": "Coilovers",
                "price": 900,
                "torque_specs": "Top nuts 30 lb-ft",
                "fluids": "Anti-seize",
                "gotchas": "Support the hub",
                "youtube_url": "https://youtube.com/watch?v=test",
            },
        ).json()
        assert mod["torque_specs"] == "Top nuts 30 lb-ft" and mod[
            "youtube_url"
        ].startswith("https://youtube.com/")
        mod = admin.put(f"/api/mods/{mod['id']}", json={**mod, "fluids": ""}).json()
        assert mod["fluids"] == "" and mod["gotchas"] == "Support the hub"


def test_general_due_date_reminders_and_fuel_octane(tmp_path):
    main.DB_PATH = tmp_path / "renewals.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        inspection = admin.post(
            "/api/reminders",
            json={
                "vehicle_id": 1,
                "name": "State inspection",
                "due_date": "2026-10-01",
                "last_date": "2026-01-01",
                "last_mileage": 0,
            },
        ).json()
        registration = admin.post(
            "/api/reminders",
            json={
                "vehicle_id": 1,
                "name": "Tag renewal",
                "due_date": "2026-11-15",
                "last_date": "2026-01-01",
                "last_mileage": 0,
            },
        ).json()
        assert (
            inspection["due_date"] == "2026-10-01"
            and registration["name"] == "Tag renewal"
        )
        assert (
            admin.put(
                f"/api/reminders/{inspection['id']}",
                json={**inspection, "due_date": "2026-10-15"},
            ).json()["due_date"]
            == "2026-10-15"
        )
        fuel = admin.post(
            "/api/fuel",
            json={
                "vehicle_id": 1,
                "date": "2026-09-21",
                "odometer": 100,
                "gallons": 5,
                "cost": 20,
                "octane": "93",
            },
        ).json()
        assert fuel["octane"] == "93"
        fuel = admin.put(
            f"/api/fuel/{fuel['id']}", json={**fuel, "octane": "E30"}
        ).json()
        assert fuel["octane"] == "E30"
        unset = admin.post(
            "/api/fuel",
            json={
                "vehicle_id": 1,
                "date": "2026-09-22",
                "odometer": 200,
                "gallons": 5,
                "cost": 20,
            },
        ).json()
        assert unset["octane"] == ""


def test_yearly_and_service_reminder_shapes(tmp_path):
    main.DB_PATH = tmp_path / "reminder-types.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        vehicle = admin.post(
            "/api/vehicles",
            json={"name": "2020 Test Car", "year": "2020", "mileage": 1000},
        ).json()
        renewal = admin.post(
            "/api/reminders",
            json={
                "vehicle_id": vehicle["id"],
                "name": "Registration",
                "due_date": "2025-03-15",
                "repeats_yearly": True,
                "last_date": "2025-01-01",
                "last_mileage": 0,
            },
        ).json()
        assert renewal["due_date"] == "2025-03-15" and renewal["repeats_yearly"] is True
        service = admin.post(
            "/api/reminders",
            json={
                "vehicle_id": vehicle["id"],
                "name": "Oil",
                "miles_interval": 5000,
                "months_interval": 6,
                "due_date": None,
                "repeats_yearly": False,
                "last_date": "2026-01-01",
                "last_mileage": 1000,
            },
        ).json()
        assert service["miles_interval"] == 5000 and service["due_date"] is None
        changed = admin.put(
            f"/api/reminders/{renewal['id']}", json={**renewal, "repeats_yearly": False}
        ).json()
        assert changed["repeats_yearly"] is False


def test_private_vehicle_receipts_are_not_exposed(tmp_path):
    main.DB_PATH = tmp_path / "receipt-auth.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        private = admin.post(
            "/api/vehicles",
            json={
                "name": "2020 Private Car",
                "year": "2020",
                "mileage": 1,
                "private": True,
            },
        ).json()
        fuel = admin.post(
            "/api/fuel",
            json={
                "vehicle_id": private["id"],
                "date": "2026-09-21",
                "odometer": 2,
                "gallons": 1,
                "cost": 1,
            },
        ).json()
        admin.post(
            "/api/users",
            json={"username": "member", "password": "password-123", "is_admin": False},
        )
        upload = admin.post(
            "/api/receipts",
            data={"kind": "fuel", "entry_id": fuel["id"]},
            files={"file": ("receipt.png", b"png", "image/png")},
        )
        receipt_id = upload.json()["id"]
    with TestClient(main.app) as member:
        member.post(
            "/api/login", json={"username": "member", "password": "password-123"}
        )
        assert member.get("/api/receipts").json() == []
        assert member.get(f"/api/receipts/{receipt_id}").status_code == 404
        assert member.delete(f"/api/receipts/{receipt_id}").status_code == 404
        denied = member.post(
            "/api/receipts",
            data={"kind": "fuel", "entry_id": fuel["id"]},
            files={"file": ("receipt.png", b"png", "image/png")},
        )
        assert denied.status_code == 404


def test_service_install_notes_round_trip(tmp_path):
    main.DB_PATH = tmp_path / "service-notes.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        vehicle = admin.post(
            "/api/vehicles",
            json={"name": "2020 Test Car", "year": "2020", "mileage": 1000},
        ).json()
        service = admin.post(
            "/api/services",
            json={
                "vehicle_id": vehicle["id"],
                "date": "2026-09-21",
                "mileage": 1100,
                "type": "Oil change",
                "cost": 40,
                "provider": "DIY",
                "notes": "Done",
                "torque_specs": "29 lb-ft",
                "fluids": "5W-30, 5 qt",
                "gotchas": "Replace washer",
                "youtube_url": "https://youtube.com/watch?v=test",
            },
        ).json()
        assert service["torque_specs"] == "29 lb-ft"
        assert service["fluids"] == "5W-30, 5 qt"
        service = admin.put(
            f"/api/services/{service['id']}", json={**service, "gotchas": ""}
        ).json()
        assert service["gotchas"] == "" and service["youtube_url"].startswith(
            "https://youtube.com/"
        )


def test_private_entry_cannot_be_moved_by_another_member(tmp_path):
    main.DB_PATH = tmp_path / "entry-move-auth.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        private = admin.post(
            "/api/vehicles",
            json={
                "name": "2020 Private Car",
                "year": "2020",
                "mileage": 1,
                "private": True,
            },
        ).json()
        service = admin.post(
            "/api/services",
            json={
                "vehicle_id": private["id"],
                "date": "2026-09-21",
                "mileage": 2,
                "type": "Secret",
                "cost": 1,
            },
        ).json()
        admin.post(
            "/api/users",
            json={"username": "member", "password": "password-123", "is_admin": False},
        )
        public = admin.post(
            "/api/vehicles",
            json={
                "name": "2021 Public Car",
                "year": "2021",
                "mileage": 1,
                "private": False,
            },
        ).json()
    with TestClient(main.app) as member:
        member.post(
            "/api/login", json={"username": "member", "password": "password-123"}
        )
        moved = {**service, "vehicle_id": public["id"]}
        assert (
            member.put(f"/api/services/{service['id']}", json=moved).status_code == 404
        )


def test_v1_notes_endpoint_returns_notes(tmp_path):
    main.DB_PATH = tmp_path / "v1-notes.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        vehicle = admin.post(
            "/api/vehicles",
            json={"name": "2020 Test Car", "year": "2020", "mileage": 1},
        ).json()
        admin.post(
            "/api/notes",
            json={"vehicle_id": vehicle["id"], "date": "2026-09-22", "body": "hello"},
        )
        token = admin.post("/api/tokens", json={"name": "test"}).json()["token"]
    response = TestClient(main.app).get(
        f"/api/v1/vehicles/{vehicle['id']}/notes",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200 and response.json()[0]["body"] == "hello"


def test_backup_is_admin_only_and_round_trips_current_fields(tmp_path):
    main.DB_PATH = tmp_path / "backup-v3.db"
    main.RECEIPTS_DIR = tmp_path / "receipts"
    main.RECEIPTS_DIR.mkdir()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        vehicle = admin.post(
            "/api/vehicles",
            json={
                "name": "2024 Backup Car",
                "year": "2024",
                "mileage": 10,
                "private": True,
                "fuel_type": "Premium",
                "tire_size": "225/45R17",
                "oil_spec": "5W-30",
            },
        ).json()
        specs = admin.put(
            f"/api/vehicles/{vehicle['id']}/specs", json={"wheel_size": "17 × 7.5 in"}
        ).json()
        assert specs["wheel_size"] == "17 × 7.5 in"
        service = admin.post(
            "/api/services",
            json={
                "vehicle_id": vehicle["id"],
                "date": "2026-09-22",
                "mileage": 11,
                "type": "Oil",
                "cost": 40,
                "torque_specs": "29 lb-ft",
                "fluids": "5 qt",
                "gotchas": "washer",
                "youtube_url": "https://youtube.com/watch?v=x",
            },
        ).json()
        admin.post(
            "/api/reminders",
            json={
                "vehicle_id": vehicle["id"],
                "name": "Tag",
                "due_date": "2026-10-01",
                "repeats_yearly": True,
                "last_date": "2026-01-01",
                "last_mileage": 0,
            },
        )
        admin.post(
            "/api/notes",
            json={"vehicle_id": vehicle["id"], "date": "2026-09-22", "body": "note"},
        )
        admin.post(
            "/api/mods",
            json={
                "vehicle_id": vehicle["id"],
                "name": "Mod",
                "price": 1,
                "torque_specs": "10",
                "gotchas": "careful",
                "youtube_url": "https://youtube.com/watch?v=y",
            },
        )
        admin.post(
            "/api/fuel",
            json={
                "vehicle_id": vehicle["id"],
                "date": "2026-09-22",
                "odometer": 12,
                "gallons": 1,
                "cost": 4,
                "octane": "93",
            },
        )
        assert (
            admin.post(
                "/api/receipts",
                data={"kind": "service", "entry_id": service["id"]},
                files={"file": ("r.png", b"png", "image/png")},
            ).status_code
            == 201
        )
        admin.post(
            "/api/users",
            json={"username": "member", "password": "password-123", "is_admin": False},
        )
        exported = admin.get("/api/export")
        assert exported.status_code == 200, exported.text
        backup = exported.json()
        assert (
            backup["version"] == 3
            and backup["tables"]["notes"]
            and backup["tables"]["modifications"]
        )
        assert (
            backup["tables"]["reminders"][0]["repeats_yearly"] == 1
            and backup["tables"]["fuel_entries"][0]["octane"] == "93"
        )
        assert (
            backup["tables"]["services"][0]["fluids"] == "5 qt"
            and backup["receipt_files"]
        )
        assert (
            next(
                row
                for row in backup["tables"]["vehicles"]
                if row["id"] == vehicle["id"]
            )["tire_size"]
            == ""
        )
        assert (
            next(
                row
                for row in backup["tables"]["vehicle_specs"]
                if row["vehicle_id"] == vehicle["id"]
            )["tire_size"]
            == "225/45R17"
        )
        assert (
            next(
                row
                for row in backup["tables"]["vehicle_specs"]
                if row["vehicle_id"] == vehicle["id"]
            )["wheel_size"]
            == "17 × 7.5 in"
        )
        restored = admin.post("/api/import", json=backup)
        assert restored.status_code == 200 and restored.json()["version"] == 3
        assert (
            admin.post(
                "/api/login", json={"username": "admin", "password": "password-123"}
            ).status_code
            == 200
        )
        again = admin.get("/api/export").json()
        assert (
            again["tables"]["services"][0]["fluids"] == "5 qt"
            and again["receipt_files"] == backup["receipt_files"]
        )
        assert (
            next(
                row
                for row in again["tables"]["vehicle_specs"]
                if row["vehicle_id"] == vehicle["id"]
            )["tire_size"]
            == "225/45R17"
        )
        assert (
            next(
                v for v in admin.get("/api/vehicles").json() if v["id"] == vehicle["id"]
            )["tire_size"]
            == "225/45R17"
        )
        assert (
            next(
                row
                for row in again["tables"]["vehicle_specs"]
                if row["vehicle_id"] == vehicle["id"]
            )["wheel_size"]
            == "17 × 7.5 in"
        )
    with TestClient(main.app) as member:
        member.post(
            "/api/login", json={"username": "member", "password": "password-123"}
        )
        assert member.get("/api/export").status_code == 403
        assert member.post("/api/import", json=backup).status_code == 403


def test_v1_mods_endpoint_lists_and_creates(tmp_path):
    main.DB_PATH = tmp_path / "v1-mods.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        token = admin.post("/api/tokens", json={"name": "mods"}).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        created = admin.post(
            "/api/v1/vehicles/1/mods",
            headers=auth,
            json={
                "name": "Radio",
                "price": 229.96,
                "date": None,
                "gotchas": "Use harness",
            },
        )
        assert created.status_code == 201
        assert (
            created.json()["name"] == "Radio"
            and created.json()["date"] is None
            and created.json()["price"] == 229.96
        )
        rows = admin.get("/api/v1/vehicles/1/mods", headers=auth)
        assert rows.status_code == 200 and rows.json()[0]["gotchas"] == "Use harness"
        assert admin.get("/api/v1/vehicles/1/mods").status_code == 401
        assert (
            admin.post(
                "/api/v1/vehicles/999/mods", headers=auth, json={"name": "Nope"}
            ).status_code
            == 404
        )


def test_vehicle_specs_tab_api_and_token_write(tmp_path):
    main.DB_PATH = tmp_path / "full-specs.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        assert admin.get("/api/vehicles/1/specs").json()["engine"] == ""
        saved = admin.put(
            "/api/vehicles/1/specs",
            json={
                "engine": "2.0L inline-4",
                "transmission": "5-speed manual",
                "drivetrain": "RWD",
                "curb_weight": "2,116 lb",
                "displacement": "1.6L",
                "mpg_city": "22",
                "mpg_highway": "30",
                "oil_type": "10W-30",
                "oil_capacity": "3.4 qt",
                "battery_group": "51R",
                "spark_plugs": "NGK BKR6E-11",
                "wiper_sizes": "18 / 18",
                "coolant_type": "ethylene glycol",
                "brake_fluid": "DOT 3",
                "air_filter_part_number": "CA7598",
                "wheel_lug_torque": "80 lb-ft",
                "wheel_size": "15 × 6 in",
            },
        )
        assert saved.status_code == 200 and saved.json()["drivetrain"] == "RWD"
        token = admin.post("/api/tokens", json={"name": "specs"}).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        updated = admin.put(
            "/api/v1/vehicles/1/specs",
            headers=auth,
            json={**saved.json(), "horsepower": "116 hp"},
        )
        assert (
            updated.status_code == 200
            and updated.json()["horsepower"] == "116 hp"
            and updated.json()["battery_group"] == "51R"
            and updated.json()["wheel_lug_torque"] == "80 lb-ft"
            and updated.json()["wheel_size"] == "15 × 6 in"
        )
        assert (
            admin.get("/api/v1/vehicles/1/specs", headers=auth).json()["engine"]
            == "2.0L inline-4"
        )
        assert (
            admin.put(
                "/api/v1/vehicles/999/specs", headers=auth, json={"engine": "x"}
            ).status_code
            == 404
        )


def test_swagger_docs_csp_allows_required_assets(tmp_path):
    main.DB_PATH = tmp_path / "docs.db"
    main.init_db()
    with TestClient(main.app) as client:
        assert client.get("/api/docs").status_code == 401
        assert client.get("/api/openapi.json").status_code == 401
        client.post(
            "/api/setup", json={"username": "admin", "password": "password-123"}
        )
        docs = client.get("/api/docs")
        assert docs.status_code == 200 and "Swagger UI" in docs.text
        assert client.get("/api/openapi.json").json()["info"]["title"] == "Garage"
        csp = docs.headers["content-security-policy"]
        assert (
            "https://cdn.jsdelivr.net" in csp and "https://fastapi.tiangolo.com" in csp
        )
        assert "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in csp
        assert (
            "https://cdn.jsdelivr.net"
            not in client.get("/api/status").headers["content-security-policy"]
        )


def test_vehicle_owner_permissions(tmp_path):
    main.DB_PATH = tmp_path / "owner-perms.db"
    main.RECEIPTS_DIR = tmp_path / "receipts"
    main.RECEIPTS_DIR.mkdir(exist_ok=True)
    main._login_failures.clear()
    main._api_calls.clear()
    main._api_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        member = admin.post(
            "/api/users",
            json={
                "username": "member-test",
                "password": "password-456",
                "is_admin": False,
            },
        ).json()
        other = admin.post(
            "/api/users",
            json={
                "username": "other-test",
                "password": "password-789",
                "is_admin": False,
            },
        ).json()
        own = admin.post(
            "/api/vehicles",
            json={
                "name": "Member Car",
                "year": "2020",
                "mileage": 100,
                "owner_id": member["id"],
            },
        ).json()
        theirs = admin.post(
            "/api/vehicles",
            json={
                "name": "Other Car",
                "year": "2021",
                "mileage": 200,
                "owner_id": other["id"],
            },
        ).json()
        assert own["owner_id"] == member["id"] and theirs["owner_id"] == other["id"]
        c_service = admin.post(
            "/api/services",
            json={
                "vehicle_id": theirs["id"],
                "date": "2026-09-01",
                "mileage": 210,
                "type": "Oil change",
                "cost": 50,
            },
        ).json()
        c_fuel = admin.post(
            "/api/fuel",
            json={
                "vehicle_id": theirs["id"],
                "date": "2026-09-01",
                "odometer": 210,
                "gallons": 8,
                "cost": 30,
            },
        ).json()
        c_reminder = admin.post(
            "/api/reminders",
            json={
                "vehicle_id": theirs["id"],
                "name": "Oil",
                "miles_interval": 5000,
                "last_date": "2026-09-01",
                "last_mileage": 210,
            },
        ).json()
        c_note = admin.post(
            "/api/notes",
            json={"vehicle_id": theirs["id"], "date": "2026-09-01", "body": "theirs"},
        ).json()
        c_mod = admin.post(
            "/api/mods",
            json={"vehicle_id": theirs["id"], "name": "Roof rack", "price": 300},
        ).json()
        c_receipt = admin.post(
            "/api/receipts",
            data={"kind": "fuel", "entry_id": str(c_fuel["id"])},
            files={"file": ("r.png", PNG_BYTES, "image/png")},
        ).json()
    with TestClient(main.app) as member_client:
        member_client.post(
            "/api/login", json={"username": "member-test", "password": "password-456"}
        )
        vid = own["id"]
        # owner can edit their own vehicle and everything on it
        assert (
            member_client.put(
                f"/api/vehicles/{vid}",
                json={
                    "name": "Member Car",
                    "year": "2020",
                    "mileage": 150,
                    "icon": "🚗",
                },
            ).status_code
            == 200
        )
        assert (
            member_client.put(
                f"/api/vehicles/{vid}/specs", json={"engine": "2.5L inline-4"}
            ).status_code
            == 200
        )
        assert (
            member_client.post(
                f"/api/vehicles/{vid}/photo",
                files={"file": ("car.png", PNG_BYTES, "image/png")},
            ).status_code
            == 201
        )
        service = member_client.post(
            "/api/services",
            json={
                "vehicle_id": vid,
                "date": "2026-09-10",
                "mileage": 160,
                "type": "Tire rotation",
                "cost": 25,
            },
        ).json()
        assert (
            member_client.put(
                f"/api/services/{service['id']}",
                json={
                    "vehicle_id": vid,
                    "date": "2026-09-10",
                    "mileage": 160,
                    "type": "Tire rotation",
                    "cost": 20,
                },
            ).status_code
            == 200
        )
        receipt = member_client.post(
            "/api/receipts",
            data={"kind": "service", "entry_id": str(service["id"])},
            files={"file": ("r.png", PNG_BYTES, "image/png")},
        )
        assert receipt.status_code == 201
        assert (
            member_client.delete(f"/api/receipts/{receipt.json()['id']}").status_code
            == 200
        )
        assert member_client.delete(f"/api/services/{service['id']}").status_code == 200
        fuel = member_client.post(
            "/api/fuel",
            json={
                "vehicle_id": vid,
                "date": "2026-09-11",
                "odometer": 170,
                "gallons": 9,
                "cost": 32,
            },
        ).json()
        assert (
            member_client.put(
                f"/api/fuel/{fuel['id']}",
                json={
                    "vehicle_id": vid,
                    "date": "2026-09-11",
                    "odometer": 170,
                    "gallons": 9,
                    "cost": 33,
                },
            ).status_code
            == 200
        )
        assert member_client.delete(f"/api/fuel/{fuel['id']}").status_code == 200
        reminder = member_client.post(
            "/api/reminders",
            json={
                "vehicle_id": vid,
                "name": "Oil",
                "miles_interval": 5000,
                "last_date": "2026-09-01",
                "last_mileage": 100,
            },
        ).json()
        assert (
            member_client.put(
                f"/api/reminders/{reminder['id']}",
                json={
                    "vehicle_id": vid,
                    "name": "Oil",
                    "miles_interval": 6000,
                    "last_date": "2026-09-01",
                    "last_mileage": 100,
                },
            ).status_code
            == 200
        )
        assert (
            member_client.delete(f"/api/reminders/{reminder['id']}").status_code == 200
        )
        note = member_client.post(
            "/api/notes", json={"vehicle_id": vid, "date": "2026-09-12", "body": "mine"}
        ).json()
        assert (
            member_client.put(
                f"/api/notes/{note['id']}",
                json={"vehicle_id": vid, "date": "2026-09-12", "body": "mine updated"},
            ).status_code
            == 200
        )
        assert member_client.delete(f"/api/notes/{note['id']}").status_code == 200
        mod = member_client.post(
            "/api/mods", json={"vehicle_id": vid, "name": "Floor mats", "price": 80}
        ).json()
        assert (
            member_client.put(
                f"/api/mods/{mod['id']}",
                json={"vehicle_id": vid, "name": "Floor mats", "price": 75},
            ).status_code
            == 200
        )
        assert member_client.delete(f"/api/mods/{mod['id']}").status_code == 200
        tid = theirs["id"]
        # the other user's vehicle is invisible: reads 404 or filter out, writes 404
        assert member_client.get(f"/api/services?vehicle_id={tid}").json() == []
        assert member_client.get(f"/api/vehicles/{tid}/specs").status_code == 404
        assert member_client.get(f'/api/receipts/{c_receipt["id"]}').status_code == 404
        assert (
            member_client.put(
                f"/api/vehicles/{tid}",
                json={"name": "Hijacked", "year": "2021", "mileage": 200, "icon": "🚗"},
            ).status_code
            == 404
        )
        assert member_client.delete(f"/api/vehicles/{tid}").status_code == 404
        assert (
            member_client.put(
                f"/api/vehicles/{tid}/specs", json={"engine": "x"}
            ).status_code
            == 404
        )
        assert (
            member_client.post(
                f"/api/vehicles/{tid}/photo",
                files={"file": ("car.png", PNG_BYTES, "image/png")},
            ).status_code
            == 404
        )
        assert (
            member_client.post(
                "/api/services",
                json={
                    "vehicle_id": tid,
                    "date": "2026-09-10",
                    "mileage": 220,
                    "type": "Nope",
                },
            ).status_code
            == 404
        )
        assert (
            member_client.put(
                f"/api/services/{c_service['id']}",
                json={
                    "vehicle_id": tid,
                    "date": "2026-09-10",
                    "mileage": 220,
                    "type": "Nope",
                },
            ).status_code
            == 404
        )
        assert (
            member_client.delete(f"/api/services/{c_service['id']}").status_code == 404
        )
        assert (
            member_client.post(
                "/api/fuel",
                json={
                    "vehicle_id": tid,
                    "date": "2026-09-10",
                    "odometer": 220,
                    "gallons": 8,
                    "cost": 30,
                },
            ).status_code
            == 404
        )
        assert (
            member_client.put(
                f"/api/fuel/{c_fuel['id']}",
                json={
                    "vehicle_id": tid,
                    "date": "2026-09-10",
                    "odometer": 220,
                    "gallons": 8,
                    "cost": 30,
                },
            ).status_code
            == 404
        )
        assert member_client.delete(f"/api/fuel/{c_fuel['id']}").status_code == 404
        assert (
            member_client.post(
                "/api/reminders",
                json={
                    "vehicle_id": tid,
                    "name": "Nope",
                    "miles_interval": 5000,
                    "last_date": "2026-09-01",
                    "last_mileage": 210,
                },
            ).status_code
            == 404
        )
        assert (
            member_client.put(
                f"/api/reminders/{c_reminder['id']}",
                json={
                    "vehicle_id": tid,
                    "name": "Nope",
                    "miles_interval": 5000,
                    "last_date": "2026-09-01",
                    "last_mileage": 210,
                },
            ).status_code
            == 404
        )
        assert (
            member_client.delete(f"/api/reminders/{c_reminder['id']}").status_code
            == 404
        )
        assert (
            member_client.post(
                "/api/notes",
                json={"vehicle_id": tid, "date": "2026-09-10", "body": "nope"},
            ).status_code
            == 404
        )
        assert (
            member_client.put(
                f"/api/notes/{c_note['id']}",
                json={"vehicle_id": tid, "date": "2026-09-10", "body": "nope"},
            ).status_code
            == 404
        )
        assert member_client.delete(f"/api/notes/{c_note['id']}").status_code == 404
        assert (
            member_client.post(
                "/api/mods", json={"vehicle_id": tid, "name": "Nope", "price": 1}
            ).status_code
            == 404
        )
        assert (
            member_client.put(
                f"/api/mods/{c_mod['id']}",
                json={"vehicle_id": tid, "name": "Nope", "price": 1},
            ).status_code
            == 404
        )
        assert member_client.delete(f"/api/mods/{c_mod['id']}").status_code == 404
        assert (
            member_client.post(
                "/api/receipts",
                data={"kind": "fuel", "entry_id": str(c_fuel["id"])},
                files={"file": ("r.png", PNG_BYTES, "image/png")},
            ).status_code
            == 404
        )
        assert (
            member_client.delete(f"/api/receipts/{c_receipt['id']}").status_code == 404
        )
        # entries cannot be moved between vehicles across the ownership line, either way
        moved_out = member_client.post(
            "/api/services",
            json={
                "vehicle_id": vid,
                "date": "2026-09-13",
                "mileage": 180,
                "type": "Mine",
            },
        ).json()
        assert (
            member_client.put(
                f"/api/services/{moved_out['id']}",
                json={
                    "vehicle_id": tid,
                    "date": "2026-09-13",
                    "mileage": 180,
                    "type": "Mine",
                },
            ).status_code
            == 404
        )
        assert (
            member_client.put(
                f"/api/services/{c_service['id']}",
                json={
                    "vehicle_id": vid,
                    "date": "2026-09-01",
                    "mileage": 210,
                    "type": "Oil change",
                },
            ).status_code
            == 404
        )
        assert (
            member_client.delete(f"/api/services/{moved_out['id']}").status_code == 200
        )
    with TestClient(main.app) as admin_again:
        admin_again.post(
            "/api/login", json={"username": "admin-test", "password": "password-123"}
        )
        # admin keeps full access to another user's vehicle
        tid = theirs["id"]
        assert (
            admin_again.put(
                f"/api/vehicles/{tid}",
                json={
                    "name": "Other Car",
                    "year": "2021",
                    "mileage": 205,
                    "icon": "🚗",
                },
            ).status_code
            == 200
        )
        assert (
            admin_again.put(
                f"/api/vehicles/{tid}/specs", json={"engine": "3.6L V6"}
            ).status_code
            == 200
        )
        service = admin_again.post(
            "/api/services",
            json={
                "vehicle_id": tid,
                "date": "2026-09-14",
                "mileage": 230,
                "type": "Brake pads",
                "cost": 120,
            },
        ).json()
        assert (
            admin_again.put(
                f"/api/services/{c_service['id']}",
                json={
                    "vehicle_id": tid,
                    "date": "2026-09-01",
                    "mileage": 210,
                    "type": "Oil change",
                    "cost": 55,
                },
            ).status_code
            == 200
        )
        assert admin_again.delete(f"/api/services/{service['id']}").status_code == 200
        assert admin_again.delete(f"/api/vehicles/{tid}").status_code == 200


def test_v1_token_permissions_follow_token_owner(tmp_path):
    main.DB_PATH = tmp_path / "v1-owner-perms.db"
    main._login_failures.clear()
    main._api_calls.clear()
    main._api_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        other = admin.post(
            "/api/users",
            json={
                "username": "other-test",
                "password": "password-789",
                "is_admin": False,
            },
        ).json()
        second = admin.post(
            "/api/users",
            json={
                "username": "second-admin",
                "password": "password-000",
                "is_admin": True,
            },
        ).json()
        theirs = admin.post(
            "/api/vehicles",
            json={
                "name": "Other Car",
                "year": "2021",
                "mileage": 200,
                "owner_id": other["id"],
            },
        ).json()
    with TestClient(main.app) as second_client:
        second_client.post(
            "/api/login", json={"username": "second-admin", "password": "password-000"}
        )
        own = second_client.post(
            "/api/vehicles", json={"name": "Second Car", "year": "2022", "mileage": 50}
        ).json()
        token = second_client.post(
            "/api/tokens", json={"name": "second-script"}
        ).json()["token"]
    with TestClient(main.app) as admin:
        admin.post(
            "/api/login", json={"username": "admin-test", "password": "password-123"}
        )
        assert (
            admin.put(
                f"/api/users/{second['id']}", json={"is_admin": False}
            ).status_code
            == 200
        )
    auth = {"Authorization": f"Bearer {token}"}
    with TestClient(main.app) as client:
        # the demoted token owner can still write to their own vehicle
        assert (
            client.post(
                f"/api/v1/vehicles/{own['id']}/services",
                headers=auth,
                json={"date": "2026-09-15", "mileage": 60, "type": "Wash"},
            ).status_code
            == 201
        )
        assert (
            client.put(
                f"/api/v1/vehicles/{own['id']}/mileage",
                headers=auth,
                json={"mileage": 65},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/v1/vehicles/{own['id']}/notes",
                headers=auth,
                json={"date": "2026-09-15", "body": "mine"},
            ).status_code
            == 201
        )
        assert (
            client.post(
                f"/api/v1/vehicles/{own['id']}/mods",
                headers=auth,
                json={"name": "Mats", "price": 50},
            ).status_code
            == 201
        )
        assert (
            client.put(
                f"/api/v1/vehicles/{own['id']}/specs",
                headers=auth,
                json={"engine": "1.5L"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/v1/vehicles/{own['id']}/fuel",
                headers=auth,
                data={
                    "date": "2026-09-15",
                    "odometer": "65",
                    "gallons": "5",
                    "cost": "20",
                },
            ).status_code
            == 201
        )
        # and another user's vehicle is invisible to the token
        tid = theirs["id"]
        assert (
            client.post(
                f"/api/v1/vehicles/{tid}/services",
                headers=auth,
                json={"date": "2026-09-15", "mileage": 210, "type": "Nope"},
            ).status_code
            == 404
        )
        assert (
            client.put(
                f"/api/v1/vehicles/{tid}/mileage", headers=auth, json={"mileage": 999}
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/v1/vehicles/{tid}/notes",
                headers=auth,
                json={"date": "2026-09-15", "body": "nope"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/v1/vehicles/{tid}/mods", headers=auth, json={"name": "Nope"}
            ).status_code
            == 404
        )
        assert (
            client.put(
                f"/api/v1/vehicles/{tid}/specs", headers=auth, json={"engine": "x"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/v1/vehicles/{tid}/fuel",
                headers=auth,
                data={
                    "date": "2026-09-15",
                    "odometer": "210",
                    "gallons": "5",
                    "cost": "20",
                },
            ).status_code
            == 404
        )
        # reads on it are 404 too, and the vehicle list is scoped to the token owner's own vehicles
        assert (
            client.get(f"/api/v1/vehicles/{tid}/services", headers=auth).status_code
            == 404
        )
        assert (
            client.get(f"/api/v1/vehicles/{tid}/specs", headers=auth).status_code == 404
        )
        assert [
            v["name"] for v in client.get("/api/v1/vehicles", headers=auth).json()
        ] == ["Second Car"]


def test_per_user_garage_visibility(tmp_path):
    main.DB_PATH = tmp_path / "per-user.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        member = admin.post(
            "/api/users", json={"username": "member-test", "password": "password-456"}
        ).json()
        other = admin.post(
            "/api/users", json={"username": "other-test", "password": "password-789"}
        ).json()
        mine = admin.post(
            "/api/vehicles",
            json={
                "name": "Mine",
                "year": "2020",
                "mileage": 1,
                "owner_id": member["id"],
            },
        ).json()
        mine_private = admin.post(
            "/api/vehicles",
            json={
                "name": "Mine private",
                "year": "2021",
                "mileage": 2,
                "owner_id": member["id"],
                "private": True,
            },
        ).json()
        theirs = admin.post(
            "/api/vehicles",
            json={
                "name": "Theirs shared",
                "year": "2022",
                "mileage": 3,
                "owner_id": other["id"],
            },
        ).json()
        theirs_private = admin.post(
            "/api/vehicles",
            json={
                "name": "Theirs private",
                "year": "2023",
                "mileage": 4,
                "owner_id": other["id"],
                "private": True,
            },
        ).json()
        admins = admin.post(
            "/api/vehicles", json={"name": "Admin shared", "year": "2024", "mileage": 5}
        ).json()
        for vid in (mine["id"], theirs["id"], admins["id"]):
            admin.post(
                "/api/services",
                json={
                    "vehicle_id": vid,
                    "date": "2026-09-01",
                    "mileage": 10,
                    "type": "Check",
                },
            )
            admin.post(
                "/api/fuel",
                json={
                    "vehicle_id": vid,
                    "date": "2026-09-01",
                    "odometer": 10,
                    "gallons": 5,
                    "cost": 20,
                },
            )
            admin.post(
                "/api/reminders",
                json={
                    "vehicle_id": vid,
                    "name": "Oil",
                    "miles_interval": 5000,
                    "last_date": "2026-09-01",
                    "last_mileage": 10,
                },
            )
            admin.post(
                "/api/notes",
                json={"vehicle_id": vid, "date": "2026-09-01", "body": "note"},
            )
            admin.post("/api/mods", json={"vehicle_id": vid, "name": "Mod", "price": 1})
    with TestClient(main.app) as member_client:
        member_client.post(
            "/api/login", json={"username": "member-test", "password": "password-456"}
        )
        # a non-admin sees only vehicles they own, everywhere
        assert {v["name"] for v in member_client.get("/api/vehicles").json()} == {
            "Mine",
            "Mine private",
        }
        for path in (
            "/api/services",
            "/api/fuel",
            "/api/reminders",
            "/api/notes",
            "/api/mods",
        ):
            rows = member_client.get(path).json()
            assert {r["vehicle_id"] for r in rows} == {mine["id"]}, path
        # other users' vehicles do not exist for a member: reads and writes 404
        tid = theirs["id"]
        assert member_client.get(f"/api/vehicles/{tid}/specs").status_code == 404
        assert (
            member_client.post(
                "/api/services",
                json={
                    "vehicle_id": tid,
                    "date": "2026-09-02",
                    "mileage": 11,
                    "type": "Nope",
                },
            ).status_code
            == 404
        )
        assert (
            member_client.put(
                f"/api/vehicles/{tid}",
                json={"name": "Nope", "year": "2022", "mileage": 3, "icon": "🚗"},
            ).status_code
            == 404
        )
    with TestClient(main.app) as admin_again:
        admin_again.post(
            "/api/login", json={"username": "admin-test", "password": "password-123"}
        )
        # admins still see every shared vehicle plus their own private ones
        names = {v["name"] for v in admin_again.get("/api/vehicles").json()}
        assert {"Ford Mustang", "Mine", "Theirs shared", "Admin shared"} <= names
        # private vehicles of other users stay hidden from admins until show-all
        assert "Mine private" not in names
        assert "Theirs private" not in names
        admin_again.put("/api/me/vehicle-view", json={"show_all": True})
        names = {v["name"] for v in admin_again.get("/api/vehicles").json()}
        assert "Mine private" in names and "Theirs private" in names


def test_tire_size_and_fuel_type_are_spec_fields_that_drive_header_chips(tmp_path):
    main.DB_PATH = tmp_path / "tire-spec.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        blank = admin.get("/api/vehicles/1/specs").json()
        assert blank["tire_size"] == "" and blank["fuel_type"] == ""
        saved = admin.put(
            "/api/vehicles/1/specs",
            json={
                "wheel_size": "18 × 7.5 in",
                "tire_size": " 225/75R16 ",
                "wheel_lug_torque": "150 lb-ft",
                "fuel_type": "Regular gasoline",
                "oil_type": "5W-30",
                "oil_capacity": "6 qt",
            },
        ).json()
        assert (
            saved["tire_size"] == "225/75R16"
            and saved["wheel_size"] == "18 × 7.5 in"
            and saved["fuel_type"] == "Regular gasoline"
        )
        vehicle = admin.get("/api/vehicles").json()[0]
        assert (vehicle["tire_size"], vehicle["fuel_type"], vehicle["oil_spec"]) == (
            "225/75R16",
            "Regular gasoline",
            "5W-30, 6 qt",
        )
        # an older client that replaces specs without the new keys must not wipe them
        kept = admin.put(
            "/api/vehicles/1/specs",
            json={
                "wheel_size": "18 × 8 in",
                "oil_type": "5W-30",
                "oil_capacity": "6 qt",
            },
        ).json()
        assert (
            kept["tire_size"] == "225/75R16"
            and kept["fuel_type"] == "Regular gasoline"
            and kept["wheel_size"] == "18 × 8 in"
        )
        # an explicit empty string clears
        assert (
            admin.put("/api/vehicles/1/specs", json={**kept, "tire_size": ""}).json()[
                "tire_size"
            ]
            == ""
        )
        assert admin.get("/api/vehicles").json()[0]["tire_size"] == ""
        assert (
            admin.put("/api/vehicles/1/specs", json={"tire_size": "x" * 81}).status_code
            == 422
        )
        # token API reads and writes the field too
        token = admin.post("/api/tokens", json={"name": "tires"}).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        assert (
            admin.put(
                "/api/v1/vehicles/1/specs",
                headers=auth,
                json={**kept, "tire_size": "275/65R18"},
            ).json()["tire_size"]
            == "275/65R18"
        )
        assert (
            admin.get("/api/v1/vehicles/1/specs", headers=auth).json()["tire_size"]
            == "275/65R18"
        )
        assert (
            admin.get("/api/v1/vehicles", headers=auth).json()[0]["tire_size"]
            == "275/65R18"
        )
        # legacy vehicle-level writes land in specs; omitting them leaves specs untouched
        vehicle = admin.get("/api/vehicles").json()[0]
        body = {k: vehicle[k] for k in ("name", "year", "mileage", "icon", "private")}
        after = admin.put("/api/vehicles/1", json=body).json()
        assert after["tire_size"] == "275/65R18" and after["oil_spec"] == "5W-30, 6 qt"
        after = admin.put(
            "/api/vehicles/1",
            json={
                **body,
                "tire_size": "195/65 R15",
                "oil_spec": "0W-20, 4.4 qt",
                "fuel_type": "Premium gasoline",
            },
        ).json()
        assert (after["tire_size"], after["oil_spec"], after["fuel_type"]) == (
            "195/65 R15",
            "0W-20, 4.4 qt",
            "Premium gasoline",
        )
        specs = admin.get("/api/vehicles/1/specs").json()
        assert (specs["tire_size"], specs["oil_type"], specs["oil_capacity"]) == (
            "195/65 R15",
            "0W-20",
            "4.4 qt",
        )
        created = admin.post(
            "/api/vehicles", json={"name": "Truck", "tire_size": "245/75R16"}
        ).json()
        assert (
            created["tire_size"] == "245/75R16"
            and admin.get(f"/api/vehicles/{created['id']}/specs").json()["tire_size"]
            == "245/75R16"
        )
        with main.db() as c:
            assert (
                c.execute(
                    "SELECT count(*) FROM vehicles WHERE tire_size<>'' OR fuel_type<>'' OR oil_spec<>''"
                ).fetchone()[0]
                == 0
            )


def test_split_oil_spec_uses_existing_text_only():
    assert main.split_oil_spec("0W-20, 4.0 qt") == ("0W-20", "4.0 qt")
    assert main.split_oil_spec("5W-30, 6 L") == ("5W-30", "6 L")
    assert main.split_oil_spec("5W-30") == ("5W-30", "")
    assert main.split_oil_spec("Full synthetic, see manual") == (
        "Full synthetic, see manual",
        "",
    )
    assert (
        main.join_oil_spec("0W-20", "") == "0W-20" and main.join_oil_spec("", "") == ""
    )


def _legacy_header(vehicle_id, fuel, tire, oil):
    with main.db() as c:
        c.execute(
            "UPDATE vehicles SET fuel_type=?,tire_size=?,oil_spec=? WHERE id=?",
            (fuel, tire, oil, vehicle_id),
        )


def test_startup_migrates_vehicle_level_header_values_into_specs(tmp_path):
    main.DB_PATH = tmp_path / "migrate.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        truck = admin.post("/api/vehicles", json={"name": "Pickup"}).json()["id"]
        roadster = admin.post("/api/vehicles", json={"name": "Roadster"}).json()["id"]
        clash = admin.post("/api/vehicles", json={"name": "Clash"}).json()["id"]
        admin.put(
            f"/api/vehicles/{roadster}/specs",
            json={"wheel_size": "17 × 7 in", "oil_capacity": "4.5 qt"},
        )
        admin.put(
            f"/api/vehicles/{clash}/specs",
            json={"tire_size": "225/40R18", "oil_type": "5W-30"},
        )
    # simulate a database written by the previous release: values only at vehicle level
    _legacy_header(truck, "Regular gasoline", "225/75R16", "5W-30, 6 qt")
    _legacy_header(roadster, "Premium gasoline", "195/65 R15", "0W-20")
    _legacy_header(clash, "", "225/45R17", "0W-20, 4 qt")
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/login", json={"username": "admin", "password": "password-123"})
        vehicles = {v["id"]: v for v in admin.get("/api/vehicles").json()}
        assert (
            vehicles[truck]["tire_size"],
            vehicles[truck]["fuel_type"],
            vehicles[truck]["oil_spec"],
        ) == ("225/75R16", "Regular gasoline", "5W-30, 6 qt")
        assert (
            vehicles[roadster]["tire_size"],
            vehicles[roadster]["fuel_type"],
            vehicles[roadster]["oil_spec"],
        ) == ("195/65 R15", "Premium gasoline", "0W-20, 4.5 qt")
        truck_specs = admin.get(f"/api/vehicles/{truck}/specs").json()
        assert (
            truck_specs["tire_size"],
            truck_specs["oil_type"],
            truck_specs["oil_capacity"],
        ) == ("225/75R16", "5W-30", "6 qt")
        roadster_specs = admin.get(f"/api/vehicles/{roadster}/specs").json()
        assert (
            roadster_specs["wheel_size"] == "17 × 7 in"
            and roadster_specs["tire_size"] == "195/65 R15"
        )
        # a different value already in specs wins, and the old header value is kept as a note
        assert (
            vehicles[clash]["tire_size"] == "225/40R18"
            and vehicles[clash]["oil_spec"] == "5W-30, 4 qt"
        )
        notes = [n for n in admin.get("/api/notes").json() if n["vehicle_id"] == clash]
        assert (
            len(notes) == 1
            and "Tires: 225/45R17" in notes[0]["body"]
            and "Oil: 0W-20, 4 qt" in notes[0]["body"]
        )
        # clearing a value after migration is not undone by the next restart
        admin.put(f"/api/vehicles/{truck}/specs", json={**truck_specs, "tire_size": ""})
    main.init_db()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/login", json={"username": "admin", "password": "password-123"})
        assert admin.get(f"/api/vehicles/{truck}/specs").json()["tire_size"] == ""
        assert (
            len([n for n in admin.get("/api/notes").json() if n["vehicle_id"] == clash])
            == 1
        )
    with main.db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM vehicles WHERE tire_size<>'' OR fuel_type<>'' OR oil_spec<>''"
            ).fetchone()[0]
            == 0
        )


def test_importing_a_pre_migration_backup_moves_header_values_into_specs(tmp_path):
    main.DB_PATH = tmp_path / "old-backup.db"
    main.RECEIPTS_DIR = tmp_path / "receipts-old"
    main.RECEIPTS_DIR.mkdir()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        vid = admin.post("/api/vehicles", json={"name": "Old Backup Car"}).json()["id"]
        backup = admin.get("/api/export").json()
        row = next(r for r in backup["tables"]["vehicles"] if r["id"] == vid)
        row.update(
            fuel_type="Premium gasoline",
            tire_size="195/65 R15",
            oil_spec="0W-20, 4.4 qt",
        )
        backup["tables"]["vehicle_specs"] = [
            {k: v for k, v in r.items() if k not in ("tire_size", "fuel_type")}
            for r in backup["tables"]["vehicle_specs"]
        ]
        assert admin.post("/api/import", json=backup).status_code == 200
        admin.post("/api/login", json={"username": "admin", "password": "password-123"})
        v = next(v for v in admin.get("/api/vehicles").json() if v["id"] == vid)
        assert (v["tire_size"], v["fuel_type"], v["oil_spec"]) == (
            "195/65 R15",
            "Premium gasoline",
            "0W-20, 4.4 qt",
        )
        assert (
            admin.get(f"/api/vehicles/{vid}/specs").json()["tire_size"] == "195/65 R15"
        )


def test_import_rejects_receipt_paths_outside_receipts_folder(tmp_path):
    main.DB_PATH = tmp_path / "import-paths.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        backup = admin.get("/api/export").json()
        vid = backup["tables"]["vehicles"][0]["id"]
        for bad in ("/etc/passwd", "../garage.db", "..", "a/b.png"):
            evil = json.loads(json.dumps(backup))
            evil["tables"]["receipts"] = [
                {
                    "id": 99,
                    "kind": "vehicle",
                    "entry_id": vid,
                    "stored_name": bad,
                    "orig_name": "x",
                    "mime": "image/png",
                    "size": 1,
                    "uploaded_by": 1,
                    "created_at": "x",
                }
            ]
            assert admin.post("/api/import", json=evil).status_code == 400
            assert admin.get("/api/receipts/99").status_code == 404
        evil = json.loads(json.dumps(backup))
        evil["receipt_files"] = {"..": "AA=="}
        assert admin.post("/api/import", json=evil).status_code == 400


def test_import_restores_with_tokens_and_vehicle_photo(tmp_path):
    main.DB_PATH = tmp_path / "import-fk.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        vid = admin.get("/api/vehicles").json()[0]["id"]
        assert (
            admin.post(
                f"/api/vehicles/{vid}/photo",
                files={"file": ("car.png", PNG_BYTES, "image/png")},
            ).status_code
            == 201
        )
        token = admin.post("/api/tokens", json={"name": "phone"}).json()["token"]
        backup = admin.get("/api/export").json()
        assert backup["tables"]["api_tokens"]
        restored = admin.post("/api/import", json=backup)
        assert restored.status_code == 200, restored.text
        admin.post("/api/login", json={"username": "admin", "password": "password-123"})
        vehicle = admin.get("/api/vehicles").json()[0]
        assert (
            vehicle["photo_url"] and admin.get(vehicle["photo_url"]).status_code == 200
        )
        assert (
            admin.get(
                "/api/v1/vehicles", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 200
        )
        legacy = json.loads(json.dumps(backup))
        del legacy["tables"]["api_tokens"]
        assert admin.post("/api/import", json=legacy).status_code == 200


def test_malformed_dates_are_rejected_and_never_break_lists(tmp_path):
    main.DB_PATH = tmp_path / "dates.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        vid = admin.get("/api/vehicles").json()[0]["id"]
        bad = "<script>"
        assert (
            admin.post(
                "/api/fuel",
                json={"vehicle_id": vid, "date": bad, "odometer": 1, "gallons": 1},
            ).status_code
            == 422
        )
        assert (
            admin.post(
                "/api/notes", json={"vehicle_id": vid, "date": bad, "body": "x"}
            ).status_code
            == 422
        )
        assert (
            admin.post(
                "/api/reminders",
                json={
                    "vehicle_id": vid,
                    "name": "r",
                    "months_interval": 6,
                    "last_date": bad,
                },
            ).status_code
            == 422
        )
        assert (
            admin.post(
                "/api/services",
                json={"vehicle_id": vid, "type": "oil", "date": "2026-02-30"},
            ).status_code
            == 422
        )
        assert (
            admin.post(
                "/api/services", json={"vehicle_id": vid, "type": "oil", "date": ""}
            ).status_code
            == 200
        )
        token = admin.post("/api/tokens", json={"name": "t"}).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        assert (
            admin.post(
                f"/api/v1/vehicles/{vid}/fuel",
                headers=auth,
                data={"date": "yesterday", "odometer": "5", "gallons": "2"},
            ).status_code
            == 422
        )
        # Rows written by older versions with bad dates must not take the app down.
        with main.db() as c:
            stamp = main.now_iso()
            c.execute(
                "INSERT INTO fuel_entries(vehicle_id,fill_date,odometer,gallons,cost,logged_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (vid, bad, 10, 1, 0, 1, stamp, stamp),
            )
            c.execute(
                "INSERT INTO reminders(vehicle_id,name,months_interval,last_date,due_date,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (vid, "old", 6, bad, "nope", stamp, stamp),
            )
        assert admin.get("/api/vehicles").status_code == 200
        assert admin.get("/api/v1/vehicles", headers=auth).status_code == 200
        assert (
            admin.get(f"/api/v1/vehicles/{vid}/maintenance", headers=auth).status_code
            == 200
        )
        with main.db() as c:
            main.due_maintenance_items(c)


def test_video_links_must_be_http(tmp_path):
    main.DB_PATH = tmp_path / "urls.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        vid = admin.get("/api/vehicles").json()[0]["id"]
        for route, body in (
            ("/api/services", {"vehicle_id": vid, "type": "oil"}),
            ("/api/mods", {"vehicle_id": vid, "name": "intake"}),
        ):
            assert (
                admin.post(
                    route, json={**body, "youtube_url": "javascript:alert(1)"}
                ).status_code
                == 422
            )
            assert admin.post(
                route, json={**body, "youtube_url": "https://example.com/v"}
            ).status_code in (200, 201)


def test_password_change_ends_other_sessions_and_revoked_tokens_fail(tmp_path):
    main.DB_PATH = tmp_path / "pw.db"
    main.init_db()
    with TestClient(main.app) as admin, TestClient(main.app) as member:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        uid = admin.post(
            "/api/users", json={"username": "member", "password": "password-123"}
        ).json()["id"]
        member.post(
            "/api/login", json={"username": "member", "password": "password-123"}
        )
        assert member.get("/api/me").status_code == 200
        admin.put(f"/api/users/{uid}", json={"password": "new-password-1"})
        assert member.get("/api/me").status_code == 401
        token = admin.post("/api/tokens", json={"name": "t"}).json()["token"]
        with main.db() as c:
            c.execute("UPDATE api_tokens SET revoked=1")
        assert (
            admin.get(
                "/api/v1/vehicles", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 401
        )


def test_uploads_over_limit_are_rejected(tmp_path):
    main.DB_PATH = tmp_path / "upload.db"
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post("/api/setup", json={"username": "admin", "password": "password-123"})
        vid = admin.get("/api/vehicles").json()[0]["id"]
        big = b"0" * (main.RECEIPT_MAX_BYTES + 1)
        r = admin.post(
            f"/api/vehicles/{vid}/photo", files={"file": ("big.png", big, "image/png")}
        )
        assert r.status_code == 400 and "10 MB" in r.json()["detail"]
