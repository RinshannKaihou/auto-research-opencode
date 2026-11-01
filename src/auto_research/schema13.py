"""Persistent exploration choices and evidence delivery receipts."""

from contextlib import closing
from pathlib import Path
import sqlite3
from uuid import uuid4

from .errors import ValidationError


DDL = """
CREATE TABLE IF NOT EXISTS exploration_scopes (
 scope_id TEXT NOT NULL, revision INTEGER NOT NULL, generation INTEGER NOT NULL,
 goal_version INTEGER NOT NULL, data TEXT NOT NULL, actor TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(scope_id,revision)
);
CREATE INDEX IF NOT EXISTS exploration_scopes_generation ON exploration_scopes(generation,scope_id,revision);
CREATE TABLE IF NOT EXISTS exploration_routes (
 route_id TEXT NOT NULL, revision INTEGER NOT NULL, scope_id TEXT NOT NULL,
 goal_version INTEGER NOT NULL, data TEXT NOT NULL, actor TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(route_id,revision)
);
CREATE INDEX IF NOT EXISTS exploration_routes_scope ON exploration_routes(scope_id,route_id,revision);
CREATE TABLE IF NOT EXISTS exploration_decisions (
 decision_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, scope_revision INTEGER NOT NULL,
 generation INTEGER NOT NULL, data TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS exploration_decisions_scope ON exploration_decisions(scope_id,decision_id);
CREATE TABLE IF NOT EXISTS exploration_bindings (
 decision_id TEXT NOT NULL, route_id TEXT NOT NULL, route_revision INTEGER NOT NULL,
 scope_id TEXT NOT NULL, scope_revision INTEGER NOT NULL, node_id TEXT NOT NULL,
 role_in_route TEXT NOT NULL, PRIMARY KEY(decision_id,route_id,node_id)
);
CREATE INDEX IF NOT EXISTS exploration_bindings_node ON exploration_bindings(node_id);
CREATE TABLE IF NOT EXISTS exploration_results (
 result_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, source_key TEXT NOT NULL,
 kind TEXT NOT NULL, source_ref TEXT, source_sequence INTEGER NOT NULL,
 data TEXT NOT NULL, handled_by_decision TEXT, created_at TEXT NOT NULL,
 UNIQUE(scope_id,source_key)
);
CREATE INDEX IF NOT EXISTS exploration_results_pending ON exploration_results(scope_id,handled_by_decision);
CREATE TABLE IF NOT EXISTS evidence_read_receipts (
 receipt_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, host_id TEXT NOT NULL,
 session_id TEXT NOT NULL, plugin_instance_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
 draft_id TEXT, part TEXT NOT NULL, start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
 digest TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(generation,host_id,session_id,plugin_instance_id,snapshot_id,part,start_offset,end_offset,digest)
);
"""


def ensure_schema13(db):
    for statement in DDL.split(";"):
        if statement.strip():
            db.execute(statement)


def migrate_schema13(path: Path):
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 13:
                db.rollback()
                return
            if version != 12:
                raise ValidationError(f"Expected schema 12, found {version}")
            backup = path.parent / "schema-12-backup.sqlite3"
            if backup.exists():
                backup = path.parent / f"schema-12-backup-{uuid4().hex}.sqlite3"
            temporary = backup.with_suffix(".tmp")
            try:
                with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(temporary)) as target:
                    source.backup(target)
                temporary.replace(backup)
            finally:
                temporary.unlink(missing_ok=True)
            with closing(sqlite3.connect(backup)) as saved:
                if (saved.execute("PRAGMA user_version").fetchone()[0] != 12
                        or saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok"):
                    raise ValidationError("Invalid schema 12 backup")
                existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in {"requests", "events", "workflow_project", "exploration_tasks", "workflow_intents"} & existing:
                    if saved.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != db.execute(
                            f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                        raise ValidationError(f"Schema 12 backup row count differs: {table}")
            ensure_schema13(db)
            db.execute("PRAGMA user_version=13")
            db.commit()
        except BaseException:
            db.rollback()
            raise
