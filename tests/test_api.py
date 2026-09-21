import os
from pathlib import Path
os.environ['GARAGE_DATA_DIR']='/tmp/garage-pytest-data'
from fastapi.testclient import TestClient
from app import main

def test_shared_garage_and_permissions(tmp_path):
    main.DB_PATH=tmp_path/'garage.db'; main.init_db()
    with TestClient(main.app) as admin:
        assert admin.post('/api/setup',json={'username':'admin-test','password':'password-123'}).status_code==200
        assert admin.get('/api/vehicles').json() == [{'id': 1, 'name': 'Ford Mustang', 'year': '1969', 'mileage': 0, 'icon': '🚗', 'added_by': 'System', 'created_at': admin.get('/api/vehicles').json()[0]['created_at'], 'updated_at': admin.get('/api/vehicles').json()[0]['updated_at']}]
        assert admin.post('/api/users',json={'username':'member-test','password':'password-456','is_admin':False}).status_code==200
        service={'vehicle_id':1,'date':'2026-09-20','mileage':24000,'type':'Oil change','cost':55,'provider':'DIY','notes':''}
        assert admin.post('/api/services',json=service).status_code==200
        reminder={'vehicle_id':1,'name':'Oil change','miles_interval':5000,'months_interval':6,'last_date':'2026-03-01','last_mileage':20000}
        assert admin.post('/api/reminders',json=reminder).status_code==200
    with TestClient(main.app) as member:
        assert member.post('/api/login',json={'username':'member-test','password':'password-456'}).status_code==200
        rows=member.get('/api/services?vehicle_id=1').json()
        assert rows[0]['logged_by']=='admin-test'
        assert member.get('/api/users').status_code==403


def test_security_headers_and_login_rate_limit(tmp_path):
    main.DB_PATH = tmp_path / 'security.db'
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as client:
        response = client.get('/api/status')
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert response.headers['x-frame-options'] == 'DENY'
        assert response.headers['referrer-policy'] == 'no-referrer'
        assert "default-src 'self'" in response.headers['content-security-policy']
        client.post('/api/setup', json={'username': 'admin-test', 'password': 'password-123'})
    main._login_failures.clear()
    with TestClient(main.app) as client:
        for _ in range(main.LOGIN_LIMIT):
            response = client.post('/api/login', json={'username': 'admin-test', 'password': 'wrong-password'})
            assert response.status_code == 401
            assert response.json()['detail'] == 'Invalid username or password'
        response = client.post('/api/login', json={'username': 'admin-test', 'password': 'wrong-password'})
        assert response.status_code == 429
        assert int(response.headers['retry-after']) > 0
        assert response.json()['detail'] == 'Too many login attempts. Try again later.'


def test_admin_can_rename_garage_and_member_cannot(tmp_path):
    main.DB_PATH = tmp_path / 'settings.db'
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup', json={'username':'admin-test','password':'password-123'})
        assert admin.get('/api/settings').json() == {'garage_name':'Your Garage','hide_service_log':False,'hide_maintenance':False,'hide_costs':False,'hide_fuel':False}
        assert admin.put('/api/settings', json={'garage_name':'Test Garage'}).json()['garage_name'] == 'Test Garage'
        admin.post('/api/users', json={'username':'member-test','password':'password-456','is_admin':False})
    with TestClient(main.app) as member:
        member.post('/api/login', json={'username':'member-test','password':'password-456'})
        assert member.get('/api/settings').json()['garage_name'] == 'Test Garage'
        assert member.put('/api/settings', json={'garage_name':'Nope'}).status_code == 403


def test_admin_can_hide_vehicle_sections(tmp_path):
    main.DB_PATH = tmp_path / 'sections.db'
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup', json={'username':'admin-test','password':'password-123'})
        result = admin.put('/api/settings', json={'hide_fuel':True,'hide_costs':True}).json()
        assert result['hide_fuel'] is True and result['hide_costs'] is True
        assert result['hide_service_log'] is False and result['hide_maintenance'] is False
        assert result['garage_name'] == 'Your Garage'
        admin.post('/api/users', json={'username':'member-test','password':'password-456','is_admin':False})
    with TestClient(main.app) as member:
        member.post('/api/login', json={'username':'member-test','password':'password-456'})
        settings = member.get('/api/settings').json()
        assert settings['hide_fuel'] is True and settings['hide_costs'] is True
        assert member.put('/api/settings', json={'hide_fuel':False}).status_code == 403


def test_fuel_log_computes_mpg_and_bumps_mileage(tmp_path):
    main.DB_PATH = tmp_path / 'fuel.db'
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup', json={'username':'admin-test','password':'password-123'})
        first = admin.post('/api/fuel', json={'vehicle_id':1,'date':'2026-09-01','odometer':20000,'gallons':10,'cost':35})
        assert first.status_code == 200 and first.json()['mpg'] is None
        second = admin.post('/api/fuel', json={'vehicle_id':1,'date':'2026-09-15','odometer':20300,'gallons':10,'cost':37}).json()
        assert second['mpg'] == 30.0
        rows = admin.get('/api/fuel?vehicle_id=1').json()
        assert [r['odometer'] for r in rows] == [20300, 20000]
        assert admin.get('/api/vehicles').json()[0]['mileage'] == 20300
        admin.post('/api/users', json={'username':'member-test','password':'password-456','is_admin':False})
    with TestClient(main.app) as member:
        member.post('/api/login', json={'username':'member-test','password':'password-456'})
        assert member.put('/api/fuel/%s' % second['id'], json={'vehicle_id':1,'date':'2026-09-15','odometer':20310,'gallons':10,'cost':37}).status_code == 200
        assert member.delete('/api/fuel/%s' % second['id']).status_code == 200
        assert len(member.get('/api/fuel?vehicle_id=1').json()) == 1


PNG_BYTES = __import__('base64').b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')

Add receipt photo uploads for service and fuel entriesdef test_receipt_upload_view_delete(tmp_path):
    main.DB_PATH = tmp_path / 'receipts.db'
    main.RECEIPTS_DIR = tmp_path / 'receipts'
    main.RECEIPTS_DIR.mkdir(exist_ok=True)
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup', json={'username':'admin-test','password':'password-123'})
        admin.post('/api/services', json={'vehicle_id':1,'date':'2026-09-20','mileage':24000,'type':'Oil change','cost':55})
        admin.post('/api/fuel', json={'vehicle_id':1,'date':'2026-09-20','odometer':24000,'gallons':10,'cost':35})
        created = admin.post('/api/receipts', data={'kind':'service','entry_id':'1'}, files={'file':('receipt.png', PNG_BYTES, 'image/png')})
        assert created.status_code == 201
        rid = created.json()['id']
        assert admin.post('/api/receipts', data={'kind':'fuel','entry_id':'1'}, files={'file':('fuel.png', PNG_BYTES, 'image/png')}).status_code == 201
        assert admin.post('/api/receipts', data={'kind':'service','entry_id':'1'}, files={'file':('notes.txt', b'nope', 'text/plain')}).status_code == 400
        assert len(admin.get('/api/receipts?kind=service&entry_id=1').json()) == 1
        served = admin.get(f'/api/receipts/{rid}')
        assert served.status_code == 200 and served.content == PNG_BYTES
        assert admin.delete('/api/services/1').status_code == 200
        assert admin.get('/api/receipts?kind=service&entry_id=1').json() == []
        assert admin.get(f'/api/receipts/{rid}').status_code == 404
        remaining = admin.get('/api/receipts?kind=fuel&entry_id=1').json()
        assert admin.delete(f"/api/receipts/{remaining[0]['id']}").status_code == 200
    with TestClient(main.app) as anon:
        assert anon.get('/api/receipts').status_code == 401
