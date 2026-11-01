"""Generation-scoped conclusion policy and exploration result notifications."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .errors import ValidationError


def ensure_schema15(db):
    info = list(db.execute('PRAGMA table_info(workflow_notifications)'))
    if next(row for row in info if row[1] == 'task_id')[3]:
        db.execute('CREATE TABLE workflow_notifications_v15 ('
                   'notification_id TEXT PRIMARY KEY, task_id TEXT, recipient TEXT NOT NULL, '
                   'kind TEXT NOT NULL, reference TEXT, payload TEXT NOT NULL, '
                   'state TEXT NOT NULL, created_at TEXT NOT NULL)')
        db.execute('INSERT INTO workflow_notifications_v15 SELECT * FROM workflow_notifications')
        db.execute('DROP TABLE workflow_notifications')
        db.execute('ALTER TABLE workflow_notifications_v15 RENAME TO workflow_notifications')
    columns = {row[1] for row in db.execute('PRAGMA table_info(evidence_read_receipts)')}
    if 'model_message_id' not in columns:
        db.execute('ALTER TABLE evidence_read_receipts ADD COLUMN model_message_id TEXT')
    db.execute('CREATE TABLE IF NOT EXISTS generation_conclusion_contracts ('
               'generation INTEGER PRIMARY KEY, contract TEXT NOT NULL, origin TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS evidence_receipts_material '
               'ON evidence_read_receipts(generation,host_id,session_id,part,snapshot_id,digest)')
    db.execute('CREATE INDEX IF NOT EXISTS workflow_notifications_recipient '
               'ON workflow_notifications(recipient,state,kind)')


def migrate_schema15(path: Path):
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version == 15:
                db.rollback()
                return
            if version != 14:
                raise ValidationError(f'Expected schema 14, found {version}')
            backup = path.parent / 'schema-14-backup.sqlite3'
            if backup.exists():
                backup = path.parent / f'schema-14-backup-{uuid4().hex}.sqlite3'
            temporary = backup.with_suffix('.tmp')
            try:
                with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(temporary)) as target:
                    source.backup(target)
                temporary.replace(backup)
            finally:
                temporary.unlink(missing_ok=True)
            with closing(sqlite3.connect(backup)) as saved:
                if (saved.execute('PRAGMA user_version').fetchone()[0] != 14
                        or saved.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'):
                    raise ValidationError('Invalid schema 14 backup')
                existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in {'requests', 'events', 'workflow_notifications', 'conclusion_drafts'} & existing:
                    if saved.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] != db.execute(
                            f'SELECT COUNT(*) FROM {table}').fetchone()[0]:
                        raise ValidationError(f'Schema 14 backup row count differs: {table}')
            before = list(db.execute('SELECT * FROM workflow_notifications ORDER BY notification_id'))
            project = (db.execute('SELECT p.generation,c.config FROM workflow_project p CROSS JOIN project c WHERE p.id=1').fetchone()
                       if {'workflow_project', 'project'} <= existing else None)
            ensure_schema15(db)
            after = list(db.execute('SELECT * FROM workflow_notifications ORDER BY notification_id'))
            if before != after:
                raise ValidationError('Notification values changed during migration')
            if project:
                config = json.loads(project[1])
                db.execute('INSERT INTO generation_conclusion_contracts VALUES(?,?,?)',
                           (project[0], config.get('conclusion_contract', 'legacy-v1'), 'migrated_config'))
            db.execute('PRAGMA user_version=15')
            db.commit()
        except BaseException:
            db.rollback()
            raise
