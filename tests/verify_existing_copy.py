"""Non-destructive upgrade check on a consistent backup of the existing SQLite DB."""
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from app import create_app

root=Path(__file__).resolve().parent.parent
source=root/'test-results/before-fixes.sqlite3'
target=root/'test-results/migrated-existing.sqlite3'
assert source.is_file(), 'Create the consistent pre-upgrade backup first'
with sqlite3.connect(source) as old, sqlite3.connect(target) as copy:
    old.backup(copy)
    tables=[row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('schema_migrations','sqlite_sequence')")]
    columns={table:[r[1] for r in old.execute(f'PRAGMA table_info({table})')] for table in tables}
    before={table:sorted(old.execute(f'SELECT * FROM {table}').fetchall(),key=repr) for table in tables}
app=create_app('sqlite:///'+str(target))
with sqlite3.connect(target) as copy:
    for table in tables:
        after=sorted(copy.execute(f'SELECT {",".join(columns[table])} FROM {table}').fetchall(),key=repr)
        assert after==before[table],f'Existing data changed: {table}'
    assert not copy.execute('PRAGMA foreign_key_check').fetchall()
    versions=copy.execute('SELECT version FROM schema_migrations ORDER BY version').fetchall()
    assert versions==[(1,),(2,)]
    targets=copy.execute('SELECT temperature_sensor_id,power_sensor_id FROM simulation_targets').fetchone()
    assert all(targets)
print(json.dumps(dict(result='OK',versions=[v[0] for v in versions],preserved_rows={table:len(before[table]) for table in tables}),ensure_ascii=True))
