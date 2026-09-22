from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.request
from contextlib import contextmanager
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("GARAGE_DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "garage.db"
RECEIPTS_DIR = DATA_DIR / "receipts"
RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
RECEIPT_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "image/heic": ".heic"}
RECEIPT_MAX_BYTES = 10 * 1024 * 1024
COOKIE = "garage_session"
SESSION_DAYS = 30
PBKDF2_ITERATIONS = 260_000
LOGIN_LIMIT = 5
LOGIN_WINDOW_SECONDS = 15 * 60
API_LIMIT = 100
API_WINDOW_SECONDS = 60
API_FAIL_LIMIT = 5
API_FAIL_WINDOW_SECONDS = 15 * 60
_login_failures: dict[str, deque[float]] = defaultdict(deque)
_login_lock = threading.Lock()
_api_calls: dict[str, deque[float]] = defaultdict(deque)
_api_failures: dict[str, deque[float]] = defaultdict(deque)
_api_lock = threading.Lock()

app = FastAPI(title="Garage", version="2.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json")

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; object-src 'none'; img-src 'self' data:; "
        "script-src 'self'; style-src 'self' 'unsafe-inline'"
    )
    return response

def client_ip(request: Request) -> str:
    # Uvicorn replaces request.client only when the connecting proxy is trusted.
    return request.client.host if request.client else "unknown"

def login_retry_after(ip: str) -> int:
    now = time.monotonic()
    with _login_lock:
        failures = _login_failures[ip]
        while failures and now - failures[0] >= LOGIN_WINDOW_SECONDS:
            failures.popleft()
        return max(0, int(LOGIN_WINDOW_SECONDS - (now - failures[0])) + 1) if len(failures) >= LOGIN_LIMIT else 0

def record_login_failure(ip: str) -> None:
    with _login_lock:
        _login_failures[ip].append(time.monotonic())

def clear_login_failures(ip: str) -> None:
    with _login_lock:
        _login_failures.pop(ip, None)

def _window_allows(hits: deque[float], limit: int, window: int, count: bool) -> int:
    now = time.monotonic()
    while hits and now - hits[0] >= window:
        hits.popleft()
    if len(hits) >= limit:
        return max(1, int(window - (now - hits[0])) + 1)
    if count:
        hits.append(now)
    return 0

def api_call_allowed(token_hash: str) -> int:
    with _api_lock:
        return _window_allows(_api_calls[token_hash], API_LIMIT, API_WINDOW_SECONDS, True)

def api_failure_retry_after(ip: str) -> int:
    with _api_lock:
        return _window_allows(_api_failures[ip], API_FAIL_LIMIT, API_FAIL_WINDOW_SECONDS, False)

def record_api_failure(ip: str) -> None:
    with _api_lock:
        _api_failures[ip].append(time.monotonic())

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def init_db():
    fresh_install = not DB_PATH.exists()
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL UNIQUE COLLATE NOCASE,
          password_hash TEXT NOT NULL,
          salt TEXT NOT NULL,
          is_admin INTEGER NOT NULL DEFAULT 0,
          active INTEGER NOT NULL DEFAULT 1,
          show_all_vehicles INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS vehicles (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          year TEXT NOT NULL DEFAULT '',
          mileage INTEGER NOT NULL DEFAULT 0 CHECK(mileage >= 0),
          icon TEXT NOT NULL DEFAULT '🚗',
          added_by INTEGER REFERENCES users(id),
          owner_id INTEGER REFERENCES users(id),
          private INTEGER NOT NULL DEFAULT 0,
          fuel_type TEXT NOT NULL DEFAULT '',
          tire_size TEXT NOT NULL DEFAULT '',
          oil_spec TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS services (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
          service_date TEXT NOT NULL,
          mileage INTEGER NOT NULL DEFAULT 0 CHECK(mileage >= 0),
          service_type TEXT NOT NULL,
          cost REAL NOT NULL DEFAULT 0 CHECK(cost >= 0),
          provider TEXT NOT NULL DEFAULT '',
          notes TEXT NOT NULL DEFAULT '',
          torque_specs TEXT NOT NULL DEFAULT '',
          fluids TEXT NOT NULL DEFAULT '',
          gotchas TEXT NOT NULL DEFAULT '',
          youtube_url TEXT NOT NULL DEFAULT '',
          logged_by INTEGER NOT NULL REFERENCES users(id),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fuel_entries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
          fill_date TEXT NOT NULL,
          odometer INTEGER NOT NULL DEFAULT 0 CHECK(odometer >= 0),
          gallons REAL NOT NULL CHECK(gallons > 0),
          cost REAL NOT NULL DEFAULT 0 CHECK(cost >= 0),
          logged_by INTEGER NOT NULL REFERENCES users(id),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
          note_date TEXT NOT NULL,
          body TEXT NOT NULL,
          logged_by INTEGER NOT NULL REFERENCES users(id),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS modifications (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          mod_date TEXT,
          price REAL NOT NULL DEFAULT 0 CHECK(price >= 0),
          logged_by INTEGER REFERENCES users(id),
          torque_specs TEXT NOT NULL DEFAULT '',
          fluids TEXT NOT NULL DEFAULT '',
          gotchas TEXT NOT NULL DEFAULT '',
          youtube_url TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_tokens (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,
          prefix TEXT NOT NULL,
          created_by INTEGER REFERENCES users(id),
          created_at TEXT NOT NULL,
          last_used_at TEXT,
          revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS receipts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL CHECK(kind IN ('service','fuel','vehicle')),
          entry_id INTEGER NOT NULL,
          stored_name TEXT NOT NULL,
          orig_name TEXT NOT NULL DEFAULT '',
          mime TEXT NOT NULL DEFAULT 'image/jpeg',
          size INTEGER NOT NULL DEFAULT 0,
          uploaded_by INTEGER REFERENCES users(id),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mileage_updates (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
          mileage INTEGER NOT NULL CHECK(mileage >= 0),
          recorded_by INTEGER REFERENCES users(id),
          recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reminders (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          miles_interval INTEGER,
          months_interval INTEGER,
          last_date TEXT NOT NULL,
          last_mileage INTEGER NOT NULL DEFAULT 0,
          due_date TEXT,
          repeats_yearly INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """)
        user_columns = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "show_all_vehicles" not in user_columns:
            c.execute("ALTER TABLE users ADD COLUMN show_all_vehicles INTEGER NOT NULL DEFAULT 0")
        vehicle_columns = {r["name"] for r in c.execute("PRAGMA table_info(vehicles)")}
        if "owner_id" not in vehicle_columns:
            c.execute("ALTER TABLE vehicles ADD COLUMN owner_id INTEGER REFERENCES users(id)")
        if "private" not in vehicle_columns:
            c.execute("ALTER TABLE vehicles ADD COLUMN private INTEGER NOT NULL DEFAULT 0")
        for col in ("fuel_type","tire_size","oil_spec"):
            if col not in vehicle_columns: c.execute(f"ALTER TABLE vehicles ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        mod_columns={r["name"] for r in c.execute("PRAGMA table_info(modifications)")}
        for col in ("torque_specs","fluids","gotchas","youtube_url"):
            if col not in mod_columns: c.execute(f"ALTER TABLE modifications ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        admin = c.execute("SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone()
        if admin:
            c.execute("UPDATE vehicles SET owner_id=? WHERE owner_id IS NULL", (admin["id"],))
        reminder_columns={r["name"] for r in c.execute("PRAGMA table_info(reminders)")}
        if "due_date" not in reminder_columns: c.execute("ALTER TABLE reminders ADD COLUMN due_date TEXT")
        if "repeats_yearly" not in reminder_columns: c.execute("ALTER TABLE reminders ADD COLUMN repeats_yearly INTEGER NOT NULL DEFAULT 0")
        fuel_columns={r["name"] for r in c.execute("PRAGMA table_info(fuel_entries)")}
        if "octane" not in fuel_columns: c.execute("ALTER TABLE fuel_entries ADD COLUMN octane TEXT NOT NULL DEFAULT ''")
        if "reminder_id" not in {r["name"] for r in c.execute("PRAGMA table_info(services)")}:
            c.execute("ALTER TABLE services ADD COLUMN reminder_id INTEGER REFERENCES reminders(id)")
        service_columns={r["name"] for r in c.execute("PRAGMA table_info(services)")}
        for col in ("torque_specs", "fluids", "gotchas", "youtube_url"):
            if col not in service_columns: c.execute(f"ALTER TABLE services ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        if "photo_receipt_id" not in {r["name"] for r in c.execute("PRAGMA table_info(vehicles)")}:
            c.execute("ALTER TABLE vehicles ADD COLUMN photo_receipt_id INTEGER REFERENCES receipts(id)")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", ("garage_name", "Your Garage"))
        for key in SETTINGS_KEYS[1:]:
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, "0"))
        if fresh_install:
            stamp = now_iso()
            c.execute(
                "INSERT INTO vehicles(name,year,mileage,icon,added_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                ("Ford Mustang", "1969", 0, "🚗", None, stamp, stamp),
            )

@app.on_event("startup")
def startup():
    init_db()
    if os.getenv("GARAGE_NOTIFY_WORKER", "true").lower() == "true":
        threading.Thread(target=notification_worker, daemon=True).start()

def clean_username(value: str) -> str:
    value = value.strip()
    if len(value) < 3 or len(value) > 40 or not all(ch.isalnum() or ch in "._-" for ch in value):
        raise HTTPException(400, "Username must be 3-40 characters using letters, numbers, dots, dashes, or underscores")
    return value

def password_record(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return digest.hex(), salt.hex()

def verify_password(password: str, expected: str, salt: str) -> bool:
    digest, _ = password_record(password, salt)
    return hmac.compare_digest(digest, expected)

def public_user(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"]), "active": bool(row["active"]), "show_all_vehicles": bool(row["show_all_vehicles"])}

def current_user(request: Request, admin: bool = False) -> sqlite3.Row:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Not signed in")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db() as c:
        row = c.execute("""SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
          WHERE s.token_hash=? AND s.expires_at>? AND u.active=1""", (token_hash, now_iso())).fetchone()
    if not row:
        raise HTTPException(401, "Session expired")
    if admin and not row["is_admin"]:
        raise HTTPException(403, "Administrator access required")
    return row

def set_session(response: Response, user_id: int):
    raw = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    with db() as c:
        c.execute("DELETE FROM sessions WHERE expires_at<=?", (now_iso(),))
        c.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                  (hashlib.sha256(raw.encode()).hexdigest(), user_id, expires.isoformat(), now_iso()))
    response.set_cookie(COOKIE, raw, max_age=SESSION_DAYS*86400, httponly=True, samesite="strict",
                        secure=os.getenv("GARAGE_COOKIE_SECURE", "false").lower() == "true", path="/")

def vehicle_accessible(row: sqlite3.Row, user: sqlite3.Row) -> bool:
    return bool(not row["private"] or row["owner_id"] == user["id"] or (user["is_admin"] and user["show_all_vehicles"]))

def get_visible_vehicle(c: sqlite3.Connection, vehicle_id: int, user: sqlite3.Row) -> sqlite3.Row:
    row = c.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
    if not row or not vehicle_accessible(row, user):
        raise HTTPException(404, "Vehicle not found")
    return row

def visible_vehicle_ids(c: sqlite3.Connection, user: sqlite3.Row) -> set[int]:
    return {r["id"] for r in c.execute("SELECT * FROM vehicles") if vehicle_accessible(r, user)}

def token_user(c: sqlite3.Connection, token: sqlite3.Row) -> sqlite3.Row:
    row = c.execute("SELECT * FROM users WHERE id=? AND active=1", (token["created_by"],)).fetchone()
    if not row:
        raise HTTPException(403, "Token owner is inactive")
    return row

def display_user(c: sqlite3.Connection, user_id: int | None) -> str:
    if user_id is None:
        return ""
    row = c.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    return row[0] if row else "Former user"

class Credentials(BaseModel):
    username: str
    password: str

class VehicleIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    year: str = Field(default="", max_length=4)
    mileage: int = Field(default=0, ge=0)
    icon: str = Field(default="🚗", max_length=8)
    owner_id: int | None = None
    private: bool = False
    fuel_type: str = Field(default="", max_length=80)
    tire_size: str = Field(default="", max_length=80)
    oil_spec: str = Field(default="", max_length=120)

class ServiceIn(BaseModel):
    vehicle_id: int
    date: str
    mileage: int = Field(default=0, ge=0)
    type: str = Field(min_length=1, max_length=100)
    cost: float = Field(default=0, ge=0)
    provider: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=1000)
    torque_specs: str = Field(default="", max_length=500)
    fluids: str = Field(default="", max_length=500)
    gotchas: str = Field(default="", max_length=1000)
    youtube_url: str = Field(default="", max_length=500)
    reminder_id: int | None = None

class FuelIn(BaseModel):
    vehicle_id: int
    date: str
    odometer: int = Field(ge=0)
    gallons: float = Field(gt=0)
    cost: float = Field(default=0, ge=0)
    octane: str = Field(default="", max_length=20)

class ReminderIn(BaseModel):
    vehicle_id: int
    name: str = Field(min_length=1, max_length=100)
    miles_interval: int | None = Field(default=None, ge=1)
    months_interval: int | None = Field(default=None, ge=1)
    last_date: str
    last_mileage: int = Field(default=0, ge=0)
    due_date: str | None = None
    repeats_yearly: bool = False

SETTINGS_KEYS = ("garage_name", "hide_service_log", "hide_maintenance", "hide_costs", "hide_fuel", "hide_notes", "use_vehicle_photos", "use_kilometers")

def read_settings(c) -> dict[str, Any]:
    data = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")}
    out: dict[str, Any] = {"garage_name": data.get("garage_name", "Your Garage")}
    for key in SETTINGS_KEYS[1:]:
        out[key] = data.get(key, "0") == "1"
    return out

class SettingsIn(BaseModel):
    garage_name: str | None = Field(default=None, max_length=80)
    hide_service_log: bool | None = None
    hide_maintenance: bool | None = None
    hide_costs: bool | None = None
    hide_fuel: bool | None = None
    hide_notes: bool | None = None
    use_vehicle_photos: bool | None = None
    use_kilometers: bool | None = None

class UserCreate(BaseModel):
    username: str
    password: str
    is_admin: bool = False

class UserUpdate(BaseModel):
    username: str | None = None
    password: str | None = None
    is_admin: bool | None = None
    active: bool | None = None

@app.get("/api/status")
def status():
    with db() as c:
        setup_required = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    return {"setup_required": setup_required}

@app.post("/api/setup")
def setup(body: Credentials, response: Response):
    username = clean_username(body.username)
    pw, salt = password_record(body.password)
    with db() as c:
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            raise HTTPException(409, "Setup is already complete")
        cur = c.execute("INSERT INTO users(username,password_hash,salt,is_admin,active,created_at) VALUES(?,?,?,1,1,?)",
                        (username,pw,salt,now_iso()))
        uid = cur.lastrowid
        c.execute("UPDATE vehicles SET owner_id=? WHERE owner_id IS NULL",(uid,))
    set_session(response, uid)
    return {"ok": True}

@app.post("/api/login")
def login(body: Credentials, request: Request, response: Response):
    ip = client_ip(request)
    retry_after = login_retry_after(ip)
    if retry_after:
        raise HTTPException(429, "Too many login attempts. Try again later.", headers={"Retry-After": str(retry_after)})
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (body.username.strip(),)).fetchone()
    if not row or not row["active"] or not verify_password(body.password, row["password_hash"], row["salt"]):
        record_login_failure(ip)
        raise HTTPException(401, "Invalid username or password")
    clear_login_failures(ip)
    set_session(response, row["id"])
    return {"user": public_user(row)}

@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        with db() as c: c.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}

@app.get("/api/me")
def me(request: Request):
    return public_user(current_user(request))

class VehicleViewIn(BaseModel):
    show_all: bool

@app.put("/api/me/vehicle-view")
def set_vehicle_view(body: VehicleViewIn, request: Request):
    user=current_user(request, True)
    with db() as c:
        c.execute("UPDATE users SET show_all_vehicles=? WHERE id=?",(int(body.show_all),user["id"]))
        return public_user(c.execute("SELECT * FROM users WHERE id=?",(user["id"],)).fetchone())

def mileage_estimate(c, vehicle_id: int, today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(timezone.utc).date()
    points = [(r["service_date"], r["mileage"]) for r in c.execute("SELECT service_date,mileage FROM services WHERE vehicle_id=? AND mileage>0", (vehicle_id,))]
    points += [(r["fill_date"], r["odometer"]) for r in c.execute("SELECT fill_date,odometer FROM fuel_entries WHERE vehicle_id=? AND odometer>0", (vehicle_id,))]
    points = sorted(set(points))
    if not points:
        return {"est_mileage": None, "miles_per_day": None}
    first_date, first_odo = points[0]
    last_date, last_odo = points[-1]
    rate = None
    days = (datetime.strptime(last_date, "%Y-%m-%d").date() - datetime.strptime(first_date, "%Y-%m-%d").date()).days
    if len(points) >= 2 and days >= 7 and last_odo > first_odo:
        rate = (last_odo - first_odo) / days
    est = last_odo
    if rate:
        days_since = (today - datetime.strptime(last_date, "%Y-%m-%d").date()).days
        est = last_odo + rate * max(0, days_since)
    return {"est_mileage": round(est), "miles_per_day": round(rate, 1) if rate is not None else None}

def effective_mileage(c, vehicle_row) -> int:
    est = mileage_estimate(c, vehicle_row["id"])["est_mileage"]
    return max(vehicle_row["mileage"], est or 0)

def vehicle_dict(c, row):
    est = mileage_estimate(c, row["id"])
    return {"id":row["id"],"name":row["name"],"year":row["year"],"mileage":row["mileage"],"icon":row["icon"],
            "est_mileage":est["est_mileage"],"miles_per_day":est["miles_per_day"],"photo_receipt_id":row["photo_receipt_id"],
            "photo_url":f"/api/receipts/{row['photo_receipt_id']}" if row["photo_receipt_id"] else None,
            "added_by":display_user(c,row["added_by"]) or "System", "owner_id":row["owner_id"], "owner":display_user(c,row["owner_id"]) or "System", "private":bool(row["private"]), "fuel_type":row["fuel_type"], "tire_size":row["tire_size"], "oil_spec":row["oil_spec"],
            "mileage_updated_by": (lambda r: display_user(c, r["recorded_by"]) if r else "")(c.execute("SELECT recorded_by FROM mileage_updates WHERE vehicle_id=? ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()),
            "created_at":row["created_at"],"updated_at":row["updated_at"]}

@app.get("/api/vehicles")
def list_vehicles(request: Request):
    user=current_user(request)
    with db() as c: return [vehicle_dict(c,r) for r in c.execute("SELECT * FROM vehicles ORDER BY id") if vehicle_accessible(r,user)]

@app.post("/api/vehicles")
def add_vehicle(body: VehicleIn, request: Request):
    user=current_user(request); stamp=now_iso()
    with db() as c:
        owner_id=body.owner_id if user["is_admin"] and body.owner_id else user["id"]
        if not c.execute("SELECT 1 FROM users WHERE id=? AND active=1",(owner_id,)).fetchone(): raise HTTPException(400,"Owner not found")
        cur=c.execute("INSERT INTO vehicles(name,year,mileage,icon,added_by,owner_id,private,fuel_type,tire_size,oil_spec,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (body.name.strip(),body.year.strip(),body.mileage,body.icon,user["id"],owner_id,int(body.private),body.fuel_type.strip(),body.tire_size.strip(),body.oil_spec.strip(),stamp,stamp))
        row=c.execute("SELECT * FROM vehicles WHERE id=?",(cur.lastrowid,)).fetchone(); return vehicle_dict(c,row)

@app.put("/api/vehicles/{item_id}")
def update_vehicle(item_id:int, body:VehicleIn, request:Request):
    user=current_user(request); stamp=now_iso()
    with db() as c:
        old=get_visible_vehicle(c,item_id,user)
        if old["owner_id"] != user["id"] and not user["is_admin"]: raise HTTPException(403,"Only the owner or an administrator can edit this vehicle")
        owner_id=body.owner_id if user["is_admin"] and body.owner_id else old["owner_id"] or user["id"]
        cur=c.execute("UPDATE vehicles SET name=?,year=?,mileage=?,icon=?,owner_id=?,private=?,fuel_type=?,tire_size=?,oil_spec=?,updated_at=? WHERE id=?",
                      (body.name.strip(),body.year.strip(),body.mileage,body.icon,owner_id,int(body.private),body.fuel_type.strip(),body.tire_size.strip(),body.oil_spec.strip(),stamp,item_id))
        if body.mileage != old["mileage"]:
            c.execute("INSERT INTO mileage_updates(vehicle_id,mileage,recorded_by,recorded_at) VALUES(?,?,?,?)",
                      (item_id,body.mileage,user["id"],stamp))
        if not cur.rowcount: raise HTTPException(404,"Vehicle not found")
        return vehicle_dict(c,c.execute("SELECT * FROM vehicles WHERE id=?",(item_id,)).fetchone())

@app.post("/api/vehicles/{item_id}/photo", status_code=201)
async def upload_vehicle_photo(item_id: int, request: Request, file: UploadFile = File(...)):
    user = current_user(request)
    mime = (file.content_type or "").lower()
    if mime not in RECEIPT_TYPES:
        raise HTTPException(400, "Vehicle photos must be JPEG, PNG, WebP, GIF, or HEIC images")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > RECEIPT_MAX_BYTES:
        raise HTTPException(400, "Vehicle photos are limited to 10 MB")
    with db() as c:
        v = get_visible_vehicle(c, item_id, user)
        if v["photo_receipt_id"]:
            old_photo = c.execute("SELECT * FROM receipts WHERE id=?", (v["photo_receipt_id"],)).fetchone()
            if old_photo:
                (RECEIPTS_DIR / old_photo["stored_name"]).unlink(missing_ok=True)
                c.execute("UPDATE vehicles SET photo_receipt_id=NULL WHERE id=?", (item_id,))
                c.execute("DELETE FROM receipts WHERE id=?", (old_photo["id"],))
        stored = f"{secrets.token_hex(16)}{RECEIPT_TYPES[mime]}"
        (RECEIPTS_DIR / stored).write_bytes(data)
        cur = c.execute("INSERT INTO receipts(kind,entry_id,stored_name,orig_name,mime,size,uploaded_by,created_at) VALUES('vehicle',?,?,?,?,?,?,?)",
                        (item_id, stored, (file.filename or "")[:120], mime, len(data), user["id"], now_iso()))
        photo_id = cur.lastrowid
        c.execute("UPDATE vehicles SET photo_receipt_id=?,updated_at=? WHERE id=?", (photo_id, now_iso(), item_id))
        return receipt_dict(c, c.execute("SELECT * FROM receipts WHERE id=?", (photo_id,)).fetchone())

@app.delete("/api/vehicles/{item_id}")
def delete_vehicle(item_id:int, request:Request):
    user=current_user(request)
    with db() as c:
        v=get_visible_vehicle(c,item_id,user)
        if v["owner_id"] != user["id"] and not user["is_admin"]: raise HTTPException(403,"Only the owner or an administrator can delete this vehicle")
        service_ids=[r["id"] for r in c.execute("SELECT id FROM services WHERE vehicle_id=?",(item_id,))]
        fuel_ids=[r["id"] for r in c.execute("SELECT id FROM fuel_entries WHERE vehicle_id=?",(item_id,))]
        for sid in service_ids: delete_receipt_files(c, "service", sid)
        for fid in fuel_ids: delete_receipt_files(c, "fuel", fid)
        delete_receipt_files(c, "vehicle", item_id)
        if not c.execute("DELETE FROM vehicles WHERE id=?",(item_id,)).rowcount: raise HTTPException(404,"Vehicle not found")
    return {"ok":True}

def update_vehicle_mileage(c, vehicle_id: int, mileage: int, user_id: int | None, stamp: str) -> None:
    row = c.execute("SELECT mileage FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
    if row and mileage > row["mileage"]:
        c.execute("UPDATE vehicles SET mileage=?,updated_at=? WHERE id=?", (mileage, stamp, vehicle_id))
        c.execute("INSERT INTO mileage_updates(vehicle_id,mileage,recorded_by,recorded_at) VALUES(?,?,?,?)",
                  (vehicle_id, mileage, user_id, stamp))

def apply_reminder_reset(c, vehicle_id: int, reminder_id: int | None, service_date: str, mileage: int) -> None:
    if reminder_id is None:
        return
    cur = c.execute("UPDATE reminders SET last_date=?,last_mileage=?,updated_at=? WHERE id=? AND vehicle_id=?",
                    (service_date, mileage, now_iso(), reminder_id, vehicle_id))
    if not cur.rowcount:
        raise HTTPException(400, "Maintenance item not found for this vehicle")

def service_dict(c,row):
    return {"id":row["id"],"vehicle_id":row["vehicle_id"],"date":row["service_date"],"mileage":row["mileage"],
            "type":row["service_type"],"cost":row["cost"],"provider":row["provider"],"notes":row["notes"],
            "torque_specs":row["torque_specs"],"fluids":row["fluids"],"gotchas":row["gotchas"],"youtube_url":row["youtube_url"],
            "logged_by":display_user(c,row["logged_by"]),"logged_by_id":row["logged_by"],"reminder_id":row["reminder_id"],"created_at":row["created_at"],"updated_at":row["updated_at"]}

@app.get("/api/services")
def list_services(request:Request, vehicle_id:int|None=None):
    user=current_user(request)
    with db() as c:
        visible=visible_vehicle_ids(c,user)
        rows=c.execute("SELECT * FROM services WHERE (? IS NULL OR vehicle_id=?) ORDER BY service_date DESC,id DESC",(vehicle_id,vehicle_id))
        return [service_dict(c,r) for r in rows if r["vehicle_id"] in visible]

@app.post("/api/services")
def add_service(body:ServiceIn, request:Request):
    user=current_user(request); stamp=now_iso()
    with db() as c:
        get_visible_vehicle(c,body.vehicle_id,user)
        if not c.execute("SELECT 1 FROM vehicles WHERE id=?",(body.vehicle_id,)).fetchone(): raise HTTPException(404,"Vehicle not found")
        apply_reminder_reset(c, body.vehicle_id, body.reminder_id, body.date, body.mileage)
        cur=c.execute("""INSERT INTO services(vehicle_id,service_date,mileage,service_type,cost,provider,notes,torque_specs,fluids,gotchas,youtube_url,logged_by,reminder_id,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(body.vehicle_id,body.date,body.mileage,body.type.strip(),body.cost,body.provider.strip(),body.notes.strip(),body.torque_specs.strip(),body.fluids.strip(),body.gotchas.strip(),body.youtube_url.strip(),user["id"],body.reminder_id,stamp,stamp))
        update_vehicle_mileage(c, body.vehicle_id, body.mileage, user["id"], stamp)
        return service_dict(c,c.execute("SELECT * FROM services WHERE id=?",(cur.lastrowid,)).fetchone())

@app.put("/api/services/{item_id}")
def update_service(item_id:int,body:ServiceIn,request:Request):
    user=current_user(request)
    with db() as c:
        get_visible_vehicle(c,body.vehicle_id,user)
        apply_reminder_reset(c, body.vehicle_id, body.reminder_id, body.date, body.mileage)
        cur=c.execute("""UPDATE services SET vehicle_id=?,service_date=?,mileage=?,service_type=?,cost=?,provider=?,notes=?,torque_specs=?,fluids=?,gotchas=?,youtube_url=?,reminder_id=?,updated_at=? WHERE id=?""",
          (body.vehicle_id,body.date,body.mileage,body.type.strip(),body.cost,body.provider.strip(),body.notes.strip(),body.torque_specs.strip(),body.fluids.strip(),body.gotchas.strip(),body.youtube_url.strip(),body.reminder_id,now_iso(),item_id))
        if not cur.rowcount: raise HTTPException(404,"Service not found")
        return service_dict(c,c.execute("SELECT * FROM services WHERE id=?",(item_id,)).fetchone())

@app.delete("/api/services/{item_id}")
def delete_service(item_id:int,request:Request):
    user=current_user(request)
    with db() as c:
        entry=c.execute("SELECT vehicle_id FROM services WHERE id=?",(item_id,)).fetchone()
        if not entry: raise HTTPException(404,"Entry not found")
        get_visible_vehicle(c,entry["vehicle_id"],user)
        if not c.execute("DELETE FROM services WHERE id=?",(item_id,)).rowcount: raise HTTPException(404,"Service not found")
        delete_receipt_files(c, "service", item_id)
    return {"ok":True}

def fuel_rows(c, vehicle_id: int | None = None) -> list[dict[str, Any]]:
    rows = c.execute("SELECT * FROM fuel_entries WHERE (? IS NULL OR vehicle_id=?) ORDER BY fill_date,id", (vehicle_id, vehicle_id)).fetchall()
    out: list[dict[str, Any]] = []
    prev: sqlite3.Row | None = None
    for r in rows:
        mpg = None
        if prev is not None and r["odometer"] > prev["odometer"]:
            mpg = (r["odometer"] - prev["odometer"]) / r["gallons"]
        out.append({"id": r["id"], "vehicle_id": r["vehicle_id"], "date": r["fill_date"], "odometer": r["odometer"],
                    "gallons": r["gallons"], "cost": r["cost"], "octane":r["octane"], "mpg": mpg,
                    "logged_by": display_user(c, r["logged_by"]), "created_at": r["created_at"], "updated_at": r["updated_at"]})
        prev = r
    out.reverse()
    return out

def one_fuel(c, item_id: int) -> dict[str, Any]:
    row = c.execute("SELECT * FROM fuel_entries WHERE id=?", (item_id,)).fetchone()
    return next(r for r in fuel_rows(c, row["vehicle_id"]) if r["id"] == item_id)

@app.get("/api/fuel")
def list_fuel(request: Request, vehicle_id: int | None = None):
    user=current_user(request)
    with db() as c:
        visible=visible_vehicle_ids(c,user)
        return [r for r in fuel_rows(c, vehicle_id) if r["vehicle_id"] in visible]

@app.post("/api/fuel")
def add_fuel(body: FuelIn, request: Request):
    user = current_user(request); stamp = now_iso()
    with db() as c:
        get_visible_vehicle(c,body.vehicle_id,user)
        if not c.execute("SELECT 1 FROM vehicles WHERE id=?", (body.vehicle_id,)).fetchone():
            raise HTTPException(404, "Vehicle not found")
        cur = c.execute("INSERT INTO fuel_entries(vehicle_id,fill_date,odometer,gallons,cost,octane,logged_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (body.vehicle_id, body.date, body.odometer, body.gallons, body.cost, body.octane.strip(), user["id"], stamp, stamp))
        update_vehicle_mileage(c, body.vehicle_id, body.odometer, user["id"], stamp)
        return one_fuel(c, cur.lastrowid)

@app.put("/api/fuel/{item_id}")
def update_fuel(item_id: int, body: FuelIn, request: Request):
    user=current_user(request)
    with db() as c:
        get_visible_vehicle(c,body.vehicle_id,user)
        cur = c.execute("UPDATE fuel_entries SET vehicle_id=?,fill_date=?,odometer=?,gallons=?,cost=?,octane=?,updated_at=? WHERE id=?",
                        (body.vehicle_id, body.date, body.odometer, body.gallons, body.cost, body.octane.strip(), now_iso(), item_id))
        if not cur.rowcount:
            raise HTTPException(404, "Fill-up not found")
        return one_fuel(c, item_id)

@app.delete("/api/fuel/{item_id}")
def delete_fuel(item_id: int, request: Request):
    user=current_user(request)
    with db() as c:
        entry=c.execute("SELECT vehicle_id FROM fuel_entries WHERE id=?",(item_id,)).fetchone()
        if not entry: raise HTTPException(404,"Entry not found")
        get_visible_vehicle(c,entry["vehicle_id"],user)
        if not c.execute("DELETE FROM fuel_entries WHERE id=?", (item_id,)).rowcount:
            raise HTTPException(404, "Fill-up not found")
        delete_receipt_files(c, "fuel", item_id)
    return {"ok": True}

def receipt_vehicle_id(c: sqlite3.Connection, row: sqlite3.Row) -> int | None:
    if row["kind"] == "vehicle":
        return row["entry_id"]
    table = "services" if row["kind"] == "service" else "fuel_entries" if row["kind"] == "fuel" else None
    if not table:
        return None
    entry = c.execute(f"SELECT vehicle_id FROM {table} WHERE id=?", (row["entry_id"],)).fetchone()
    return entry["vehicle_id"] if entry else None

def require_receipt_access(c: sqlite3.Connection, row: sqlite3.Row, user: sqlite3.Row) -> None:
    vehicle_id = receipt_vehicle_id(c, row)
    if vehicle_id is None:
        raise HTTPException(404, "Receipt not found")
    get_visible_vehicle(c, vehicle_id, user)

def receipt_dict(c, row) -> dict[str, Any]:
    return {"id": row["id"], "kind": row["kind"], "entry_id": row["entry_id"], "orig_name": row["orig_name"],
            "mime": row["mime"], "size": row["size"], "uploaded_by": display_user(c, row["uploaded_by"]),
            "created_at": row["created_at"], "url": f"/api/receipts/{row['id']}"}

def delete_receipt_files(c, kind: str, entry_id: int) -> None:
    for r in c.execute("SELECT stored_name FROM receipts WHERE kind=? AND entry_id=?", (kind, entry_id)):
        (RECEIPTS_DIR / r["stored_name"]).unlink(missing_ok=True)
    c.execute("DELETE FROM receipts WHERE kind=? AND entry_id=?", (kind, entry_id))

@app.get("/api/receipts")
def list_receipts(request: Request, kind: str | None = None, entry_id: int | None = None):
    user = current_user(request)
    with db() as c:
        rows = c.execute("SELECT * FROM receipts WHERE (? IS NULL OR kind=?) AND (? IS NULL OR entry_id=?) ORDER BY id", (kind, kind, entry_id, entry_id))
        return [receipt_dict(c, r) for r in rows if (vehicle_id := receipt_vehicle_id(c, r)) is not None and vehicle_accessible(get_vehicle_or_404(c, vehicle_id), user)]

@app.post("/api/receipts", status_code=201)
async def upload_receipt(request: Request, kind: str = Form(...), entry_id: int = Form(...), file: UploadFile = File(...)):
    user = current_user(request)
    if kind not in ("service", "fuel"):
        raise HTTPException(400, "Receipts attach to service or fuel entries")
    mime = (file.content_type or "").lower()
    if mime not in RECEIPT_TYPES:
        raise HTTPException(400, "Receipt photos must be JPEG, PNG, WebP, GIF, or HEIC images")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > RECEIPT_MAX_BYTES:
        raise HTTPException(400, "Receipt photos are limited to 10 MB")
    with db() as c:
        table = "services" if kind == "service" else "fuel_entries"
        entry = c.execute(f"SELECT vehicle_id FROM {table} WHERE id=?", (entry_id,)).fetchone()
        if not entry:
            raise HTTPException(404, "Entry not found")
        get_visible_vehicle(c, entry["vehicle_id"], user)
        stored = f"{secrets.token_hex(16)}{RECEIPT_TYPES[mime]}"
        (RECEIPTS_DIR / stored).write_bytes(data)
        cur = c.execute("INSERT INTO receipts(kind,entry_id,stored_name,orig_name,mime,size,uploaded_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (kind, entry_id, stored, (file.filename or "")[:120], mime, len(data), user["id"], now_iso()))
        return receipt_dict(c, c.execute("SELECT * FROM receipts WHERE id=?", (cur.lastrowid,)).fetchone())

@app.get("/api/receipts/{receipt_id}")
def get_receipt(receipt_id: int, request: Request):
    user = current_user(request)
    with db() as c:
        row = c.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row:
            require_receipt_access(c, row, user)
    if not row:
        raise HTTPException(404, "Receipt not found")
    path = RECEIPTS_DIR / row["stored_name"]
    if not path.exists():
        raise HTTPException(404, "Receipt file is missing")
    return FileResponse(path, media_type=row["mime"])

@app.delete("/api/receipts/{receipt_id}")
def delete_receipt(receipt_id: int, request: Request):
    user = current_user(request)
    with db() as c:
        row = c.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Receipt not found")
        require_receipt_access(c, row, user)
        if row["kind"] == "vehicle":
            c.execute("UPDATE vehicles SET photo_receipt_id=NULL WHERE photo_receipt_id=?", (receipt_id,))
        (RECEIPTS_DIR / row["stored_name"]).unlink(missing_ok=True)
        c.execute("DELETE FROM receipts WHERE id=?", (receipt_id,))
    return {"ok": True}

def get_vehicle_or_404(c, vehicle_id: int) -> sqlite3.Row:
    row = c.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Vehicle not found")
    return row

def reminder_status(mileage: int, r, today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(timezone.utc).date()
    progress: list[float] = []
    labels: list[str] = []
    if r["miles_interval"]:
        due = r["last_mileage"] + r["miles_interval"]
        progress.append((mileage - r["last_mileage"]) / r["miles_interval"])
        labels.append(f"{max(0, due - mileage):,} mi remaining")
    if r["months_interval"]:
        last = datetime.strptime(r["last_date"], "%Y-%m-%d").date()
        month_index = last.month - 1 + r["months_interval"]
        year = last.year + month_index // 12
        month = month_index % 12 + 1
        days_in_month = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
        due_date = date(year, month, min(last.day, days_in_month))
        total = (due_date - last).days
        progress.append((today - last).days / total if total > 0 else 1)
        days_left = (due_date - today).days
        labels.append(f"{days_left} days remaining" if days_left >= 0 else f"{-days_left} days late")
    if r["due_date"]:
        due_date=datetime.strptime(r["due_date"], "%Y-%m-%d").date()
        if r["repeats_yearly"]:
            max_day=[31,29 if today.year%4==0 and (today.year%100!=0 or today.year%400==0) else 28,31,30,31,30,31,31,30,31,30,31][due_date.month-1]
            due_date=date(today.year,due_date.month,min(due_date.day,max_day))
            if due_date < today: due_date=due_date.replace(year=today.year+1)
        days_left=(due_date-today).days
        progress.append(1 if days_left < 0 else .8 if days_left <= 30 else 0)
        labels.append(f"due {due_date.strftime('%b %-d, %Y')}" if days_left >= 0 else f"{-days_left} days late")
    p = max(progress) if progress else 0
    state = "overdue" if p >= 1 else "soon" if p >= 0.8 else "ok"
    return {"state": state, "progress": min(1, p), "label": " · ".join(labels) or "No schedule"}

def reminder_dict(row):
    return {"id":row["id"],"vehicle_id":row["vehicle_id"],"name":row["name"],"miles_interval":row["miles_interval"],
            "months_interval":row["months_interval"],"last_date":row["last_date"],"last_mileage":row["last_mileage"],"due_date":row["due_date"],"repeats_yearly":bool(row["repeats_yearly"])}

@app.get("/api/reminders")
def list_reminders(request:Request,vehicle_id:int|None=None):
    user=current_user(request)
    with db() as c:
        visible=visible_vehicle_ids(c,user)
        rows=c.execute("SELECT * FROM reminders WHERE (? IS NULL OR vehicle_id=?) ORDER BY id",(vehicle_id,vehicle_id))
        return [reminder_dict(r) for r in rows if r["vehicle_id"] in visible]

@app.post("/api/reminders")
def add_reminder(body:ReminderIn,request:Request):
    user=current_user(request)
    if not body.miles_interval and not body.months_interval and not body.due_date: raise HTTPException(400,"Choose a due date, miles, months, or a combination")
    stamp=now_iso()
    with db() as c:
        get_visible_vehicle(c,body.vehicle_id,user)
        cur=c.execute("""INSERT INTO reminders(vehicle_id,name,miles_interval,months_interval,last_date,last_mileage,due_date,repeats_yearly,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)""",(body.vehicle_id,body.name.strip(),body.miles_interval,body.months_interval,body.last_date,body.last_mileage,body.due_date or None,int(body.repeats_yearly),stamp,stamp))
        return reminder_dict(c.execute("SELECT * FROM reminders WHERE id=?",(cur.lastrowid,)).fetchone())

@app.put("/api/reminders/{item_id}")
def update_reminder(item_id:int,body:ReminderIn,request:Request):
    user=current_user(request)
    if not body.miles_interval and not body.months_interval and not body.due_date: raise HTTPException(400,"Choose a due date, miles, months, or a combination")
    with db() as c:
        get_visible_vehicle(c,body.vehicle_id,user)
        cur=c.execute("""UPDATE reminders SET vehicle_id=?,name=?,miles_interval=?,months_interval=?,last_date=?,last_mileage=?,due_date=?,repeats_yearly=?,updated_at=? WHERE id=?""",
          (body.vehicle_id,body.name.strip(),body.miles_interval,body.months_interval,body.last_date,body.last_mileage,body.due_date or None,int(body.repeats_yearly),now_iso(),item_id))
        if not cur.rowcount: raise HTTPException(404,"Reminder not found")
        return reminder_dict(c.execute("SELECT * FROM reminders WHERE id=?",(item_id,)).fetchone())

@app.delete("/api/reminders/{item_id}")
def delete_reminder(item_id:int,request:Request):
    user=current_user(request)
    with db() as c:
        entry=c.execute("SELECT vehicle_id FROM reminders WHERE id=?",(item_id,)).fetchone()
        if not entry: raise HTTPException(404,"Entry not found")
        get_visible_vehicle(c,entry["vehicle_id"],user)
        if not c.execute("DELETE FROM reminders WHERE id=?",(item_id,)).rowcount: raise HTTPException(404,"Reminder not found")
    return {"ok":True}

@app.get("/api/settings")
def get_settings(request: Request):
    current_user(request)
    with db() as c:
        return read_settings(c)

@app.put("/api/settings")
def update_settings(body: SettingsIn, request: Request):
    current_user(request, True)
    with db() as c:
        if body.garage_name is not None:
            name = body.garage_name.strip()
            if not name:
                raise HTTPException(400, "Garage name is required")
            c.execute("INSERT INTO settings(key,value) VALUES('garage_name',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (name,))
        for key in SETTINGS_KEYS[1:]:
            value = getattr(body, key)
            if value is not None:
                c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, "1" if value else "0"))
        return read_settings(c)

@app.get("/api/users")
def list_users(request:Request):
    current_user(request,True)
    with db() as c:return [public_user(r) for r in c.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE")]

@app.post("/api/users")
def create_user(body:UserCreate,request:Request):
    current_user(request,True); username=clean_username(body.username); pw,salt=password_record(body.password)
    try:
        with db() as c:
            cur=c.execute("INSERT INTO users(username,password_hash,salt,is_admin,active,created_at) VALUES(?,?,?,?,1,?)",
                          (username,pw,salt,int(body.is_admin),now_iso()))
            return public_user(c.execute("SELECT * FROM users WHERE id=?",(cur.lastrowid,)).fetchone())
    except sqlite3.IntegrityError: raise HTTPException(409,"Username already exists")

@app.put("/api/users/{item_id}")
def update_user(item_id:int,body:UserUpdate,request:Request):
    actor=current_user(request,True)
    with db() as c:
        row=c.execute("SELECT * FROM users WHERE id=?",(item_id,)).fetchone()
        if not row: raise HTTPException(404,"User not found")
        username=clean_username(body.username) if body.username is not None else row["username"]
        is_admin=int(body.is_admin if body.is_admin is not None else row["is_admin"])
        active=int(body.active if body.active is not None else row["active"])
        if actor["id"]==item_id and (not is_admin or not active): raise HTTPException(400,"You cannot deactivate yourself or remove your own administrator access")
        pw,salt=(row["password_hash"],row["salt"])
        if body.password: pw,salt=password_record(body.password)
        try:c.execute("UPDATE users SET username=?,password_hash=?,salt=?,is_admin=?,active=? WHERE id=?",(username,pw,salt,is_admin,active,item_id))
        except sqlite3.IntegrityError:raise HTTPException(409,"Username already exists")
        if not active:c.execute("DELETE FROM sessions WHERE user_id=?",(item_id,))
        return public_user(c.execute("SELECT * FROM users WHERE id=?",(item_id,)).fetchone())

def token_dict(row, raw: str | None = None) -> dict[str, Any]:
    out = {"id": row["id"], "name": row["name"], "prefix": row["prefix"], "created_at": row["created_at"], "last_used_at": row["last_used_at"]}
    if raw is not None:
        out["token"] = raw
    return out

class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)

@app.get("/api/tokens")
def list_tokens(request: Request):
    current_user(request, True)
    with db() as c:
        return [token_dict(r) for r in c.execute("SELECT * FROM api_tokens ORDER BY id")]

@app.post("/api/tokens", status_code=201)
def create_token(body: TokenIn, request: Request):
    user = current_user(request, True)
    raw = "gar_" + secrets.token_urlsafe(32)
    with db() as c:
        cur = c.execute("INSERT INTO api_tokens(name,token_hash,prefix,created_by,created_at) VALUES(?,?,?,?,?)",
                        (body.name.strip(), hashlib.sha256(raw.encode()).hexdigest(), raw[:11], user["id"], now_iso()))
        return token_dict(c.execute("SELECT * FROM api_tokens WHERE id=?", (cur.lastrowid,)).fetchone(), raw)

@app.delete("/api/tokens/{item_id}")
def revoke_token(item_id: int, request: Request):
    current_user(request, True)
    with db() as c:
        if not c.execute("DELETE FROM api_tokens WHERE id=?", (item_id,)).rowcount:
            raise HTTPException(404, "Token not found")
    return {"ok": True}

def token_auth(request: Request) -> sqlite3.Row:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    token_hash = hashlib.sha256(auth[7:].strip().encode()).hexdigest()
    with db() as c:
        row = c.execute("SELECT * FROM api_tokens WHERE token_hash=?", (token_hash,)).fetchone()
    if not row:
        ip = client_ip(request)
        retry = api_failure_retry_after(ip)
        if retry:
            raise HTTPException(429, "Too many failed API attempts. Try again later.", headers={"Retry-After": str(retry)})
        record_api_failure(ip)
        raise HTTPException(401, "Invalid API token", headers={"WWW-Authenticate": "Bearer"})
    retry = api_call_allowed(token_hash)
    if retry:
        raise HTTPException(429, "API rate limit exceeded. Try again later.", headers={"Retry-After": str(retry)})
    with db() as c:
        c.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now_iso(), row["id"]))
    return row


class NoteIn(BaseModel):
    vehicle_id: int
    date: str
    body: str = Field(min_length=1, max_length=2000)

def note_dict(c, row):
    return {"id": row["id"], "vehicle_id": row["vehicle_id"], "date": row["note_date"], "body": row["body"],
            "logged_by": display_user(c, row["logged_by"]), "logged_by_id": row["logged_by"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}

@app.get("/api/notes")
def list_notes(request: Request, vehicle_id: int | None = None):
    user=current_user(request)
    with db() as c:
        visible=visible_vehicle_ids(c,user)
        rows = c.execute("SELECT * FROM notes WHERE (? IS NULL OR vehicle_id=?) ORDER BY note_date DESC,id DESC", (vehicle_id, vehicle_id))
        return [note_dict(c, r) for r in rows if r["vehicle_id"] in visible]

@app.post("/api/notes", status_code=201)
def add_note(body: NoteIn, request: Request):
    user = current_user(request); stamp = now_iso()
    with db() as c:
        get_visible_vehicle(c,body.vehicle_id,user)
        if not c.execute("SELECT 1 FROM vehicles WHERE id=?", (body.vehicle_id,)).fetchone(): raise HTTPException(404, "Vehicle not found")
        cur = c.execute("INSERT INTO notes(vehicle_id,note_date,body,logged_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (body.vehicle_id, body.date, body.body.strip(), user["id"], stamp, stamp))
        return note_dict(c, c.execute("SELECT * FROM notes WHERE id=?", (cur.lastrowid,)).fetchone())

@app.put("/api/notes/{item_id}")
def update_note(item_id: int, body: NoteIn, request: Request):
    user=current_user(request)
    with db() as c:
        get_visible_vehicle(c,body.vehicle_id,user)
        cur = c.execute("UPDATE notes SET vehicle_id=?,note_date=?,body=?,updated_at=? WHERE id=?",
                        (body.vehicle_id, body.date, body.body.strip(), now_iso(), item_id))
        if not cur.rowcount: raise HTTPException(404, "Note not found")
        return note_dict(c, c.execute("SELECT * FROM notes WHERE id=?", (item_id,)).fetchone())

@app.delete("/api/notes/{item_id}")
def delete_note(item_id: int, request: Request):
    user=current_user(request)
    with db() as c:
        entry=c.execute("SELECT vehicle_id FROM notes WHERE id=?",(item_id,)).fetchone()
        if not entry: raise HTTPException(404,"Entry not found")
        get_visible_vehicle(c,entry["vehicle_id"],user)
        if not c.execute("DELETE FROM notes WHERE id=?", (item_id,)).rowcount: raise HTTPException(404, "Note not found")
    return {"ok": True}

class ModIn(BaseModel):
    vehicle_id: int
    name: str = Field(min_length=1, max_length=120)
    date: str | None = None
    price: float = Field(default=0, ge=0)
    torque_specs: str = Field(default="", max_length=500)
    fluids: str = Field(default="", max_length=500)
    gotchas: str = Field(default="", max_length=1000)
    youtube_url: str = Field(default="", max_length=500)

def mod_dict(c, row):
    return {"id":row["id"],"vehicle_id":row["vehicle_id"],"name":row["name"],"date":row["mod_date"],"price":row["price"],
            "logged_by":display_user(c,row["logged_by"]),"torque_specs":row["torque_specs"],"fluids":row["fluids"],"gotchas":row["gotchas"],"youtube_url":row["youtube_url"],"created_at":row["created_at"],"updated_at":row["updated_at"]}

@app.get("/api/mods")
def list_mods(request:Request,vehicle_id:int|None=None):
    user=current_user(request)
    with db() as c:
        visible=visible_vehicle_ids(c,user)
        rows=c.execute("SELECT * FROM modifications WHERE (? IS NULL OR vehicle_id=?) ORDER BY COALESCE(mod_date,'' ) DESC,id DESC",(vehicle_id,vehicle_id))
        return [mod_dict(c,r) for r in rows if r["vehicle_id"] in visible]

@app.post("/api/mods",status_code=201)
def add_mod(body:ModIn,request:Request):
    user=current_user(request);stamp=now_iso()
    with db() as c:
        get_visible_vehicle(c,body.vehicle_id,user)
        get_vehicle_or_404(c,body.vehicle_id)
        cur=c.execute("INSERT INTO modifications(vehicle_id,name,mod_date,price,logged_by,torque_specs,fluids,gotchas,youtube_url,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (body.vehicle_id,body.name.strip(),body.date or None,body.price,user["id"],body.torque_specs.strip(),body.fluids.strip(),body.gotchas.strip(),body.youtube_url.strip(),stamp,stamp))
        return mod_dict(c,c.execute("SELECT * FROM modifications WHERE id=?",(cur.lastrowid,)).fetchone())

@app.put("/api/mods/{item_id}")
def update_mod(item_id:int,body:ModIn,request:Request):
    user=current_user(request)
    with db() as c:
        get_visible_vehicle(c,body.vehicle_id,user)
        cur=c.execute("UPDATE modifications SET vehicle_id=?,name=?,mod_date=?,price=?,torque_specs=?,fluids=?,gotchas=?,youtube_url=?,updated_at=? WHERE id=?",
                      (body.vehicle_id,body.name.strip(),body.date or None,body.price,body.torque_specs.strip(),body.fluids.strip(),body.gotchas.strip(),body.youtube_url.strip(),now_iso(),item_id))
        if not cur.rowcount: raise HTTPException(404,"Modification not found")
        return mod_dict(c,c.execute("SELECT * FROM modifications WHERE id=?",(item_id,)).fetchone())

@app.delete("/api/mods/{item_id}")
def delete_mod(item_id:int,request:Request):
    user=current_user(request)
    with db() as c:
        entry=c.execute("SELECT vehicle_id FROM modifications WHERE id=?",(item_id,)).fetchone()
        if not entry: raise HTTPException(404,"Entry not found")
        get_visible_vehicle(c,entry["vehicle_id"],user)
        if not c.execute("DELETE FROM modifications WHERE id=?",(item_id,)).rowcount: raise HTTPException(404,"Modification not found")
    return {"ok":True}

class ServiceV1In(BaseModel):
    date: str
    mileage: int = Field(default=0, ge=0)
    type: str = Field(min_length=1, max_length=100)
    cost: float = Field(default=0, ge=0)
    provider: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=1000)
    reminder_id: int | None = None

@app.get("/api/v1/vehicles")
def v1_list_vehicles(request: Request):
    token=token_auth(request)
    with db() as c:
        user=token_user(c,token)
        return [vehicle_dict(c, r) for r in c.execute("SELECT * FROM vehicles ORDER BY id") if vehicle_accessible(r,user)]

@app.get("/api/v1/vehicles/{vehicle_id}/services")
def v1_list_services(vehicle_id: int, request: Request):
    token=token_auth(request)
    with db() as c:
        user=token_user(c,token)
        get_visible_vehicle(c, vehicle_id, user)
        rows = c.execute("SELECT * FROM services WHERE vehicle_id=? ORDER BY service_date DESC,id DESC", (vehicle_id,))
        return [service_dict(c, r) for r in rows]

@app.get("/api/v1/vehicles/{vehicle_id}/maintenance")
def v1_maintenance(vehicle_id: int, request: Request):
    token=token_auth(request)
    with db() as c:
        user=token_user(c,token)
        v = get_visible_vehicle(c, vehicle_id, user)
        mileage = effective_mileage(c, v)
        out = []
        for r in c.execute("SELECT * FROM reminders WHERE vehicle_id=? ORDER BY id", (vehicle_id,)):
            st = reminder_status(mileage, r)
            out.append({**reminder_dict(r), "status": st["state"], "progress": st["progress"], "label": st["label"]})
        return out

@app.get("/api/v1/vehicles/{vehicle_id}/fuel")
def v1_list_fuel(vehicle_id: int, request: Request):
    token=token_auth(request)
    with db() as c:
        user=token_user(c,token)
        get_visible_vehicle(c, vehicle_id, user)
        return fuel_rows(c, vehicle_id)

@app.post("/api/v1/vehicles/{vehicle_id}/services", status_code=201)
def v1_add_service(vehicle_id: int, body: ServiceV1In, request: Request):
    token = token_auth(request); stamp = now_iso()
    with db() as c:
        user=token_user(c,token)
        get_visible_vehicle(c, vehicle_id, user)
        apply_reminder_reset(c, vehicle_id, body.reminder_id, body.date, body.mileage)
        cur = c.execute("""INSERT INTO services(vehicle_id,service_date,mileage,service_type,cost,provider,notes,logged_by,reminder_id,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (vehicle_id, body.date, body.mileage, body.type.strip(), body.cost, body.provider.strip(), body.notes.strip(), token["created_by"], body.reminder_id, stamp, stamp))
        update_vehicle_mileage(c, vehicle_id, body.mileage, token["created_by"], stamp)
        return service_dict(c, c.execute("SELECT * FROM services WHERE id=?", (cur.lastrowid,)).fetchone())

@app.post("/api/v1/vehicles/{vehicle_id}/fuel", status_code=201)
async def v1_add_fuel(vehicle_id: int, request: Request, date: str = Form(...), odometer: int = Form(..., ge=0),
                      gallons: float = Form(..., gt=0), cost: float = Form(0, ge=0), file: UploadFile | None = File(None)):
    token = token_auth(request); stamp = now_iso()
    data = await file.read() if file else b""
    mime = (file.content_type or "").lower() if file else ""
    if file and data:
        if mime not in RECEIPT_TYPES:
            raise HTTPException(400, "Receipt photos must be JPEG, PNG, WebP, GIF, or HEIC images")
        if len(data) > RECEIPT_MAX_BYTES:
            raise HTTPException(400, "Receipt photos are limited to 10 MB")
    with db() as c:
        user=token_user(c,token)
        get_visible_vehicle(c, vehicle_id, user)
        cur = c.execute("INSERT INTO fuel_entries(vehicle_id,fill_date,odometer,gallons,cost,logged_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (vehicle_id, date, odometer, gallons, cost, token["created_by"], stamp, stamp))
        fuel_id = cur.lastrowid
        update_vehicle_mileage(c, vehicle_id, odometer, token["created_by"], stamp)
        receipt = None
        if file and data:
            stored = f"{secrets.token_hex(16)}{RECEIPT_TYPES[mime]}"
            (RECEIPTS_DIR / stored).write_bytes(data)
            rcur = c.execute("INSERT INTO receipts(kind,entry_id,stored_name,orig_name,mime,size,uploaded_by,created_at) VALUES('fuel',?,?,?,?,?,?,?)",
                             (fuel_id, stored, (file.filename or "")[:120], mime, len(data), token["created_by"], stamp))
            receipt = receipt_dict(c, c.execute("SELECT * FROM receipts WHERE id=?", (rcur.lastrowid,)).fetchone())
        entry = one_fuel(c, fuel_id)
        if receipt:
            entry["receipt"] = receipt
        return entry



class MileageV1In(BaseModel):
    mileage: int = Field(ge=0)
    date: str | None = None

@app.put("/api/v1/vehicles/{vehicle_id}/mileage")
def v1_update_mileage(vehicle_id: int, body: MileageV1In, request: Request):
    token=token_auth(request); stamp=now_iso()
    with db() as c:
        user=token_user(c,token)
        get_visible_vehicle(c, vehicle_id, user)
        c.execute("UPDATE vehicles SET mileage=?,updated_at=? WHERE id=?", (body.mileage, stamp, vehicle_id))
        c.execute("INSERT INTO mileage_updates(vehicle_id,mileage,recorded_by,recorded_at) VALUES(?,?,?,?)",
                  (vehicle_id,body.mileage,token["created_by"],stamp))
        return vehicle_dict(c, c.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone())

class NoteV1In(BaseModel):
    date: str
    body: str = Field(min_length=1, max_length=2000)

@app.get("/api/v1/vehicles/{vehicle_id}/notes")
def v1_list_notes(vehicle_id: int, request: Request):
    token=token_auth(request)
    with db() as c:
        user=token_user(c,token)
        get_visible_vehicle(c, vehicle_id, user)
        rows = c.execute("SELECT * FROM notes WHERE vehicle_id=? ORDER BY note_date DESC,id DESC", (vehicle_id,))
        return [note_dict(c, r) for r in rows if r["vehicle_id"] in visible]

@app.post("/api/v1/vehicles/{vehicle_id}/notes", status_code=201)
def v1_add_note(vehicle_id: int, body: NoteV1In, request: Request):
    token = token_auth(request); stamp = now_iso()
    with db() as c:
        user=token_user(c,token)
        get_visible_vehicle(c, vehicle_id, user)
        cur = c.execute("INSERT INTO notes(vehicle_id,note_date,body,logged_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (vehicle_id, body.date, body.body.strip(), token["created_by"], stamp, stamp))
        return note_dict(c, c.execute("SELECT * FROM notes WHERE id=?", (cur.lastrowid,)).fetchone())

def send_notification(urls: str, title: str, body: str) -> tuple[bool, str]:
    targets = urls.split()
    webhook_urls = [u for u in targets if u.startswith(("http://", "https://"))]
    apprise_urls = [u for u in targets if u not in webhook_urls]
    ok = False
    errors: list[str] = []
    for url in webhook_urls:
        try:
            req = urllib.request.Request(url, data=json.dumps({"title": title, "body": body}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                ok = ok or resp.status < 400
        except Exception as exc:
            errors.append(f"webhook {url}: {exc}")
    if apprise_urls:
        try:
            import apprise
        except ImportError:
            errors.append("apprise is not installed; only http(s) webhook URLs work without it")
        else:
            ap = apprise.Apprise()
            for url in apprise_urls:
                ap.add(url)
            if ap.notify(title=title, body=body):
                ok = True
            else:
                errors.append("apprise delivery failed; check the URL and its service")
    return ok, "; ".join(errors)

def due_maintenance_items(c) -> list[dict[str, Any]]:
    items = []
    for v in c.execute("SELECT * FROM vehicles"):
        mileage = effective_mileage(c, v)
        for r in c.execute("SELECT * FROM reminders WHERE vehicle_id=?", (v["id"],)):
            st = reminder_status(mileage, r)
            if st["state"] in ("soon", "overdue"):
                items.append({"reminder_id": r["id"], "vehicle": v["name"], "item": r["name"], "state": st["state"], "label": st["label"]})
    return items

def run_notification_check() -> bool:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key='notify_urls'").fetchone()
        urls = row["value"] if row else ""
        if not urls.strip():
            return False
        row = c.execute("SELECT value FROM settings WHERE key='notify_state'").fetchone()
        try:
            known = json.loads(row["value"]) if row else {}
        except json.JSONDecodeError:
            known = {}
        items = due_maintenance_items(c)
        fresh = [i for i in items if known.get(str(i["reminder_id"])) != i["state"]]
        if fresh:
            name_row = c.execute("SELECT value FROM settings WHERE key='garage_name'").fetchone()
            title = f"{name_row['value'] if name_row else 'Garage'}: maintenance due"
            body = "\n".join(f"{'OVERDUE' if i['state'] == 'overdue' else 'Due soon'}: {i['vehicle']} - {i['item']} ({i['label']})" for i in fresh)
            ok, detail = send_notification(urls, title, body)
            if not ok:
                raise RuntimeError(detail or "notification delivery failed")
        current = {str(i["reminder_id"]): i["state"] for i in items}
        c.execute("INSERT INTO settings(key,value) VALUES('notify_state',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(current),))
        c.execute("INSERT INTO settings(key,value) VALUES('notify_last_check',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (datetime.now(timezone.utc).date().isoformat(),))
        return bool(fresh)

def notification_worker():
    while True:
        try:
            with db() as c:
                row = c.execute("SELECT value FROM settings WHERE key='notify_last_check'").fetchone()
            if (row["value"] if row else None) != datetime.now(timezone.utc).date().isoformat():
                run_notification_check()
        except Exception as exc:
            print(f"notification check failed: {exc}")
        time.sleep(3600)

class NotificationSettingsIn(BaseModel):
    apprise_urls: str = Field(default="", max_length=2000)

@app.get("/api/notifications")
def get_notification_settings(request: Request):
    current_user(request, True)
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key='notify_urls'").fetchone()
    return {"apprise_urls": row["value"] if row else ""}

@app.put("/api/notifications")
def update_notification_settings(body: NotificationSettingsIn, request: Request):
    current_user(request, True)
    urls = body.apprise_urls.strip()
    if any("://" not in u for u in urls.split()):
        raise HTTPException(400, "Each notification URL needs a scheme, like tgram:// or https://")
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES('notify_urls',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (urls,))
    return {"apprise_urls": urls}

@app.post("/api/notifications/test")
def send_test_notification(request: Request):
    current_user(request, True)
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key='notify_urls'").fetchone()
    urls = row["value"] if row else ""
    if not urls.strip():
        raise HTTPException(400, "Add at least one notification URL first")
    ok, detail = send_notification(urls, "Garage: test notification", "Notifications are working. Maintenance alerts will arrive here.")
    if not ok:
        raise HTTPException(502, detail or "Notification delivery failed")
    return {"ok": True}

@app.get("/api/export")
def export_data(request:Request):
    current_user(request)
    with db() as c:
        data={"version":2,"exported_at":now_iso(),"vehicles":[vehicle_dict(c,r) for r in c.execute("SELECT * FROM vehicles")],
              "services":[service_dict(c,r) for r in c.execute("SELECT * FROM services")],
              "reminders":[reminder_dict(r) for r in c.execute("SELECT * FROM reminders")],
              "fuel":[r for r in fuel_rows(c)]}
    return data

@app.post("/api/import")
def import_data(payload:dict[str,Any],request:Request):
    user=current_user(request)
    vehicles=payload.get("vehicles",[]); services=payload.get("services",[]); reminders=payload.get("reminders",[]); fuel=payload.get("fuel",[])
    if not isinstance(vehicles,list) or not isinstance(services,list) or not isinstance(reminders,list) or not isinstance(fuel,list): raise HTTPException(400,"Invalid JSON backup")
    stamp=now_iso(); idmap={}
    with db() as c:
        for r in c.execute("SELECT stored_name FROM receipts"): (RECEIPTS_DIR / r["stored_name"]).unlink(missing_ok=True)
        c.execute("DELETE FROM receipts");c.execute("DELETE FROM services");c.execute("DELETE FROM reminders");c.execute("DELETE FROM fuel_entries");c.execute("DELETE FROM mileage_updates");c.execute("DELETE FROM vehicles")
        for v in vehicles:
            cur=c.execute("INSERT INTO vehicles(name,year,mileage,icon,added_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
              (str(v.get("name","Vehicle"))[:80],str(v.get("year",""))[:4],max(0,int(v.get("mileage",0))),str(v.get("icon","🚗"))[:8],user["id"],stamp,stamp));idmap[str(v.get("id"))]=cur.lastrowid
        for s in services:
            vid=idmap.get(str(s.get("vehicle_id",s.get("vehicleId")))); 
            if not vid: continue
            c.execute("""INSERT INTO services(vehicle_id,service_date,mileage,service_type,cost,provider,notes,logged_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (vid,str(s.get("date",""))[:10],max(0,int(s.get("mileage",0))),str(s.get("type","Service"))[:100],max(0,float(s.get("cost",0))),str(s.get("provider",s.get("who","")))[:100],str(s.get("notes",""))[:1000],user["id"],stamp,stamp))
        for f in fuel:
            vid=idmap.get(str(f.get("vehicle_id",f.get("vehicleId"))))
            if not vid: continue
            c.execute("""INSERT INTO fuel_entries(vehicle_id,fill_date,odometer,gallons,cost,logged_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
              (vid,str(f.get("date",""))[:10],max(0,int(f.get("odometer",0))),max(0.001,float(f.get("gallons",1))),max(0,float(f.get("cost",0))),user["id"],stamp,stamp))
        for r in reminders:
            vid=idmap.get(str(r.get("vehicle_id",r.get("vehicleId")))); 
            if not vid: continue
            c.execute("""INSERT INTO reminders(vehicle_id,name,miles_interval,months_interval,last_date,last_mileage,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
              (vid,str(r.get("name","Maintenance"))[:100],r.get("miles_interval",r.get("milesInterval")),r.get("months_interval",r.get("monthsInterval")),str(r.get("last_date",r.get("lastDate","")))[:10],max(0,int(r.get("last_mileage",r.get("lastMileage",0)))),stamp,stamp))
    return {"ok":True,"vehicles":len(idmap)}

app.mount("/static",StaticFiles(directory=BASE/"static"),name="static")

@app.get("/{path:path}",include_in_schema=False)
def frontend(path:str):
    if path.startswith("api/"): raise HTTPException(404)
    return FileResponse(BASE/"static"/"index.html")
