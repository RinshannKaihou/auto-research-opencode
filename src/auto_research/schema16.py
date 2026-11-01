"""Multi-route work allocations attached to the existing runtime reservations."""

from contextlib import closing
from pathlib import Path
import sqlite3
from uuid import uuid4

from .errors import ValidationError


DDL = """
CREATE TABLE IF NOT EXISTS exploration_allocations (
 reservation_id TEXT PRIMARY KEY REFERENCES runtime_budget_reservations(reservation_id),
 intent_id TEXT NOT NULL UNIQUE, scope_id TEXT NOT NULL, task_id TEXT,
 amount REAL NOT NULL CHECK(amount >= 0), unit TEXT NOT NULL,
 resources TEXT NOT NULL, resource_state TEXT NOT NULL,
 actual_usage REAL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS exploration_allocations_task ON exploration_allocations(task_id);
CREATE INDEX IF NOT EXISTS exploration_allocations_resources ON exploration_allocations(resource_state)
"""


def ensure_schema16(db):
    for statement in DDL.split(";"):
        if statement.strip():
            db.execute(statement)


def migrate_schema16(path: Path):
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 16:
                db.rollback()
                return
            if version != 15:
                raise ValidationError(f"Expected schema 15, found {version}")
            backup = path.parent / "schema-15-backup.sqlite3"
            if backup.exists():
                backup = path.parent / f"schema-15-backup-{uuid4().hex}.sqlite3"
            temporary = backup.with_suffix(".tmp")
            try:
                with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(temporary)) as target:
                    source.backup(target)
                temporary.replace(backup)
            finally:
                temporary.unlink(missing_ok=True)
            existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            preserved = {"requests", "events", "runtime_budget_reservations", "workflow_intents",
                         "exploration_scopes", "exploration_routes", "exploration_decisions",
                         "exploration_results", "generation_conclusion_contracts"} & existing
            with closing(sqlite3.connect(backup)) as saved:
                if (saved.execute("PRAGMA user_version").fetchone()[0] != 15
                        or saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok"):
                    raise ValidationError("Invalid schema 15 backup")
                for table in preserved:
                    if saved.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() != db.execute(
                            f"SELECT * FROM {table} ORDER BY rowid").fetchall():
                        raise ValidationError(f"Schema 15 backup differs: {table}")
            ensure_schema16(db)
            db.execute("PRAGMA user_version=16")
            db.commit()
        except BaseException:
            db.rollback()
            raise
