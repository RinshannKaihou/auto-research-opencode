"""R2 additive storage. Historical tasks, requests and attribution stay intact."""
from pathlib import Path
import sqlite3
from uuid import uuid4

from .errors import ValidationError


DDL = """
CREATE TABLE IF NOT EXISTS review_candidates (
 candidate_id TEXT PRIMARY KEY, legacy_todo_id TEXT UNIQUE,
 trigger_kind TEXT NOT NULL, trigger_ref TEXT NOT NULL, node_id TEXT,
 target_refs TEXT NOT NULL, mapping_status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_obligations (
 obligation_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
 scope_owner TEXT NOT NULL, target_kind TEXT NOT NULL, target_ref TEXT NOT NULL,
 change_id TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK(target_kind IN ('knowledge','material')),
 CHECK(state IN ('pending','running','completed','out_of_scope','waived'))
);
CREATE UNIQUE INDEX IF NOT EXISTS unique_review_target ON review_obligations
 (generation,scope_owner,target_kind,target_ref) WHERE change_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS unique_review_impact ON review_obligations
 (generation,scope_owner,target_kind,target_ref,change_id) WHERE change_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS review_lifecycle_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, obligation_id TEXT, candidate_id TEXT,
 action TEXT NOT NULL, reason TEXT NOT NULL, evidence_refs TEXT NOT NULL,
 scope_snapshot TEXT NOT NULL, source_sequence INTEGER NOT NULL,
 operation_id TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS unique_review_event ON review_lifecycle_events
 (operation_id,obligation_id,action) WHERE obligation_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS checkpoint_read_receipts (
 base_id TEXT PRIMARY KEY, host_id TEXT NOT NULL, session_id TEXT NOT NULL,
 plugin_instance_id TEXT NOT NULL, association_id TEXT NOT NULL, attempt_id TEXT,
 generation INTEGER NOT NULL, node_id TEXT, head_revision INTEGER NOT NULL,
 selected_checkpoint_id TEXT, selected_revision INTEGER, state_digest TEXT NOT NULL,
 source_sequence INTEGER NOT NULL, origin TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_budgets (
 generation INTEGER PRIMARY KEY, limits TEXT NOT NULL, started_at TEXT NOT NULL,
 deadline_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_budget_reservations (
 reservation_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, kind TEXT NOT NULL,
 session_id TEXT, owner TEXT NOT NULL, operation_id TEXT NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(generation,kind,operation_id)
);
CREATE TABLE IF NOT EXISTS runtime_progress (
 generation INTEGER NOT NULL, host_id TEXT NOT NULL, session_id TEXT NOT NULL,
 last_event_sequence INTEGER NOT NULL, idle_turns INTEGER NOT NULL,
 last_finished_turn TEXT, PRIMARY KEY(generation,host_id,session_id)
)
"""


def ensure_schema11(db):
    for statement in DDL.split(";"):
        if statement.strip():
            db.execute(statement)


def migrate_schema11(path: Path):
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 11:
                db.rollback()
                return
            if version != 10:
                raise ValidationError(f"Expected schema 10, found {version}")
            backup = path.parent / "schema-10-backup.sqlite3"
            if backup.exists():
                # A previous failed attempt may predate further schema-10 writes.
                # Keep that backup and take a new, consistent snapshot on retry.
                backup = path.parent / f"schema-10-backup-{uuid4().hex}.sqlite3"
            temporary = backup.with_suffix(".tmp")
            try:
                # The writer lock excludes new commits; a separate reader can
                # back up the committed WAL snapshot without backing up this transaction.
                with sqlite3.connect(path) as source, sqlite3.connect(temporary) as target:
                    source.backup(target)
                temporary.replace(backup)
            finally:
                temporary.unlink(missing_ok=True)
            with sqlite3.connect(backup) as saved:
                if (saved.execute("PRAGMA user_version").fetchone()[0] != 10
                        or saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok"):
                    raise ValidationError("Invalid schema 10 backup")
                # Early schema-2 projects acquire some base tables only when
                # NativeStore completes initialization after the migration chain.
                existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in {"requests", "events", "knowledge_revisions", "review_todos", "node_checkpoints"} & existing:
                    if saved.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != db.execute(
                            f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                        raise ValidationError(f"Schema 10 backup row count differs: {table}")
            ensure_schema11(db)
            # Explain existing tasks without manufacturing version links or completion.
            db.execute("""INSERT OR IGNORE INTO review_candidates
                SELECT todo_id,todo_id,trigger_kind,trigger_ref,node_id,'[]','unknown',created_at
                FROM review_todos""")
            db.execute("PRAGMA user_version=11")
            db.commit()
        except BaseException:
            db.rollback()
            raise
