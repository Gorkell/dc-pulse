import unittest
import test_system as original
from iot.db import DB
from iot.domain import analyze, ingest
from iot.simulator import tick


class RegressionTests(unittest.TestCase):
    setUpClass=classmethod(original.SystemTests.setUpClass.__func__)
    tearDownClass=classmethod(original.SystemTests.tearDownClass.__func__)
    setUp=original.SystemTests.setUp
    post=original.SystemTests.post

    def state(self):
        return self.clients['admin'].get('/api/state').json

    def current(self):
        return next(f for f in self.clients['admin'].get('/api/forecasts').json['items'] if f['sensor_id']==self.sensor['id'])

    def sensor_save(self,active):
        s=next(s for s in self.state()['sensors'] if s['id']==self.sensor['id'])
        self.assertEqual(self.post('admin','sensors',{**s,'active':active}).status_code,200)

    def equipment_save(self,active):
        e=next(e for e in self.state()['equipment'] if e['id']==self.sensor['equipment_id'])
        self.assertEqual(self.post('admin','equipment',{**e,'active':active}).status_code,200)

    def restore_history(self):
        # 31 points cover half the 1800-second window, entirely after reactivation.
        for n in range(1,32):
            now=self.clock+n*30
            with DB(self.url) as db:
                db.lock()
                ingest(db,dict(sensor_id=self.sensor['id'],packet_id=f'fresh-{n}',value=25,measured_at=now),now)
                db.execute('UPDATE simulation SET clock=?',(now,))
                analyze(db,now)
            if n==6:
                self.assertFalse(self.current()['available'])
        self.assertTrue(self.current()['available'])

    def test_renamed_targets_drive_all_scenarios(self):
        targets=self.state()['targets']
        with DB(self.url) as db:
            db.lock()
            for sid,name in [(targets['temperature_sensor_id'],'Перейменована стійка'),(targets['power_sensor_id'],'Резервне живлення')]:
                db.execute('UPDATE equipment SET name=? WHERE id=(SELECT equipment_id FROM sensors WHERE id=?)',(name,sid))
            db.execute("UPDATE simulation SET running=1,speed=30,scenario='warming'")
            previous=db.one('SELECT value FROM measurements WHERE sensor_id=? ORDER BY measured_at DESC LIMIT 1',(self.sensor['id'],))['value']
            tick(db,'rename',100)
            value=db.one('SELECT value FROM measurements WHERE sensor_id=? ORDER BY measured_at DESC LIMIT 1',(self.sensor['id'],))['value']
            self.assertAlmostEqual(value-previous,.72,places=2)
            db.execute("UPDATE simulation SET scenario='overheat'")
            tick(db,'rename',102)
            self.assertIsNotNone(db.one("SELECT id FROM incidents WHERE sensor_id=? AND problem='threshold'",(self.sensor['id'],)))
            count=db.one('SELECT COUNT(*) AS n FROM measurements WHERE sensor_id=?',(self.sensor['id'],))['n']
            db.execute("UPDATE simulation SET scenario='disconnect'")
            tick(db,'rename',104)
            self.assertEqual(db.one('SELECT COUNT(*) AS n FROM measurements WHERE sensor_id=?',(self.sensor['id'],))['n'],count)
            db.execute("UPDATE simulation SET scenario='power'")
            for n in range(12):tick(db,'rename',106+n*2)
            self.assertIsNotNone(db.one("SELECT id FROM incidents WHERE sensor_id=? AND problem='threshold'",(targets['power_sensor_id'],)))
        self.assertEqual(self.state()['targets']['temperature_sensor_id'],targets['temperature_sensor_id'])
        self.assertEqual(self.state()['targets']['power_sensor_id'],targets['power_sensor_id'])

    def test_sensor_archive_history_and_reactivation(self):
        initial=self.current()
        self.assertTrue(initial['available'])
        with DB(self.url) as db:
            stored=db.one('SELECT * FROM forecasts WHERE id=?',(initial['id'],))
            count=db.one('SELECT COUNT(*) AS n FROM measurements')['n']
        self.sensor_save(0)
        f=self.current()
        self.assertFalse(f['available']);self.assertIsNone(f['value'])
        self.assertTrue(f['historical_available']);self.assertIn('датчик архівований',f['explanation'])
        self.assertEqual(next(s for s in self.state()['sensors'] if s['id']==self.sensor['id'])['state'],'inactive')
        self.assertTrue(self.state()['targets']['temperature_error'])
        self.sensor_save(1)
        self.assertFalse(self.current()['available'])
        with DB(self.url) as db:
            self.assertEqual(db.one('SELECT * FROM forecasts WHERE id=?',(initial['id'],)),stored)
            self.assertEqual(db.one('SELECT COUNT(*) AS n FROM measurements')['n'],count)
        self.restore_history()

    def test_equipment_archive_and_reactivation(self):
        original=self.current()
        self.equipment_save(0)
        data=self.state()
        sensors=[s for s in data['sensors'] if s['equipment_id']==self.sensor['equipment_id']]
        self.assertTrue(all(s['active']==1 and s['state']=='inactive' for s in sensors))
        self.assertIn('обладнання архівоване',self.current()['explanation'])
        self.assertFalse(self.current()['available'])
        self.equipment_save(1)
        self.assertFalse(self.current()['available'])
        self.assertIn('новий аналіз',self.current()['explanation'])
        with DB(self.url) as db:
            self.assertTrue(db.one('SELECT available FROM forecasts WHERE id=?',(original['id'],))['available'])
        self.restore_history()

    def test_archived_target_pauses_and_missing_target_is_safe(self):
        self.sensor_save(0)
        with DB(self.url) as db:
            db.lock();db.execute("UPDATE simulation SET scenario='overheat',running=1")
            self.assertFalse(tick(db,'archive',100))
            self.assertFalse(db.one('SELECT running FROM simulation')['running'])
        self.assertEqual(self.post('operator','simulation',dict(scenario='overheat',running=1,speed=1)).status_code,409)
        self.assertEqual(self.post('operator','simulation/targets',dict(temperature_sensor_id=None,power_sensor_id=None)).status_code,200)
        data=self.state()
        self.assertIsNone(data['targets']['temperature_sensor_id'])
        self.assertIn('відсутня',data['targets']['temperature_error'])

    def test_explicit_target_selection_and_permissions(self):
        data=self.state()
        second=next(s for s in data['sensors'] if s['metric']=='temperature' and s['id']!=self.sensor['id'] and next(e for e in data['equipment'] if e['id']==s['equipment_id'])['kind']=='rack')
        targets={k:data['targets'][k] for k in ('temperature_sensor_id','power_sensor_id')}
        targets['temperature_sensor_id']=second['id']
        self.assertEqual(self.post('manager','simulation/targets',targets).status_code,403)
        self.assertEqual(self.post('operator','simulation/targets',targets).status_code,200)
        self.assertEqual(self.state()['targets']['temperature_sensor_id'],second['id'])
        self.assertEqual(self.post('admin','simulation/targets',{**targets,'temperature_sensor_id':targets['power_sensor_id']}).status_code,400)
        self.assertEqual(self.post('admin','simulation/targets',{**targets,'temperature_sensor_id':'absent'}).status_code,400)

    def test_reset_restores_new_target_ids(self):
        before=self.state()['targets']
        self.assertEqual(self.post('admin','demo/reset',dict(confirmation='СКИНУТИ ДЕМО')).status_code,200)
        after=self.state()
        for field,metric in [('temperature_sensor_id','temperature'),('power_sensor_id','charge')]:
            sid=after['targets'][field]
            self.assertNotEqual(sid,before[field])
            self.assertTrue(any(s['id']==sid and s['metric']==metric for s in after['sensors']))
        self.assertIsNone(after['targets']['temperature_error'])
        self.assertIsNone(after['targets']['power_error'])
        self.assertEqual(next(u for u in after['users'] if u['username']=='operator')['name'],'Шерман Максим')
        with DB(self.url) as db:
            db.lock();db.execute("UPDATE simulation SET scenario='overheat',running=1,speed=30")
            for n in range(2):self.assertTrue(tick(db,'reset',100+n*2))
            self.assertIsNotNone(db.one("SELECT id FROM incidents WHERE sensor_id=? AND problem='threshold'",(after['targets']['temperature_sensor_id'],)))

    def test_stale_current_forecast_does_not_modify_historical_result(self):
        original=self.current()
        with DB(self.url) as db:
            db.execute('UPDATE simulation SET clock=clock+181')
        f=self.current()
        self.assertFalse(f['available']);self.assertIn('застарілий',f['explanation'])
        self.assertIsNone(f['threshold_at'])
        with DB(self.url) as db:self.assertEqual(db.one('SELECT available FROM forecasts WHERE id=?',(original['id'],))['available'],1)


if __name__=='__main__':unittest.main()
