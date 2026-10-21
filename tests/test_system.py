import concurrent.futures
import os
import tempfile
import unittest
from pathlib import Path
from app import create_app
from iot.db import DB
from iot.domain import ingest, analyze, forecast, transition, DomainError
from iot.simulator import tick
from database_safety import validate_test_database


class SystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.url=os.environ.get('TEST_DATABASE_URL') or 'sqlite:///'+str(Path(self.temp.name)/f'{self.id()}.sqlite3')
        # Explicit TEST_DATABASE_URL must point to an expendable, empty test database.
        if os.environ.get('TEST_DATABASE_URL'):
            validate_test_database(self.url)
            with DB(self.url) as db:
                db.execute('DROP SCHEMA public CASCADE')
                db.execute('CREATE SCHEMA public')
        self.app=create_app(self.url)
        self.app.testing=True
        self.clients={}
        self.csrf={}
        for role in ('admin','operator','engineer','manager'):
            c=self.app.test_client()
            r=c.post('/api/login',json={'username':role,'password':os.environ.get('DEMO_PASSWORD','Demo-IoT-2026!')})
            self.assertEqual(r.status_code,200)
            self.clients[role]=c
            self.csrf[role]=r.json['csrf']
        with DB(self.url) as db:
            self.clock=db.one('SELECT clock FROM simulation')['clock']
            self.sensor=db.one("SELECT s.* FROM sensors s JOIN equipment e ON e.id=s.equipment_id WHERE e.name='R-01' AND s.metric='temperature'")
            self.users={u['role']:u for u in db.all('SELECT * FROM users')}

    def post(self,role,path,data):
        return self.clients[role].post('/api/'+path,json=data,headers={'X-CSRF-Token':self.csrf[role]})

    def make_alarm(self):
        with DB(self.url) as db:
            db.lock()
            ingest(db,dict(sensor_id=self.sensor['id'],value=38,measured_at=self.clock+1,packet_id='alarm'),self.clock+1)
            db.execute('UPDATE simulation SET clock=?',(self.clock+1,))
            analyze(db,self.clock+1)
            return db.one("SELECT * FROM incidents WHERE problem='threshold'")

    def test_auth_and_roles(self):
        self.assertEqual(self.app.test_client().get('/api/state').status_code,401)
        for role in ('operator','engineer','manager'):
            self.assertEqual(self.post(role,'settings',dict(analysis_window=1800,horizon=900,recovery_seconds=120)).status_code,403)
            self.assertEqual(self.post(role,'equipment',dict(name='X',kind='rack',location='A',active=1)).status_code,403)
        self.assertEqual(self.post('admin','settings',dict(analysis_window=1800,horizon=900,recovery_seconds=120)).status_code,200)
        self.assertEqual(self.clients['admin'].post('/api/settings',json={}).status_code,403)
        for role in ('engineer','manager'):
            self.assertEqual(self.post(role,'simulation',dict(scenario='normal',running=1,speed=1)).status_code,403)

    def test_validation_dedup_and_out_of_order(self):
        p=dict(sensor_id=self.sensor['id'],value=26,measured_at=self.clock+1,packet_id='unique')
        with DB(self.url) as db:
            db.lock()
            self.assertTrue(ingest(db,p,self.clock+1))
            self.assertFalse(ingest(db,p,self.clock+1))
            for changes in ({'value':float('nan')},{'value':121},{'value':True},{'measured_at':self.clock+999},{'packet_id':''},{'sensor_id':'missing'}):
                with self.assertRaises(DomainError):ingest(db,{**p,**changes},self.clock+1)
            with self.assertRaises(DomainError):ingest(db,{**p,'value':27},self.clock+1)
            self.assertTrue(ingest(db,{**p,'packet_id':'old','measured_at':self.clock-15,'value':39},self.clock+1))
            analyze(db,self.clock+1)
            self.assertIsNone(db.one("SELECT id FROM incidents WHERE problem='threshold'"))

    def test_threshold_stale_and_unique_concurrency(self):
        incident=self.make_alarm()
        self.assertIsNotNone(incident)
        def run(_):
            with DB(self.url) as db:
                db.lock();analyze(db,self.clock+1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(run,range(8)))
        with DB(self.url) as db:
            self.assertEqual(len(db.all("SELECT * FROM incidents WHERE problem='threshold'")),1)
            db.lock();analyze(db,self.clock+200)
            self.assertIsNotNone(db.one("SELECT * FROM incidents WHERE sensor_id=? AND problem='stale'",(self.sensor['id'],)))
            self.assertIsNone(db.one('SELECT normal_since FROM incidents WHERE id=?',(incident['id'],))['normal_since'])

    def test_forecast_examples(self):
        now=10000
        stable=[dict(measured_at=now-i*30,value=25) for i in range(61)]
        f=forecast(stable,now,1800,900,180,32)
        self.assertEqual(f['available'],1);self.assertAlmostEqual(f['value'],25);self.assertIsNone(f['threshold_at'])
        rising=[dict(measured_at=now-i*30,value=28-i*.2) for i in range(61)]
        f=forecast(rising,now,1800,900,180,32)
        self.assertAlmostEqual(f['value'],34);self.assertAlmostEqual(f['threshold_at'],now+600)
        self.assertFalse(forecast(stable[:3],now,1800,900,180,32)['available'])
        self.assertFalse(forecast(stable,now+300,1800,900,180,32)['available'])
        self.assertFalse(forecast(stable[:10]+stable[40:],now,1800,900,180,32)['available'])

    def test_lifecycle_and_recovery(self):
        i=self.make_alarm();path=f'incidents/{i["id"]}/action'
        self.assertEqual(self.post('engineer',path,dict(action='acknowledge',comment='Спроба')).status_code,403)
        self.assertEqual(self.post('operator',path,dict(action='close',comment='Спроба')).status_code,409)
        self.assertEqual(self.post('operator',path,dict(action='acknowledge',comment='Прийнято')).status_code,200)
        self.assertEqual(self.post('engineer',path,dict(action='start',comment='Починаю')).status_code,403)
        self.assertEqual(self.post('operator',path,dict(action='assign',assignee_id=self.users['engineer']['id'],comment='Призначено')).status_code,200)
        self.assertEqual(self.post('engineer',path,dict(action='start',comment='Починаю')).status_code,200)
        self.assertEqual(self.post('engineer',path,dict(action='submit',comment='Очищено вентилятор')).status_code,200)
        # analyze automatically returns review to working while alarm persists.
        self.assertEqual(self.post('operator',path,dict(action='close',comment='Перевіряю')).status_code,409)
        with DB(self.url) as db:
            db.lock();analyze(db,self.clock+1)
            self.assertEqual(db.one('SELECT status FROM incidents WHERE id=?',(i['id'],))['status'],'working')
        with DB(self.url) as db:
            db.lock()
            for n in range(1,7):
                at=self.clock+1+n*30
                ingest(db,dict(sensor_id=self.sensor['id'],value=25,measured_at=at,packet_id=f'recovery{n}'),at)
                analyze(db,at)
            db.execute('UPDATE simulation SET clock=?',(at,))
        self.assertEqual(self.post('engineer',path,dict(action='submit',comment='Охолодження відновлено')).status_code,200)
        self.assertEqual(self.post('operator',path,dict(action='close',comment='Норму підтверджено')).status_code,200)
        with DB(self.url) as db:
            db.lock();ingest(db,dict(sensor_id=self.sensor['id'],value=38,measured_at=at+1,packet_id='alarm2'),at+1);analyze(db,at+1)
            self.assertEqual(len(db.all("SELECT * FROM incidents WHERE sensor_id=? AND problem='threshold'",(self.sensor['id'],))),2)
            self.assertEqual(len(db.all('SELECT * FROM work_records WHERE incident_id=?',(i['id'],))),2)

    def test_persistence_and_archive(self):
        i=self.make_alarm()
        app2=create_app(self.url)
        with DB(self.url) as db:
            self.assertIsNotNone(db.one('SELECT id FROM incidents WHERE id=?',(i['id'],)))
            count=db.one('SELECT COUNT(*) AS n FROM measurements')['n']
        response=app2.test_client().get('/')
        self.assertTrue(len(response.data)>100)
        response.close()
        e=self.clients['admin'].get('/api/state').json['equipment'][0]
        self.assertEqual(self.post('admin','equipment',{**e,'active':0}).status_code,200)
        with DB(self.url) as db:self.assertEqual(db.one('SELECT COUNT(*) AS n FROM measurements')['n'],count)

    def test_simulator_lease_pause_and_disconnect(self):
        with DB(self.url) as db:
            db.lock();self.assertFalse(tick(db,'a',100));db.execute("UPDATE simulation SET running=1,scenario='disconnect',speed=30")
            self.assertFalse(tick(db,'b',101))
            for n in range(4):self.assertTrue(tick(db,'a',102+n*2))
            s=db.one('SELECT clock FROM simulation')['clock']
            latest=db.one('SELECT MAX(measured_at) AS t FROM measurements WHERE sensor_id=?',(self.sensor['id'],))['t']
            self.assertEqual(latest,self.clock)
            self.assertEqual(s,self.clock+240)
            self.assertIsNotNone(db.one("SELECT id FROM incidents WHERE sensor_id=? AND problem='stale'",(self.sensor['id'],)))

    def test_settings_users_exports_reset(self):
        self.assertEqual(self.post('admin','settings',dict(analysis_window=1,horizon=0,recovery_seconds=0)).status_code,400)
        self.assertEqual(self.post('admin','users',dict(name='Тест',username='test',role='engineer',password='Testing-123456!',active=1)).status_code,200)
        self.assertEqual(self.post('manager','users',{}).status_code,403)
        i=self.make_alarm()
        self.assertEqual(self.clients['engineer'].get('/api/incidents/'+i['id']).status_code,404)
        csv=self.clients['operator'].get('/api/export/incidents?status=new')
        self.assertEqual(csv.status_code,200);self.assertIn('text/csv',csv.content_type)
        self.assertEqual(self.post('admin','demo/reset',dict(confirmation='no')).status_code,400)
        self.assertEqual(self.post('admin','demo/reset',dict(confirmation='СКИНУТИ ДЕМО')).status_code,200)
        data=self.clients['admin'].get('/api/state').json
        self.assertEqual(len(data['equipment']),8);self.assertEqual(len(data['incidents']),0)
        self.assertTrue(any(u['username']=='test' for u in data['users']))

    def test_early_closure_and_stale_closure_rejected(self):
        i=self.make_alarm()
        with DB(self.url) as db:
            db.lock()
            db.execute("UPDATE incidents SET status='review',assignee_id=? WHERE id=?",(self.users['engineer']['id'],i['id']))
            now=self.clock+31
            ingest(db,dict(sensor_id=self.sensor['id'],value=25,measured_at=now,packet_id='repaired'),now)
            analyze(db,now)
            with self.assertRaises(DomainError):transition(db,i['id'],self.users['operator'],'close','Перевірка',now)
            # Even a previously recovered incident cannot close on old data.
            db.execute('UPDATE incidents SET normal_since=? WHERE id=?',(now-500,i['id']))
            with self.assertRaises(DomainError):transition(db,i['id'],self.users['operator'],'close','Перевірка',now+181)
            db.execute('UPDATE sensors SET active=0 WHERE id=?',(self.sensor['id'],))
            with self.assertRaises(DomainError):transition(db,i['id'],self.users['operator'],'close','Перевірка',now)

    def test_forecast_risk_is_distinct_from_threshold(self):
        with DB(self.url) as db:
            db.lock()
            db.execute('DELETE FROM forecasts WHERE sensor_id=?',(self.sensor['id'],))
            db.execute('DELETE FROM measurements WHERE sensor_id=?',(self.sensor['id'],))
            for n in range(61):
                at=self.clock-(60-n)*30
                ingest(db,dict(sensor_id=self.sensor['id'],value=16+n*.2,measured_at=at,packet_id=f'rise-{n}'),self.clock)
            analyze(db,self.clock)
            self.assertIsNotNone(db.one("SELECT id FROM incidents WHERE sensor_id=? AND problem='risk'",(self.sensor['id'],)))
            self.assertIsNone(db.one("SELECT id FROM incidents WHERE sensor_id=? AND problem='threshold'",(self.sensor['id'],)))
            analyze(db,self.clock)
            self.assertEqual(len(db.all('SELECT id FROM forecasts WHERE sensor_id=? AND created_at=?',(self.sensor['id'],self.clock))),1)

    def test_sensor_crud_and_simulated_power(self):
        e=self.clients['admin'].get('/api/state').json['equipment'][0]
        data=dict(equipment_id=e['id'],name='Додатковий датчик',metric='temperature',active=1,lower_bound=18,upper_bound=32,stale_seconds=180)
        self.assertEqual(self.post('admin','sensors',data).status_code,200)
        self.assertEqual(self.post('operator','sensors',data).status_code,403)
        data={**self.sensor,'lower_bound':18,'upper_bound':33,'stale_seconds':180}
        self.assertEqual(self.post('admin','sensors',data).status_code,200)
        self.assertEqual(self.post('admin','sensors',{**data,'metric':'humidity'}).status_code,400)
        with DB(self.url) as db:
            db.lock();db.execute("UPDATE simulation SET scenario='power',running=1,speed=30")
            for n in range(12):tick(db,'power-test',100+n*2)
            charge=db.one("SELECT s.id FROM sensors s JOIN equipment e ON s.equipment_id=e.id WHERE e.name='UPS-01' AND s.metric='charge'")
            self.assertIsNotNone(db.one("SELECT id FROM incidents WHERE sensor_id=? AND problem='threshold'",(charge['id'],)))


if __name__=='__main__':unittest.main()
