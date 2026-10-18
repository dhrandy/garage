import os

os.environ["GARAGE_DATA_DIR"] = "/tmp/garage-pytest-data"
os.environ["GARAGE_NOTIFY_WORKER"] = "false"
from fastapi.testclient import TestClient
from app import main


def _fresh_client(tmp_path, name="archive"):
    main.DB_PATH = tmp_path / f"{name}.db"
    main.init_db()
    client = TestClient(main.app)
    assert (
        client.post(
            "/api/setup",
            json={"username": "admin-test", "password": "password-123"},
        ).status_code
        == 200
    )
    return client


def test_archive_hides_from_due_and_blocks_writes(tmp_path):
    with _fresh_client(tmp_path) as c:
        # Seed a due reminder and a fill on the demo Mustang.
        assert (
            c.post(
                "/api/reminders",
                json={
                    "vehicle_id": 1,
                    "name": "Inspection",
                    "miles_interval": None,
                    "months_interval": None,
                    "due_date": "2020-01-01",
                    "repeats_yearly": False,
                    "last_date": "2019-01-01",
                    "last_mileage": 0,
                },
            ).status_code
            == 200
        )
        with main.db() as conn:
            assert len(main.due_maintenance_items(conn)) == 1

        # Archive it.
        r = c.post("/api/vehicles/1/archive")
        assert r.status_code == 200
        assert r.json()["archived"] is True
        assert c.get("/api/vehicles").json()[0]["archived"] is True

        # Reminders on archived cars stop counting as due.
        with main.db() as conn:
            assert main.due_maintenance_items(conn) == []

        # Entries stay put, readable.
        assert c.get("/api/reminders").json() != []

        # Writes are blocked with 409 until unarchived.
        assert (
            c.post(
                "/api/fuel",
                json={
                    "vehicle_id": 1,
                    "date": "2026-10-18",
                    "odometer": 100,
                    "gallons": 10,
                    "cost": 35,
                },
            ).status_code
            == 409
        )
        assert (
            c.post(
                "/api/services",
                json={
                    "vehicle_id": 1,
                    "date": "2026-10-18",
                    "mileage": 100,
                    "type": "Oil change",
                    "cost": 50,
                    "provider": "DIY",
                    "notes": "",
                },
            ).status_code
            == 409
        )
        assert (
            c.put(
                "/api/vehicles/1",
                json={"name": "Mustang", "year": "1969", "mileage": 5, "icon": "🚗"},
            ).status_code
            == 409
        )
        assert (
            c.put("/api/vehicles/1/specs", json={"engine": "V8"}).status_code == 409
        )

        # Delete still works on an archived car.
        # Unarchive first so the delete check below uses a live vehicle.
        r = c.post("/api/vehicles/1/unarchive")
        assert r.status_code == 200
        assert r.json()["archived"] is False
        assert (
            c.post(
                "/api/fuel",
                json={
                    "vehicle_id": 1,
                    "date": "2026-10-18",
                    "odometer": 100,
                    "gallons": 10,
                    "cost": 35,
                },
            ).status_code
            == 200
        )
        with main.db() as conn:
            assert len(main.due_maintenance_items(conn)) == 1


def test_archive_permissions_and_delete(tmp_path):
    with _fresh_client(tmp_path, "archive-perms") as c:
        assert (
            c.post(
                "/api/users",
                json={
                    "username": "member-test",
                    "password": "password-456",
                    "is_admin": False,
                },
            ).status_code
            == 200
        )
        # A member cannot archive another user's car (it is invisible: 404).
        member = TestClient(main.app)
        assert (
            member.post(
                "/api/login",
                json={"username": "member-test", "password": "password-456"},
            ).status_code
            == 200
        )
        assert member.post("/api/vehicles/1/archive").status_code == 404

        # Admin archives, member still cannot unarchive.
        assert c.post("/api/vehicles/1/archive").status_code == 200
        assert member.post("/api/vehicles/1/unarchive").status_code == 404

        # Deleting an archived vehicle works for the owner/admin.
        assert c.delete("/api/vehicles/1").status_code == 200
        assert c.get("/api/vehicles").json() == []


def test_archive_flag_survives_export_import_and_v1_writes_blocked(tmp_path):
    with _fresh_client(tmp_path, "archive-export") as c:
        assert c.post("/api/vehicles/1/archive").status_code == 200
        backup = c.get("/api/export").json()
        assert backup["tables"]["vehicles"][0]["archived"] == 1

        # Token (API) writes are blocked too.
        token = c.post("/api/tokens", json={"name": "script"}).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        assert (
            c.put(
                "/api/v1/vehicles/1/mileage",
                json={"mileage": 500},
                headers=auth,
            ).status_code
            == 409
        )
        assert (
            c.post(
                "/api/v1/vehicles/1/fuel",
                data={
                    "date": "2026-10-18",
                    "odometer": "500",
                    "gallons": "10",
                    "cost": "35",
                },
                headers=auth,
            ).status_code
            == 409
        )

        # Restore into a fresh database keeps the flag.
        main.DB_PATH = tmp_path / "archive-restore.db"
        main.init_db()
        with TestClient(main.app) as c2:
            c2.post(
                "/api/setup",
                json={"username": "admin-test", "password": "password-123"},
            )
            assert c2.post("/api/import", json=backup).status_code == 200
            # Import wipes sessions, so sign back in before reading.
            c2.post(
                "/api/login",
                json={"username": "admin-test", "password": "password-123"},
            )
            assert c2.get("/api/vehicles").json()[0]["archived"] is True


def test_hide_archived_setting_round_trip(tmp_path):
    with _fresh_client(tmp_path, "archive-setting") as c:
        assert c.get("/api/settings").json()["hide_archived"] is False
        r = c.put("/api/settings", json={"hide_archived": True})
        assert r.status_code == 200
        assert r.json()["hide_archived"] is True
