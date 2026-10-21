import tempfile
import unittest
from pathlib import Path
from app import create_app
from iot.db import DB, ROOT, migrate


class MigrationTests(unittest.TestCase):
    def test_v1_upgrade_preserves_existing_data_and_initializes_targets(self):
        with tempfile.TemporaryDirectory() as folder:
            url='sqlite:///'+str(Path(folder)/'old.sqlite3')
            with DB(url) as db:
                for statement in (ROOT/'migrations/001_initial.sql').read_text(encoding='utf-8').split(';'):
                    if statement.strip():db.execute(statement)
                db.execute("INSERT INTO users VALUES ('user','operator','Шерман Максим','operator','hash',1)")
                db.execute("INSERT INTO equipment VALUES ('rack','Інша назва','rack','A',1)")
                db.execute("INSERT INTO equipment VALUES ('ups','Живлення','ups','A',1)")
                db.execute("INSERT INTO sensors VALUES ('temp','rack','Температура','temperature','°C',1)")
                db.execute("INSERT INTO sensors VALUES ('power','ups','Заряд','charge','%',1)")
                db.execute("INSERT INTO rules VALUES ('temp',18,32,180)")
                db.execute("INSERT INTO measurements VALUES ('m','temp','packet',25,1000,1001)")
                db.execute("INSERT INTO forecasts VALUES ('f','temp',1000,900,25,0,NULL,'Історія',1)")
                db.execute("INSERT INTO incidents VALUES ('i','rack','temp','f','risk','medium','new',NULL,'Ризик',1000,1000,NULL)")
                db.execute("INSERT INTO simulation VALUES (1,'normal',0,1,1000,0,NULL,0)")
                tables=['users','equipment','sensors','rules','measurements','forecasts','incidents','simulation']
                original={table:db.all(f'SELECT * FROM {table}') for table in tables}
            migrate(url)
            migrate(url)  # Applying twice must be harmless.
            with DB(url) as db:
                for table,rows in original.items():
                    for before,after in zip(rows,db.all(f'SELECT * FROM {table}')):
                        self.assertEqual(before,{key:after[key] for key in before})
                    self.assertEqual(len(rows),len(db.all(f'SELECT * FROM {table}')))
                self.assertEqual(db.one('SELECT * FROM simulation_targets'),dict(id=1,temperature_sensor_id='temp',power_sensor_id='power'))
                self.assertEqual([r['version'] for r in db.all('SELECT version FROM schema_migrations ORDER BY version')],[1,2])
                self.assertEqual(db.all('PRAGMA foreign_key_check'),[])

    def test_clean_database_seeds_valid_targets_and_forecasts(self):
        with tempfile.TemporaryDirectory() as folder:
            url='sqlite:///'+str(Path(folder)/'clean.sqlite3')
            app=create_app(url)
            self.assertEqual(app.test_client().get('/health').status_code,200)
            with DB(url) as db:
                self.assertEqual(db.one('SELECT COUNT(*) AS n FROM measurements')['n'],1098)
                self.assertEqual(db.one('SELECT COUNT(*) AS n FROM equipment')['n'],8)
                targets=db.one('SELECT * FROM simulation_targets')
                self.assertEqual(db.one('SELECT metric FROM sensors WHERE id=?',(targets['temperature_sensor_id'],))['metric'],'temperature')
                self.assertEqual(db.one('SELECT metric FROM sensors WHERE id=?',(targets['power_sensor_id'],))['metric'],'charge')
                self.assertEqual(db.all('PRAGMA foreign_key_check'),[])
