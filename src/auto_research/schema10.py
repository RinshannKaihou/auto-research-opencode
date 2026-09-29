"""Schema 10: append-only project conclusion receipts."""
from pathlib import Path
import sqlite3

from .errors import ValidationError


CONCLUSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_conclusions (
 conclusion_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
 generation INTEGER NOT NULL UNIQUE, host_id TEXT NOT NULL, session_id TEXT NOT NULL,
 summary TEXT NOT NULL, final_ref TEXT NOT NULL, outcome TEXT NOT NULL,
 gaps TEXT NOT NULL, review TEXT NOT NULL, created_at TEXT NOT NULL,
 CHECK(outcome IN ('answered','partial','unresolved'))
)
"""


def ensure_schema10(db):
    db.execute(CONCLUSION_SCHEMA)


def migrate_schema10(path: Path):
    with sqlite3.connect(path, isolation_level=None) as db:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version == 10:
            return
        if version != 9:
            raise ValidationError(f'Expected schema 9, found {version}')
        busy, _, _ = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if busy:
            raise ValidationError('Cannot checkpoint schema 9 for backup')
        db.execute('BEGIN IMMEDIATE')
        try:
            backup = path.parent / 'schema-9-backup.sqlite3'
            try:
                with backup.open('xb') as target:
                    target.write(path.read_bytes())
            except FileExistsError:
                pass
            ensure_schema10(db)
            db.execute('PRAGMA user_version=10')
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
