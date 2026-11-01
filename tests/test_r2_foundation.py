import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from auto_research.native_store import NativeStore, SCHEMA_VERSION
from auto_research import schema11
from auto_research.service import NativeService


def legacy_project(tmp_path, version=10):
    store = NativeStore(tmp_path)
    original = store.initialize("migration fixture", "init")
    with store._connection() as db:
        db.execute("INSERT INTO review_todos VALUES('old','publication','pub/P-001',NULL,'completed','a','a')")
        # Remove the new tables to reproduce an actual pre-R2 database.
        for table in ("review_candidates", "review_obligations", "review_lifecycle_events",
                      "checkpoint_read_receipts", "runtime_budgets", "runtime_budget_reservations", "runtime_progress"):
            db.execute(f"DROP TABLE {table}")
        db.execute(f"PRAGMA user_version={version}")
    return store, original


@pytest.mark.parametrize("version", [9, 10])
def test_old_readonly_open_never_migrates(tmp_path, version):
    store, _ = legacy_project(tmp_path, version)
    before = store.db_path.read_bytes()
    NativeStore(tmp_path, readonly=True)
    assert store.db_path.read_bytes() == before
    assert not (store.meta / "schema-10-backup.sqlite3").exists()
    service = NativeService(tmp_path / 'registry.sqlite3')
    capabilities = service.project_read({'root': str(tmp_path), 'view': 'capabilities'})
    assert capabilities['schema_version'] == version
    assert capabilities['checkpoint_base'] == 'requires_migration'
    assert store.db_path.read_bytes() == before


@pytest.mark.parametrize("version", [9, 10])
def test_migration_preserves_history_and_original_idempotent_response(tmp_path, version):
    store, original = legacy_project(tmp_path, version)
    upgraded = NativeStore(tmp_path)
    assert upgraded.initialize("migration fixture", "init") == original
    with upgraded._read() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        candidate = dict(db.execute("SELECT * FROM review_candidates").fetchone())
        assert candidate["mapping_status"] == "unknown"
        assert json.loads(candidate["target_refs"]) == []
        assert db.execute("SELECT state FROM review_todos").fetchone()[0] == "completed"
        assert db.execute("SELECT COUNT(*) FROM review_obligations").fetchone()[0] == 0
    with sqlite3.connect(store.meta / "schema-10-backup.sqlite3") as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 10
        assert backup.execute("SELECT COUNT(*) FROM review_todos").fetchone()[0] == 1
    NativeStore(tmp_path)
    with upgraded._read() as db:
        assert db.execute("SELECT COUNT(*) FROM review_candidates").fetchone()[0] == 1


def test_failed_migration_is_atomic_and_can_be_retried(tmp_path, monkeypatch):
    store, _ = legacy_project(tmp_path)
    ensure = schema11.ensure_schema11
    def interrupted(db):
        ensure(db)
        raise RuntimeError("interrupted")
    monkeypatch.setattr(schema11, "ensure_schema11", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        NativeStore(tmp_path)
    with store._read() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 10
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='review_candidates'").fetchone()
    monkeypatch.setattr(schema11, "ensure_schema11", ensure)
    # A write made after failure must be present in the successful retry's backup.
    with store._connection() as db:
        db.execute("INSERT INTO review_todos VALUES('later','finish','N-001',NULL,'pending','b','b')")
    NativeStore(tmp_path)
    backups = list(store.meta.glob("schema-10-backup-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT COUNT(*) FROM review_todos").fetchone()[0] == 2


def test_measurement_does_not_treat_missing_end_or_truncated_tail_as_success(tmp_path):
    spec = importlib.util.spec_from_file_location("r2_measure", Path(__file__).parents[1] / "scripts/r2_measure.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "observations.jsonl"
    rows = [{"version": "invocation/1", "phase": "start", "invocation_id": "a", "input_fields": 2, "input_chars": 12},
            {"version": "invocation/1", "phase": "start", "invocation_id": "b", "input_fields": 1, "input_chars": 6},
            {"version": "invocation/1", "phase": "end", "invocation_id": "b", "outcome": "failed",
             "error_code": "conflict", "duration_ms": 5}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows) + '{"truncated":')
    report = module.summarize([path])
    assert report["outcomes"] == {"unknown": 1, "failed": 1}
    assert report["coverage"]["incomplete_lines"] == 1
    assert report["coverage"]["model_usage"] == "not_collected"


def test_measurement_separates_replays_transport_retries_and_missing_usage(tmp_path):
    spec = importlib.util.spec_from_file_location('r2_measure', Path(__file__).parents[1] / 'scripts/r2_measure.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    rows = [
        {'phase':'start','invocation_id':'a','operation_id':'op','input_fields':1,'input_chars':8},
        {'phase':'transport','operation_id':'op','attempt':1,'host_fields':3},
        {'phase':'transport','operation_id':'op','attempt':2},
        {'phase':'transport','operation_id':'op','attempt':1,'host_fields':3},
        {'phase':'dispatch_result','operation_id':'op','invocation_id':'a','replayed':True,'deduplicated':False},
        {'phase':'end','invocation_id':'a','outcome':'succeeded','duration_ms':2},
        {'phase':'native_usage','source_key':'request','tokens':None,'cost':None,'model':{'id':'actual'}},
        {'phase':'native_usage','source_key':'aux','request_key':'request','tokens':{'input':1,'output':2,'reasoning':3,'cache':{'read':4,'write':5}},'cost':0.25},
        {'phase':'native_usage','source_key':'aux','tokens':{'input':1,'output':2,'reasoning':3,'cache':{'read':4,'write':5}},'cost':0.25},
        {'phase':'native_usage','source_key':'late','tokens':None,'cost':None},
        {'phase':'native_aggregate','tokens':{'input':999},'cost':99},
        {'phase':'provider_retry','will_retry':False},
    ]
    path=tmp_path/'events.jsonl'
    path.write_text(''.join(json.dumps({'version':'invocation/1',**row})+'\n' for row in rows))
    result=module.summarize([path])
    assert result['transport_attempts']==3 and result['transport_retries']==1
    assert result['replayed']==1 and result['provider_retry_decisions']==0
    assert result['wire_host_metadata_fields']==6
    assert result['model_usage']=={'tokens_observed':15,'cost_observed':0.25,'sources':2,'missing_tokens':1,'missing_cost':1,'model_unknown':1}
