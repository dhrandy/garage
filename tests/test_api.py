import os
from pathlib import Path
os.environ['GARAGE_DATA_DIR']='/tmp/garage-pytest-data'
os.environ['GARAGE_NOTIFY_WORKER']='false'
from fastapi.testclient import TestClient
from app import main

def test_shared_garage_and_permissions(tmp_path):
    main.DB_PATH=tmp_path/'garage.db'; main.init_db()
    with TestClient(main.app) as admin:
        assert admin.post('/api/setup',json={'username':'admin-test','password':'password-123'}).status_code==200
        vehicle = admin.get('/api/vehicles').json()[0]
        assert {k: vehicle[k] for k in ('id','name','year','mileage','icon','added_by')} == {'id': 1, 'name': 'Ford Mustang', 'year': '1969', 'mileage': 0, 'icon': '🚗', 'added_by': 'System'}
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
        assert admin.get('/api/settings').json() == {'garage_name':'Your Garage','hide_maintenance':False,'hide_costs':False,'hide_fuel':False,'hide_notes':False,'use_vehicle_photos':False,'use_kilometers':False}
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
        assert result['hide_maintenance'] is False
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

def test_receipt_upload_view_delete(tmp_path):
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


def test_api_tokens_and_v1_endpoints(tmp_path):
    main.DB_PATH = tmp_path / 'tokens.db'
    main.RECEIPTS_DIR = tmp_path / 'receipts'
    main.RECEIPTS_DIR.mkdir(exist_ok=True)
    main._login_failures.clear(); main._api_calls.clear(); main._api_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup', json={'username':'admin-test','password':'password-123'})
        created = admin.post('/api/tokens', json={'name':'script'})
        assert created.status_code == 201
        token = created.json()['token']
        assert token.startswith('gar_') and created.json()['prefix'] == token[:11]
        assert 'token' not in admin.get('/api/tokens').json()[0]
        admin.post('/api/users', json={'username':'member-test','password':'password-456','is_admin':False})
        auth = {'Authorization': f'Bearer {token}'}
        assert admin.get('/api/v1/vehicles', headers=auth).json()[0]['name'] == 'Ford Mustang'
        service = admin.post('/api/v1/vehicles/1/services', headers=auth, json={'date':'2026-09-20','mileage':24000,'type':'Oil change','cost':55})
        assert service.status_code == 201 and service.json()['logged_by'] == 'admin-test'
        assert len(admin.get('/api/v1/vehicles/1/services', headers=auth).json()) == 1
        fuel = admin.post('/api/v1/vehicles/1/fuel', headers=auth, data={'date':'2026-09-20','odometer':'24000','gallons':'10','cost':'35'}, files={'file':('receipt.png', PNG_BYTES, 'image/png')})
        assert fuel.status_code == 201 and fuel.json()['receipt']['mime'] == 'image/png'
        assert len(admin.get('/api/v1/vehicles/1/fuel', headers=auth).json()) == 1
        admin.post('/api/reminders', json={'vehicle_id':1,'name':'Oil change','miles_interval':5000,'months_interval':None,'last_date':'2026-09-01','last_mileage':20000})
        status = admin.get('/api/v1/vehicles/1/maintenance', headers=auth).json()
        assert status[0]['status'] == 'soon' and status[0]['label'] == '1,000 mi remaining'
        assert admin.get('/api/v1/vehicles', headers={'Authorization':'Bearer gar_wrong'}).status_code == 401
        tid = created.json()['id']
        assert admin.delete(f'/api/tokens/{tid}').status_code == 200
        assert admin.get('/api/v1/vehicles', headers=auth).status_code == 401
    with TestClient(main.app) as member:
        member.post('/api/login', json={'username':'member-test','password':'password-456'})
        assert member.get('/api/tokens').status_code == 403
        assert member.post('/api/tokens', json={'name':'x'}).status_code == 403


def test_api_invalid_token_rate_limit(tmp_path):
    main.DB_PATH = tmp_path / 'api-limit.db'
    main._login_failures.clear(); main._api_calls.clear(); main._api_failures.clear()
    main.init_db()
    with TestClient(main.app) as client:
        client.post('/api/setup', json={'username':'admin-test','password':'password-123'})
        for _ in range(main.API_FAIL_LIMIT):
            assert client.get('/api/v1/vehicles', headers={'Authorization':'Bearer gar_wrong'}).status_code == 401
        response = client.get('/api/v1/vehicles', headers={'Authorization':'Bearer gar_wrong'})
        assert response.status_code == 429 and int(response.headers['retry-after']) > 0


def test_service_can_reset_maintenance_item(tmp_path):
    main.DB_PATH = tmp_path / 'reset.db'
    main._login_failures.clear(); main._api_calls.clear(); main._api_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup', json={'username':'admin-test','password':'password-123'})
        reminder = admin.post('/api/reminders', json={'vehicle_id':1,'name':'Oil change','miles_interval':5000,'months_interval':6,'last_date':'2026-03-01','last_mileage':20000}).json()
        before = len(admin.get('/api/services?vehicle_id=1').json())
        service = admin.post('/api/services', json={'vehicle_id':1,'date':'2026-09-20','mileage':24000,'type':'Oil change','cost':55,'reminder_id':reminder['id']})
        assert service.status_code == 200 and service.json()['reminder_id'] == reminder['id']
        updated = admin.get('/api/reminders?vehicle_id=1').json()[0]
        assert updated['last_date'] == '2026-09-20' and updated['last_mileage'] == 24000
        assert len(admin.get('/api/services?vehicle_id=1').json()) == before + 1
        assert admin.post('/api/services', json={'vehicle_id':1,'date':'2026-09-20','mileage':24000,'type':'X','reminder_id':999}).status_code == 400
        token = admin.post('/api/tokens', json={'name':'script'}).json()['token']
        via_api = admin.post('/api/v1/vehicles/1/services', headers={'Authorization': f'Bearer {token}'},
                             json={'date':'2026-09-21','mileage':24100,'type':'Oil change','reminder_id':reminder['id']})
        assert via_api.status_code == 201
        assert admin.get('/api/reminders?vehicle_id=1').json()[0]['last_mileage'] == 24100


def test_estimated_mileage_from_fillups_and_services(tmp_path):
    main.DB_PATH = tmp_path / 'estimate.db'
    main._login_failures.clear(); main._api_calls.clear(); main._api_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup', json={'username':'admin-test','password':'password-123'})
        admin.post('/api/services', json={'vehicle_id':1,'date':'2026-06-01','mileage':20000,'type':'Tires'})
        admin.post('/api/fuel', json={'vehicle_id':1,'date':'2026-09-01','odometer':23000,'gallons':10,'cost':35})
        vehicle = admin.get('/api/vehicles').json()[0]
        assert vehicle['mileage'] == 23000
        assert vehicle['miles_per_day'] == 32.6  # 3000 miles over 92 days
        assert vehicle['est_mileage'] >= 23000
        with main.db() as c:
            row = c.execute('SELECT * FROM vehicles WHERE id=1').fetchone()
            est = main.mileage_estimate(c, 1, today=__import__('datetime').date(2026, 10, 1))
            assert est['est_mileage'] == 23000 + round(3000/92 * 30)
            assert main.effective_mileage(c, row) == main.mileage_estimate(c, 1)['est_mileage']
            status = main.reminder_status(est['est_mileage'], {'miles_interval':3400,'months_interval':None,'last_mileage':20000,'last_date':'2026-06-01','due_date':None,'repeats_yearly':False}, today=__import__('datetime').date(2026, 10, 1))
            assert status['state'] == 'overdue'
            fresh = main.reminder_status(20000, {'miles_interval':3400,'months_interval':None,'last_mileage':20000,'last_date':'2026-06-01','due_date':None,'repeats_yearly':False}, today=__import__('datetime').date(2026, 10, 1))
            assert fresh['state'] == 'ok'


def test_vehicle_photos(tmp_path):
    main.DB_PATH = tmp_path / 'photos.db'
    main.RECEIPTS_DIR = tmp_path / 'receipts'
    main.RECEIPTS_DIR.mkdir(exist_ok=True)
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup', json={'username':'admin-test','password':'password-123'})
        photo = admin.post('/api/vehicles/1/photo', files={'file':('car.png', PNG_BYTES, 'image/png')})
        assert photo.status_code == 201
        vehicle = admin.get('/api/vehicles').json()[0]
        assert vehicle['photo_receipt_id'] == photo.json()['id']
        assert vehicle['photo_url'] == f"/api/receipts/{photo.json()['id']}"
        assert admin.get(vehicle['photo_url']).content == PNG_BYTES
        assert admin.put('/api/settings', json={'use_vehicle_photos':True}).json()['use_vehicle_photos'] is True
        replacement = admin.post('/api/vehicles/1/photo', files={'file':('car2.png', PNG_BYTES, 'image/png')}).json()
        assert admin.get(f"/api/receipts/{photo.json()['id']}").status_code == 404
        assert admin.get('/api/vehicles').json()[0]['photo_receipt_id'] == replacement['id']
        assert admin.delete(f"/api/receipts/{replacement['id']}").status_code == 200
        assert admin.get('/api/vehicles').json()[0]['photo_receipt_id'] is None
        assert admin.post('/api/vehicles/1/photo', files={'file':('x.txt', b'no', 'text/plain')}).status_code == 400


def test_notification_settings_and_test_button(tmp_path, monkeypatch):
    main.DB_PATH = tmp_path / 'notify.db'
    main._login_failures.clear()
    main.init_db()
    sent = []
    monkeypatch.setattr(main, 'send_notification', lambda urls, title, body: (sent.append((urls, title, body)) or (True, '')))
    with TestClient(main.app) as admin:
        admin.post('/api/setup', json={'username':'admin-test','password':'password-123'})
        assert admin.get('/api/notifications').json() == {'apprise_urls':''}
        assert admin.put('/api/notifications', json={'apprise_urls':'not-a-url'}).status_code == 400
        assert admin.put('/api/notifications', json={'apprise_urls':'tgram://token/chat'}).json() == {'apprise_urls':'tgram://token/chat'}
        assert admin.post('/api/notifications/test').status_code == 200
        assert sent and sent[0][0] == 'tgram://token/chat'
        admin.post('/api/users', json={'username':'member-test','password':'password-456','is_admin':False})
    with TestClient(main.app) as member:
        member.post('/api/login', json={'username':'member-test','password':'password-456'})
        assert member.get('/api/notifications').status_code == 403
        assert member.put('/api/notifications', json={'apprise_urls':'tgram://x/y'}).status_code == 403
        assert member.post('/api/notifications/test').status_code == 403


def test_daily_check_notifies_only_newly_due(tmp_path, monkeypatch):
    main.DB_PATH = tmp_path / 'due.db'
    main._login_failures.clear()
    main.init_db()
    sent = []
    monkeypatch.setattr(main, 'send_notification', lambda urls, title, body: (sent.append(body) or (True, '')))
    with TestClient(main.app) as admin:
        admin.post('/api/setup', json={'username':'admin-test','password':'password-123'})
        admin.put('/api/notifications', json={'apprise_urls':'json://example.invalid/hook'})
        admin.post('/api/services', json={'vehicle_id':1,'date':'2026-09-01','mileage':20000,'type':'Tires'})
        admin.post('/api/reminders', json={'vehicle_id':1,'name':'Oil change','miles_interval':5000,'months_interval':None,'last_date':'2026-09-01','last_mileage':20000})
        assert main.run_notification_check() is False  # 0% used: nothing due
        admin.post('/api/fuel', json={'vehicle_id':1,'date':'2026-09-20','odometer':24500,'gallons':10,'cost':35})
        assert main.run_notification_check() is True  # 90% used: newly due soon
        assert len(sent) == 1 and 'Oil change' in sent[0] and 'Due soon' in sent[0]
        assert main.run_notification_check() is False  # same state: no repeat
        admin.post('/api/fuel', json={'vehicle_id':1,'date':'2026-09-21','odometer':25100,'gallons':10,'cost':35})
        assert main.run_notification_check() is True  # escalated to overdue
        assert len(sent) == 2 and 'OVERDUE' in sent[1]
        # fixing the item clears state, so a later due item notifies again
        admin.put('/api/reminders/1', json={'vehicle_id':1,'name':'Oil change','miles_interval':5000,'months_interval':None,'last_date':'2026-09-21','last_mileage':25100})
        assert main.run_notification_check() is False
        admin.post('/api/fuel', json={'vehicle_id':1,'date':'2026-09-22','odometer':29650,'gallons':10,'cost':35})
        assert main.run_notification_check() is True and len(sent) == 3


def test_webhook_fallback_posts_json(tmp_path):
    import http.server, threading as th
    received = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(200); self.end_headers()
        def log_message(self, *args): pass
    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    th.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ok, detail = main.send_notification(f'http://127.0.0.1:{server.server_port}/hook', 'T', 'B')
        assert ok, detail
        assert received and __import__('json').loads(received[0]) == {'title':'T','body':'B'}
    finally:
        server.shutdown()


def test_vehicle_notes_and_hide_setting(tmp_path):
    main.DB_PATH = tmp_path / 'notes.db'
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup', json={'username':'admin-test','password':'password-123'})
        note = admin.post('/api/notes', json={'vehicle_id':1,'date':'2026-09-21','body':'Tire pressure is 32 psi'}).json()
        assert note['body'] == 'Tire pressure is 32 psi' and note['logged_by'] == 'admin-test'
        updated = admin.put(f"/api/notes/{note['id']}", json={'vehicle_id':1,'date':'2026-09-21','body':'Tire pressure is 35 psi'}).json()
        assert updated['body'] == 'Tire pressure is 35 psi'
        rows = admin.get('/api/notes?vehicle_id=1').json()
        assert len(rows) == 1 and rows[0]['body'] == 'Tire pressure is 35 psi'
        settings = admin.put('/api/settings', json={'hide_notes':True}).json()
        assert settings['hide_notes'] is True
        token = admin.post('/api/tokens', json={'name':'notes-script'}).json()['token']
        auth = {'Authorization': f'Bearer {token}'}
        v1_note = admin.post('/api/v1/vehicles/1/notes', json={'date':'2026-09-21','body':'Check the spare'}, headers=auth)
        assert v1_note.status_code == 201
        v1_rows = admin.get('/api/v1/vehicles/1/notes', headers=auth).json()
        assert [n['body'] for n in v1_rows] == ['Check the spare', 'Tire pressure is 35 psi']
        assert admin.get('/api/v1/vehicles/1/notes').status_code == 401
        assert admin.delete(f"/api/notes/{note['id']}").json() == {'ok': True}
        assert admin.delete(f"/api/notes/{note['id']}").status_code == 404


def test_v1_mileage_update(tmp_path):
    main.DB_PATH = tmp_path / 'mileage.db'
    main._login_failures.clear()
    main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup', json={'username':'admin-test','password':'password-123'})
        token = admin.post('/api/tokens', json={'name':'mileage-script'}).json()['token']
        auth = {'Authorization': f'Bearer {token}'}
        assert admin.put('/api/v1/vehicles/1/mileage', json={'mileage':25000,'date':'2026-09-21'}).status_code == 401
        updated = admin.put('/api/v1/vehicles/1/mileage', json={'mileage':25000,'date':'2026-09-21'}, headers=auth).json()
        assert updated['mileage'] == 25000
        vehicle = admin.get('/api/v1/vehicles', headers=auth).json()[0]
        assert vehicle['mileage'] == 25000
        assert admin.put('/api/v1/vehicles/1/mileage', json={'mileage':-5}, headers=auth).status_code == 422
        assert admin.put('/api/v1/vehicles/99/mileage', json={'mileage':100}, headers=auth).status_code == 404


def test_disabled_users_and_private_vehicle_visibility(tmp_path):
    main.DB_PATH=tmp_path/'privacy.db'; main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup',json={'username':'admin','password':'password-123'})
        member=admin.post('/api/users',json={'username':'member','password':'password-456'}).json()
        other=admin.post('/api/users',json={'username':'other','password':'password-789'}).json()
        shared=admin.post('/api/vehicles',json={'name':'Shared','year':'2024','mileage':1,'icon':'🚗'}).json()
        private=admin.post('/api/vehicles',json={'name':'Member private','year':'2025','mileage':2,'icon':'🚙','owner_id':member['id'],'private':True}).json()
        hidden=admin.post('/api/vehicles',json={'name':'Other private','year':'2026','mileage':3,'icon':'🛻','owner_id':other['id'],'private':True}).json()
        admin.put('/api/me/vehicle-view',json={'show_all':True})
        for vid in (shared['id'],private['id'],hidden['id']):
            assert admin.post('/api/services',json={'vehicle_id':vid,'date':'2026-01-01','mileage':1,'type':'Check'}).status_code==200
        assert {v['name'] for v in admin.get('/api/vehicles').json()} >= {'Shared','Member private','Other private'}
        assert admin.put(f"/api/users/{member['id']}",json={'active':False}).status_code==200
        assert admin.put(f"/api/users/{admin.get('/api/me').json()['id']}",json={'active':False}).status_code==400
    with TestClient(main.app) as disabled:
        assert disabled.post('/api/login',json={'username':'member','password':'password-456'}).status_code==401
    with TestClient(main.app) as admin:
        admin.post('/api/login',json={'username':'admin','password':'password-123'})
        assert admin.put(f"/api/users/{member['id']}",json={'active':True}).status_code==200
    with TestClient(main.app) as member_client:
        assert member_client.post('/api/login',json={'username':'member','password':'password-456'}).status_code==200
        names={v['name'] for v in member_client.get('/api/vehicles').json()}
        assert 'Shared' in names and 'Member private' in names and 'Other private' not in names
        services=member_client.get('/api/services').json()
        assert {s['vehicle_id'] for s in services}=={1,shared['id'],private['id']}
        assert member_client.post('/api/fuel',json={'vehicle_id':hidden['id'],'date':'2026-01-01','odometer':5,'gallons':1,'cost':3}).status_code==404
        assert member_client.post('/api/notes',json={'vehicle_id':hidden['id'],'date':'2026-01-01','body':'no'}).status_code==404
        assert member_client.post('/api/mods',json={'vehicle_id':hidden['id'],'name':'no','price':1}).status_code==404
        assert member_client.put(f"/api/vehicles/{hidden['id']}",json={'name':'No','year':'2026','mileage':3,'icon':'🛻'}).status_code==404


def test_admin_vehicle_view_defaults_filtered_and_persists(tmp_path):
    main.DB_PATH=tmp_path/'admin-view.db'; main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup',json={'username':'admin','password':'password-123'})
        friend=admin.post('/api/users',json={'username':'friend','password':'password-456'}).json()
        private=admin.post('/api/vehicles',json={'name':'Friend private','owner_id':friend['id'],'private':True}).json()
        assert 'Friend private' not in {v['name'] for v in admin.get('/api/vehicles').json()}
        enabled=admin.put('/api/me/vehicle-view',json={'show_all':True})
        assert enabled.status_code==200 and enabled.json()['show_all_vehicles'] is True
        assert 'Friend private' in {v['name'] for v in admin.get('/api/vehicles').json()}
    with TestClient(main.app) as admin_again:
        admin_again.post('/api/login',json={'username':'admin','password':'password-123'})
        assert admin_again.get('/api/me').json()['show_all_vehicles'] is True
        admin_again.put('/api/me/vehicle-view',json={'show_all':False})
        assert 'Friend private' not in {v['name'] for v in admin_again.get('/api/vehicles').json()}
    with TestClient(main.app) as friend_client:
        friend_client.post('/api/login',json={'username':'friend','password':'password-456'})
        assert friend_client.put('/api/me/vehicle-view',json={'show_all':True}).status_code==403
        assert 'Friend private' in {v['name'] for v in friend_client.get('/api/vehicles').json()}


def test_vehicle_specs_and_mod_install_notes(tmp_path):
    main.DB_PATH=tmp_path/'specs.db'; main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup',json={'username':'admin','password':'password-123'})
        vehicle=admin.post('/api/vehicles',json={'name':'Miata','fuel_type':'Premium gasoline','tire_size':'205/45R17','oil_spec':'5W-30, 4.8 qt'}).json()
        assert vehicle['fuel_type']=='Premium gasoline' and vehicle['tire_size']=='205/45R17' and vehicle['oil_spec']=='5W-30, 4.8 qt'
        vehicle=admin.put(f"/api/vehicles/{vehicle['id']}",json={**vehicle,'oil_spec':''}).json()
        assert vehicle['oil_spec']==''
        mod=admin.post('/api/mods',json={'vehicle_id':vehicle['id'],'name':'Coilovers','price':900,'torque_specs':'Top nuts 30 lb-ft','fluids':'Anti-seize','gotchas':'Support the hub','youtube_url':'https://youtube.com/watch?v=test'}).json()
        assert mod['torque_specs']=='Top nuts 30 lb-ft' and mod['youtube_url'].startswith('https://youtube.com/')
        mod=admin.put(f"/api/mods/{mod['id']}",json={**mod,'fluids':''}).json()
        assert mod['fluids']=='' and mod['gotchas']=='Support the hub'


def test_general_due_date_reminders_and_fuel_octane(tmp_path):
    main.DB_PATH=tmp_path/'renewals.db'; main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup',json={'username':'admin','password':'password-123'})
        inspection=admin.post('/api/reminders',json={'vehicle_id':1,'name':'State inspection','due_date':'2026-10-01','last_date':'2026-01-01','last_mileage':0}).json()
        registration=admin.post('/api/reminders',json={'vehicle_id':1,'name':'Tag renewal','due_date':'2026-11-15','last_date':'2026-01-01','last_mileage':0}).json()
        assert inspection['due_date']=='2026-10-01' and registration['name']=='Tag renewal'
        assert admin.put(f"/api/reminders/{inspection['id']}",json={**inspection,'due_date':'2026-10-15'}).json()['due_date']=='2026-10-15'
        fuel=admin.post('/api/fuel',json={'vehicle_id':1,'date':'2026-09-21','odometer':100,'gallons':5,'cost':20,'octane':'93'}).json()
        assert fuel['octane']=='93'
        fuel=admin.put(f"/api/fuel/{fuel['id']}",json={**fuel,'octane':'E30'}).json()
        assert fuel['octane']=='E30'
        unset=admin.post('/api/fuel',json={'vehicle_id':1,'date':'2026-09-22','odometer':200,'gallons':5,'cost':20}).json()
        assert unset['octane']==''


def test_yearly_and_service_reminder_shapes(tmp_path):
    main.DB_PATH=tmp_path/'reminder-types.db'; main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup',json={'username':'admin','password':'password-123'})
        vehicle=admin.post('/api/vehicles',json={'name':'2020 Test Car','year':'2020','mileage':1000}).json()
        renewal=admin.post('/api/reminders',json={'vehicle_id':vehicle['id'],'name':'Registration','due_date':'2025-03-15','repeats_yearly':True,'last_date':'2025-01-01','last_mileage':0}).json()
        assert renewal['due_date']=='2025-03-15' and renewal['repeats_yearly'] is True
        service=admin.post('/api/reminders',json={'vehicle_id':vehicle['id'],'name':'Oil','miles_interval':5000,'months_interval':6,'due_date':None,'repeats_yearly':False,'last_date':'2026-01-01','last_mileage':1000}).json()
        assert service['miles_interval']==5000 and service['due_date'] is None
        changed=admin.put(f"/api/reminders/{renewal['id']}",json={**renewal,'repeats_yearly':False}).json()
        assert changed['repeats_yearly'] is False

def test_private_vehicle_receipts_are_not_exposed(tmp_path):
    main.DB_PATH=tmp_path/'receipt-auth.db'; main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup',json={'username':'admin','password':'password-123'})
        private=admin.post('/api/vehicles',json={'name':'2020 Private Car','year':'2020','mileage':1,'private':True}).json()
        fuel=admin.post('/api/fuel',json={'vehicle_id':private['id'],'date':'2026-09-21','odometer':2,'gallons':1,'cost':1}).json()
        admin.post('/api/users',json={'username':'member','password':'password-123','is_admin':False})
        upload=admin.post('/api/receipts',data={'kind':'fuel','entry_id':fuel['id']},files={'file':('receipt.png',b'png','image/png')})
        receipt_id=upload.json()['id']
    with TestClient(main.app) as member:
        member.post('/api/login',json={'username':'member','password':'password-123'})
        assert member.get('/api/receipts').json()==[]
        assert member.get(f'/api/receipts/{receipt_id}').status_code==404
        assert member.delete(f'/api/receipts/{receipt_id}').status_code==404
        denied=member.post('/api/receipts',data={'kind':'fuel','entry_id':fuel['id']},files={'file':('receipt.png',b'png','image/png')})
        assert denied.status_code==404


def test_service_install_notes_round_trip(tmp_path):
    main.DB_PATH=tmp_path/'service-notes.db'; main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup',json={'username':'admin','password':'password-123'})
        vehicle=admin.post('/api/vehicles',json={'name':'2020 Test Car','year':'2020','mileage':1000}).json()
        service=admin.post('/api/services',json={'vehicle_id':vehicle['id'],'date':'2026-09-21','mileage':1100,'type':'Oil change','cost':40,'provider':'DIY','notes':'Done','torque_specs':'29 lb-ft','fluids':'5W-30, 5 qt','gotchas':'Replace washer','youtube_url':'https://youtube.com/watch?v=test'}).json()
        assert service['torque_specs']=='29 lb-ft'
        assert service['fluids']=='5W-30, 5 qt'
        service=admin.put(f"/api/services/{service['id']}",json={**service,'gotchas':''}).json()
        assert service['gotchas']=='' and service['youtube_url'].startswith('https://youtube.com/')


def test_private_entry_cannot_be_moved_by_another_member(tmp_path):
    main.DB_PATH=tmp_path/'entry-move-auth.db'; main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup',json={'username':'admin','password':'password-123'})
        private=admin.post('/api/vehicles',json={'name':'2020 Private Car','year':'2020','mileage':1,'private':True}).json()
        service=admin.post('/api/services',json={'vehicle_id':private['id'],'date':'2026-09-21','mileage':2,'type':'Secret','cost':1}).json()
        admin.post('/api/users',json={'username':'member','password':'password-123','is_admin':False})
        public=admin.post('/api/vehicles',json={'name':'2021 Public Car','year':'2021','mileage':1,'private':False}).json()
    with TestClient(main.app) as member:
        member.post('/api/login',json={'username':'member','password':'password-123'})
        moved={**service,'vehicle_id':public['id']}
        assert member.put(f"/api/services/{service['id']}",json=moved).status_code==404


def test_v1_notes_endpoint_returns_notes(tmp_path):
    main.DB_PATH=tmp_path/'v1-notes.db'; main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup',json={'username':'admin','password':'password-123'})
        vehicle=admin.post('/api/vehicles',json={'year':2020,'make':'Test','model':'Car','mileage':1}).json()
        admin.post('/api/notes',json={'vehicle_id':vehicle['id'],'date':'2026-09-22','body':'hello'})
        token=admin.post('/api/tokens',json={'name':'test'}).json()['token']
    response=TestClient(main.app).get(f"/api/v1/vehicles/{vehicle['id']}/notes",headers={'Authorization':f'Bearer {token}'})
    assert response.status_code==200 and response.json()[0]['body']=='hello'


def test_backup_is_admin_only_and_round_trips_current_fields(tmp_path):
    main.DB_PATH=tmp_path/'backup-v3.db'; main.RECEIPTS_DIR=tmp_path/'receipts'; main.RECEIPTS_DIR.mkdir(); main.init_db()
    with TestClient(main.app) as admin:
        admin.post('/api/setup',json={'username':'admin','password':'password-123'})
        vehicle=admin.post('/api/vehicles',json={'name':'2024 Backup Car','year':'2024','mileage':10,'private':True,'fuel_type':'Premium','tire_size':'225/45R17','oil_spec':'5W-30'}).json()
        service=admin.post('/api/services',json={'vehicle_id':vehicle['id'],'date':'2026-09-22','mileage':11,'type':'Oil','cost':40,'torque_specs':'29 lb-ft','fluids':'5 qt','gotchas':'washer','youtube_url':'https://youtube.com/watch?v=x'}).json()
        admin.post('/api/reminders',json={'vehicle_id':vehicle['id'],'name':'Tag','due_date':'2026-10-01','repeats_yearly':True,'last_date':'2026-01-01','last_mileage':0})
        admin.post('/api/notes',json={'vehicle_id':vehicle['id'],'date':'2026-09-22','body':'note'})
        admin.post('/api/mods',json={'vehicle_id':vehicle['id'],'name':'Mod','price':1,'torque_specs':'10','gotchas':'careful','youtube_url':'https://youtube.com/watch?v=y'})
        admin.post('/api/fuel',json={'vehicle_id':vehicle['id'],'date':'2026-09-22','odometer':12,'gallons':1,'cost':4,'octane':'93'})
        assert admin.post('/api/receipts',data={'kind':'service','entry_id':service['id']},files={'file':('r.png',b'png','image/png')}).status_code==201
        admin.post('/api/users',json={'username':'member','password':'password-123','is_admin':False})
        backup=admin.get('/api/export').json()
        assert backup['version']==3 and backup['tables']['notes'] and backup['tables']['modifications']
        assert backup['tables']['reminders'][0]['repeats_yearly']==1 and backup['tables']['fuel_entries'][0]['octane']=='93'
        assert backup['tables']['services'][0]['fluids']=='5 qt' and backup['receipt_files']
        restored=admin.post('/api/import',json=backup)
        assert restored.status_code==200 and restored.json()['version']==3
        again=admin.get('/api/export').json()
        assert again['tables']['services'][0]['fluids']=='5 qt' and again['receipt_files']==backup['receipt_files']
    with TestClient(main.app) as member:
        member.post('/api/login',json={'username':'member','password':'password-123'})
        assert member.get('/api/export').status_code==403
        assert member.post('/api/import',json=backup).status_code==403
