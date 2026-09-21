from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("GARAGE_DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "garage.db"
COOKIE = "garage_session"
SESSION_DAYS = 30
PBKDF2_ITERATIONS = 260_000
LOGIN_LIMIT = 5
LOGIN_WINDOW_SECONDS = 15 * 60
_login_failures: dict[str, deque[float]] = defaultdict(deque)
_login_lock = threading.Lock()

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
          logged_by INTEGER NOT NULL REFERENCES users(id),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reminders (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          miles_interval INTEGER,
          months_interval INTEGER,
          last_date TEXT NOT NULL,
          last_mileage INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """)
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
    return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"]), "active": bool(row["active"])}

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

def display_user(c: sqlite3.Connection, user_id: int | None) -> str:
    if user_id is None:
        return "System"
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

class ServiceIn(BaseModel):
    vehicle_id: int
    date: str
    mileage: int = Field(default=0, ge=0)
    type: str = Field(min_length=1, max_length=100)
    cost: float = Field(default=0, ge=0)
    provider: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=1000)

class ReminderIn(BaseModel):
    vehicle_id: int
    name: str = Field(min_length=1, max_length=100)
    miles_interval: int | None = Field(default=None, ge=1)
    months_interval: int | None = Field(default=None, ge=1)
    last_date: str
    last_mileage: int = Field(default=0, ge=0)

SETTINGS_KEYS = ("garage_name", "hide_service_log", "hide_maintenance", "hide_costs", "hide_fuel")

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

def vehicle_dict(c, row):
    return {"id":row["id"],"name":row["name"],"year":row["year"],"mileage":row["mileage"],"icon":row["icon"],
            "added_by":display_user(c,row["added_by"]),"created_at":row["created_at"],"updated_at":row["updated_at"]}

@app.get("/api/vehicles")
def list_vehicles(request: Request):
    current_user(request)
    with db() as c: return [vehicle_dict(c,r) for r in c.execute("SELECT * FROM vehicles ORDER BY id")]

@app.post("/api/vehicles")
def add_vehicle(body: VehicleIn, request: Request):
    user=current_user(request); stamp=now_iso()
    with db() as c:
        cur=c.execute("INSERT INTO vehicles(name,year,mileage,icon,added_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                      (body.name.strip(),body.year.strip(),body.mileage,body.icon,user["id"],stamp,stamp))
        row=c.execute("SELECT * FROM vehicles WHERE id=?",(cur.lastrowid,)).fetchone(); return vehicle_dict(c,row)

@app.put("/api/vehicles/{item_id}")
def update_vehicle(item_id:int, body:VehicleIn, request:Request):
    current_user(request)
    with db() as c:
        cur=c.execute("UPDATE vehicles SET name=?,year=?,mileage=?,icon=?,updated_at=? WHERE id=?",
                      (body.name.strip(),body.year.strip(),body.mileage,body.icon,now_iso(),item_id))
        if not cur.rowcount: raise HTTPException(404,"Vehicle not found")
        return vehicle_dict(c,c.execute("SELECT * FROM vehicles WHERE id=?",(item_id,)).fetchone())

@app.delete("/api/vehicles/{item_id}")
def delete_vehicle(item_id:int, request:Request):
    current_user(request)
    with db() as c:
        if not c.execute("DELETE FROM vehicles WHERE id=?",(item_id,)).rowcount: raise HTTPException(404,"Vehicle not found")
    return {"ok":True}

def service_dict(c,row):
    return {"id":row["id"],"vehicle_id":row["vehicle_id"],"date":row["service_date"],"mileage":row["mileage"],
            "type":row["service_type"],"cost":row["cost"],"provider":row["provider"],"notes":row["notes"],
            "logged_by":display_user(c,row["logged_by"]),"logged_by_id":row["logged_by"],"created_at":row["created_at"],"updated_at":row["updated_at"]}

@app.get("/api/services")
def list_services(request:Request, vehicle_id:int|None=None):
    current_user(request)
    with db() as c:
        rows=c.execute("SELECT * FROM services WHERE (? IS NULL OR vehicle_id=?) ORDER BY service_date DESC,id DESC",(vehicle_id,vehicle_id))
        return [service_dict(c,r) for r in rows]

@app.post("/api/services")
def add_service(body:ServiceIn, request:Request):
    user=current_user(request); stamp=now_iso()
    with db() as c:
        if not c.execute("SELECT 1 FROM vehicles WHERE id=?",(body.vehicle_id,)).fetchone(): raise HTTPException(404,"Vehicle not found")
        cur=c.execute("""INSERT INTO services(vehicle_id,service_date,mileage,service_type,cost,provider,notes,logged_by,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)""",(body.vehicle_id,body.date,body.mileage,body.type.strip(),body.cost,body.provider.strip(),body.notes.strip(),user["id"],stamp,stamp))
        c.execute("UPDATE vehicles SET mileage=MAX(mileage,?),updated_at=? WHERE id=?",(body.mileage,stamp,body.vehicle_id))
        return service_dict(c,c.execute("SELECT * FROM services WHERE id=?",(cur.lastrowid,)).fetchone())

@app.put("/api/services/{item_id}")
def update_service(item_id:int,body:ServiceIn,request:Request):
    current_user(request)
    with db() as c:
        cur=c.execute("""UPDATE services SET vehicle_id=?,service_date=?,mileage=?,service_type=?,cost=?,provider=?,notes=?,updated_at=? WHERE id=?""",
          (body.vehicle_id,body.date,body.mileage,body.type.strip(),body.cost,body.provider.strip(),body.notes.strip(),now_iso(),item_id))
        if not cur.rowcount: raise HTTPException(404,"Service not found")
        return service_dict(c,c.execute("SELECT * FROM services WHERE id=?",(item_id,)).fetchone())

@app.delete("/api/services/{item_id}")
def delete_service(item_id:int,request:Request):
    current_user(request)
    with db() as c:
        if not c.execute("DELETE FROM services WHERE id=?",(item_id,)).rowcount: raise HTTPException(404,"Service not found")
    return {"ok":True}

def reminder_dict(row):
    return {"id":row["id"],"vehicle_id":row["vehicle_id"],"name":row["name"],"miles_interval":row["miles_interval"],
            "months_interval":row["months_interval"],"last_date":row["last_date"],"last_mileage":row["last_mileage"]}

@app.get("/api/reminders")
def list_reminders(request:Request,vehicle_id:int|None=None):
    current_user(request)
    with db() as c:
        rows=c.execute("SELECT * FROM reminders WHERE (? IS NULL OR vehicle_id=?) ORDER BY id",(vehicle_id,vehicle_id))
        return [reminder_dict(r) for r in rows]

@app.post("/api/reminders")
def add_reminder(body:ReminderIn,request:Request):
    current_user(request)
    if not body.miles_interval and not body.months_interval: raise HTTPException(400,"Choose miles, months, or both")
    stamp=now_iso()
    with db() as c:
        cur=c.execute("""INSERT INTO reminders(vehicle_id,name,miles_interval,months_interval,last_date,last_mileage,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?)""",(body.vehicle_id,body.name.strip(),body.miles_interval,body.months_interval,body.last_date,body.last_mileage,stamp,stamp))
        return reminder_dict(c.execute("SELECT * FROM reminders WHERE id=?",(cur.lastrowid,)).fetchone())

@app.put("/api/reminders/{item_id}")
def update_reminder(item_id:int,body:ReminderIn,request:Request):
    current_user(request)
    if not body.miles_interval and not body.months_interval: raise HTTPException(400,"Choose miles, months, or both")
    with db() as c:
        cur=c.execute("""UPDATE reminders SET vehicle_id=?,name=?,miles_interval=?,months_interval=?,last_date=?,last_mileage=?,updated_at=? WHERE id=?""",
          (body.vehicle_id,body.name.strip(),body.miles_interval,body.months_interval,body.last_date,body.last_mileage,now_iso(),item_id))
        if not cur.rowcount: raise HTTPException(404,"Reminder not found")
        return reminder_dict(c.execute("SELECT * FROM reminders WHERE id=?",(item_id,)).fetchone())

@app.delete("/api/reminders/{item_id}")
def delete_reminder(item_id:int,request:Request):
    current_user(request)
    with db() as c:
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

@app.get("/api/export")
def export_data(request:Request):
    current_user(request)
    with db() as c:
        data={"version":2,"exported_at":now_iso(),"vehicles":[vehicle_dict(c,r) for r in c.execute("SELECT * FROM vehicles")],
              "services":[service_dict(c,r) for r in c.execute("SELECT * FROM services")],
              "reminders":[reminder_dict(r) for r in c.execute("SELECT * FROM reminders")]}
    return data

@app.post("/api/import")
def import_data(payload:dict[str,Any],request:Request):
    user=current_user(request)
    vehicles=payload.get("vehicles",[]); services=payload.get("services",[]); reminders=payload.get("reminders",[])
    if not isinstance(vehicles,list) or not isinstance(services,list) or not isinstance(reminders,list): raise HTTPException(400,"Invalid JSON backup")
    stamp=now_iso(); idmap={}
    with db() as c:
        c.execute("DELETE FROM services");c.execute("DELETE FROM reminders");c.execute("DELETE FROM vehicles")
        for v in vehicles:
            cur=c.execute("INSERT INTO vehicles(name,year,mileage,icon,added_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
              (str(v.get("name","Vehicle"))[:80],str(v.get("year",""))[:4],max(0,int(v.get("mileage",0))),str(v.get("icon","🚗"))[:8],user["id"],stamp,stamp));idmap[str(v.get("id"))]=cur.lastrowid
        for s in services:
            vid=idmap.get(str(s.get("vehicle_id",s.get("vehicleId")))); 
            if not vid: continue
            c.execute("""INSERT INTO services(vehicle_id,service_date,mileage,service_type,cost,provider,notes,logged_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (vid,str(s.get("date",""))[:10],max(0,int(s.get("mileage",0))),str(s.get("type","Service"))[:100],max(0,float(s.get("cost",0))),str(s.get("provider",s.get("who","")))[:100],str(s.get("notes",""))[:1000],user["id"],stamp,stamp))
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
