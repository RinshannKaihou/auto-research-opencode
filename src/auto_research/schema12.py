"""Add a durable revision for changes to research execution control."""
from contextlib import closing
from pathlib import Path
import sqlite3
from uuid import uuid4

from .errors import ValidationError


def ensure_schema12(db):
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_project'").fetchone()
    if not exists:
        return
    columns = {row[1] for row in db.execute("PRAGMA table_info(workflow_project)")}
    if "control_revision" not in columns:
        db.execute("ALTER TABLE workflow_project ADD COLUMN control_revision INTEGER NOT NULL DEFAULT 0")
    db.execute("""CREATE TRIGGER IF NOT EXISTS control_project_revision
        AFTER UPDATE OF state,generation,main_session_id ON workflow_project
        WHEN NEW.state IS NOT OLD.state OR NEW.generation IS NOT OLD.generation
             OR NEW.main_session_id IS NOT OLD.main_session_id
        BEGIN
            UPDATE workflow_project SET control_revision=control_revision+1 WHERE id=NEW.id;
        END""")
    sessions = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_sessions'").fetchone()
    if sessions:
        db.execute("""CREATE TRIGGER IF NOT EXISTS control_session_revision
            AFTER UPDATE OF pause_reason,detached,role ON workflow_sessions
            WHEN NEW.pause_reason IS NOT OLD.pause_reason OR NEW.detached IS NOT OLD.detached
                 OR NEW.role IS NOT OLD.role
            BEGIN
                UPDATE workflow_project SET control_revision=control_revision+1 WHERE id=1;
            END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS control_session_insert_revision
            AFTER INSERT ON workflow_sessions
            BEGIN UPDATE workflow_project SET control_revision=control_revision+1 WHERE id=1; END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS control_session_delete_revision
            AFTER DELETE ON workflow_sessions
            BEGIN UPDATE workflow_project SET control_revision=control_revision+1 WHERE id=1; END""")


def migrate_schema12(path: Path):
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 12:
                db.rollback()
                return
            if version != 11:
                raise ValidationError(f"Expected schema 11, found {version}")
            backup = path.parent / "schema-11-backup.sqlite3"
            if backup.exists():
                backup = path.parent / f"schema-11-backup-{uuid4().hex}.sqlite3"
            temporary = backup.with_suffix(".tmp")
            try:
                with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(temporary)) as target:
                    source.backup(target)
                temporary.replace(backup)
            finally:
                temporary.unlink(missing_ok=True)
            with closing(sqlite3.connect(backup)) as saved:
                if (saved.execute("PRAGMA user_version").fetchone()[0] != 11
                        or saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok"):
                    raise ValidationError("Invalid schema 11 backup")
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in {"requests", "events", "workflow_project", "exploration_tasks", "workflow_intents"} & tables:
                    if saved.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != db.execute(
                            f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                        raise ValidationError(f"Schema 11 backup row count differs: {table}")
            ensure_schema12(db)
            db.execute("PRAGMA user_version=12")
            db.commit()
        except BaseException:
            db.rollback()
            raise
