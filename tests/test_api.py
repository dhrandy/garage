import os
from pathlib import Path
os.environ['GARAGE_DATA_DIR']='/tmp/garage-pytest-data'
from fastapi.testclient import TestClient
from app import main

def test_shared_garage_and_permissions(tmp_path):
    main.DB_PATH=tmp_path/'garage.db'; main.init_db()
    with TestClient(main.app) as admin:
        assert admin.post('/api/setup',json={'username':'admin-test','password':'password-123'}).status_code==200
        assert len(admin.get('/api/vehicles').json())==4
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
