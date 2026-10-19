import os

os.environ["GARAGE_DATA_DIR"] = "/tmp/garage-pytest-data"
os.environ["GARAGE_NOTIFY_WORKER"] = "false"
from fastapi.testclient import TestClient
from app import main

PNG_BYTES = __import__("base64").b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _setup(tmp_path):
    main.DB_PATH = tmp_path / "receipt-token.db"
    main.RECEIPTS_DIR = tmp_path / "receipts"
    main.RECEIPTS_DIR.mkdir(exist_ok=True)
    main._login_failures.clear()
    main._api_calls.clear()
    main._api_failures.clear()
    main.init_db()


def _upload(client, headers=None, kind="service", entry_id="1"):
    return client.post(
        "/api/receipts",
        data={"kind": kind, "entry_id": entry_id},
        files={"file": ("receipt.png", PNG_BYTES, "image/png")},
        headers=headers or {},
    )


def test_receipt_upload_accepts_api_token(tmp_path):
    _setup(tmp_path)
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        token = admin.post("/api/tokens", json={"name": "script"}).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        service = admin.post(
            "/api/v1/vehicles/1/services",
            json={"date": "2026-09-20", "mileage": 100, "type": "Oil", "cost": 5},
            headers=auth,
        )
        assert service.status_code == 201
    # A fresh client with no session cookie: only the token authenticates.
    with TestClient(main.app) as script:
        assert _upload(script).status_code == 401
        bad = {"Authorization": "Bearer gar_wrong"}
        assert _upload(script, bad).status_code == 401
        done = _upload(script, auth)
        assert done.status_code == 201
        assert done.json()["uploaded_by"] == "admin-test"
        assert _upload(script, auth, kind="service", entry_id="99").status_code == 404
        # The token still cannot reach settings.
        assert script.get("/api/settings", headers=auth).status_code == 401


def test_receipt_token_follows_owner_and_archive_rules(tmp_path):
    _setup(tmp_path)
    with TestClient(main.app) as admin:
        admin.post(
            "/api/setup", json={"username": "admin-test", "password": "password-123"}
        )
        admin_token = admin.post("/api/tokens", json={"name": "a"}).json()["token"]
        admin_auth = {"Authorization": f"Bearer {admin_token}"}
        admin.post(
            "/api/v1/vehicles/1/services",
            json={"date": "2026-09-20", "mileage": 100, "type": "Oil", "cost": 5},
            headers=admin_auth,
        )
        admin.post(
            "/api/users",
            json={"username": "member-test", "password": "password-456", "is_admin": False},
        )
    with TestClient(main.app) as member:
        member.post(
            "/api/login", json={"username": "member-test", "password": "password-456"}
        )
        member_token = member.post("/api/tokens", json={"name": "m"}).json()["token"]
        member_auth = {"Authorization": f"Bearer {member_token}"}
    with TestClient(main.app) as script:
        # Member token cannot touch the admin-owned car's entry.
        assert _upload(script, member_auth).status_code == 404
    with TestClient(main.app) as admin:
        admin.post(
            "/api/login", json={"username": "admin-test", "password": "password-123"}
        )
        assert admin.post("/api/vehicles/1/archive").status_code == 200
    with TestClient(main.app) as script:
        assert _upload(script, admin_auth).status_code == 409
