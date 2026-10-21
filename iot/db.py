import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DB:
    def __init__(self, url):
        self.postgres = url.startswith(('postgresql://', 'postgres://'))
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            self.conn = psycopg.connect(url, row_factory=dict_row)
        else:
            path = url.removeprefix('sqlite:///')
            if path != ':memory:':
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path, timeout=20)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute('PRAGMA foreign_keys=ON')
            self.conn.execute('PRAGMA journal_mode=WAL')

    def execute(self, sql, args=()):
        return self.conn.execute(sql.replace('?', '%s') if self.postgres else sql, args)

    def all(self, sql, args=()):
        return [dict(row) for row in self.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        row = self.execute(sql, args).fetchone()
        return dict(row) if row else None

    def lock(self):
        # Serialize analysis, ingestion and transitions, including across processes.
        if self.postgres:
            self.execute('SELECT pg_advisory_xact_lock(7311982)')
        else:
            self.execute('BEGIN IMMEDIATE')

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, typ, value, tb):
        self.conn.rollback() if typ else self.conn.commit()
        self.close()


def migrate(url):
    with DB(url) as db:
        db.lock()
        db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)')
        versions = {r['version'] for r in db.all('SELECT version FROM schema_migrations')}
        for path in sorted((ROOT / 'migrations').glob('*.sql')):
            if int(path.name.split('_')[0]) not in versions:
                for statement in path.read_text(encoding='utf-8').split(';'):
                    if statement.strip():
                        db.execute(statement)
