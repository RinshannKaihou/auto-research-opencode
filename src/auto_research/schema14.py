"""Evidence-based conclusion drafts and typed review facts."""

from contextlib import closing
from pathlib import Path
import sqlite3
from uuid import uuid4

from .errors import ValidationError


DDL = """
CREATE TABLE IF NOT EXISTS review_assessments (
 event_id INTEGER PRIMARY KEY REFERENCES review_lifecycle_events(event_id),
 review_type TEXT NOT NULL, source_ref TEXT NOT NULL, target_ref TEXT NOT NULL,
 identity TEXT NOT NULL, limitations TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_assessments_target ON review_assessments(target_ref,review_type);
CREATE TABLE IF NOT EXISTS conclusion_drafts (
 draft_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, host_id TEXT NOT NULL,
 session_id TEXT NOT NULL, basis_digest TEXT NOT NULL, manifest_digest TEXT NOT NULL,
 source_sequence INTEGER NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conclusion_evidence (
 conclusion_id TEXT PRIMARY KEY REFERENCES project_conclusions(conclusion_id),
 draft_id TEXT NOT NULL REFERENCES conclusion_drafts(draft_id), basis_digest TEXT NOT NULL,
 data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conclusion_publications (
 publication_id TEXT PRIMARY KEY REFERENCES publications(publication_id),
 draft_id TEXT NOT NULL REFERENCES conclusion_drafts(draft_id), report_item_id TEXT NOT NULL,
 appendix_item_id TEXT NOT NULL, report_digest TEXT NOT NULL, appendix_digest TEXT NOT NULL,
 basis_digest TEXT NOT NULL
);
"""


def ensure_schema14(db):
    for statement in DDL.split(";"):
        if statement.strip():
            db.execute(statement)


def migrate_schema14(path: Path):
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 14:
                db.rollback()
                return
            if version != 13:
                raise ValidationError(f"Expected schema 13, found {version}")
            backup = path.parent / "schema-13-backup.sqlite3"
            if backup.exists():
                backup = path.parent / f"schema-13-backup-{uuid4().hex}.sqlite3"
            temporary = backup.with_suffix(".tmp")
            try:
                with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(temporary)) as target:
                    source.backup(target)
                temporary.replace(backup)
            finally:
                temporary.unlink(missing_ok=True)
            with closing(sqlite3.connect(backup)) as saved:
                if (saved.execute("PRAGMA user_version").fetchone()[0] != 13
                        or saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok"):
                    raise ValidationError("Invalid schema 13 backup")
                existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in {"requests", "events", "exploration_scopes", "exploration_routes", "exploration_decisions", "exploration_results"} & existing:
                    if saved.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != db.execute(
                            f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                        raise ValidationError(f"Schema 13 backup row count differs: {table}")
            ensure_schema14(db)
            db.execute("PRAGMA user_version=14")
            db.commit()
        except BaseException:
            db.rollback()
            raise
