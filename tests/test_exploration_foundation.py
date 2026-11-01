"""Exploration cards are versioned choices, not dispatched work."""

import hashlib
import sqlite3

import pytest

from auto_research import native_store, schema13, schema14
from auto_research.errors import ConflictError
from auto_research.native_store import NativeStore
from test_opencode_host import Host


def scope_and_route(host):
    host.call("exploration_policy", mode="on")
    scope = host.call("exploration_scope", model_call=True, fields={"changes": {
        "objective": "Improve latency", "task_kind": "selection", "constraints": {"memory": "bounded"}}})
    route = host.call("exploration_route", model_call=True, fields={"scope_ref": scope["ref"], "changes": {
        "title": "Measure tail", "rationale": "Average cost may not explain the tail",
        "critical_assumption": "Tail costs differ from the average"}})
    return scope, route


def test_cards_do_not_create_nodes_and_replay_does_not_append(tmp_path):
    host = Host(tmp_path)
    host.open()
    scope, route = scope_and_route(host)
    fields = {"scope_ref": scope["ref"], "route_ref": route["ref"], "reason": "clarify",
              "changes": {"rationale": "Test whether the tail is different"}}
    first = host.call("exploration_route", model_call=True, fields=fields, operation_id="same-edit")
    assert host.call("exploration_route", model_call=True, fields=fields, operation_id="same-edit") == first
    assert "exploration_route_changed" in host.error("exploration_route", model_call=True,
                                                       fields=fields, operation_id="stale-edit")
    with sqlite3.connect(host.root / ".research/state.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM exploration_tasks").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM exploration_routes").fetchone()[0] == 2


def test_scope_revision_and_policy_are_explicit(tmp_path):
    host = Host(tmp_path)
    host.open()
    assert host.call("exploration_policy")["exploration_policy"] == "off"
    scope, route = scope_and_route(host)
    assert host.call("context_policy")["policy_version"] == "memory-r1-v1"
    assert "Disable exploration" in host.error("context_policy", policy="memory-v2")
    updated = host.call("exploration_scope", model_call=True, fields={"scope_ref": scope["ref"],
        "reason": "new constraint", "changes": {"constraints": {"memory": "smaller"}}})
    assert updated["goal_version"] == 2
    assert "exploration_scope_changed" in host.error("exploration_route", model_call=True, fields={
        "scope_ref": scope["ref"], "route_ref": route["ref"], "reason": "old scope", "changes": {}})
    host.call("exploration_policy", mode="off")
    assert host.call("query", ref=route["ref"])["disposition"] == "untried"
    host.call("context_policy", policy="memory-v2")


def test_only_current_main_can_manage_routes(tmp_path):
    host = Host(tmp_path)
    host.open()
    scope, route = scope_and_route(host)
    host.call("open", session="ses_node", root=str(host.root), cwd=str(host.root), session_role="node_core")
    message = host.error("exploration_route", session="ses_node", model_call=True, fields={
        "scope_ref": scope["ref"], "route_ref": route["ref"], "reason": "not main", "changes": {}})
    assert "Only the current project main" in message


def test_route_pagination_rejects_changed_current_revisions(tmp_path):
    host = Host(tmp_path)
    host.open()
    scope, route = scope_and_route(host)
    host.call("exploration_route", model_call=True, fields={"scope_ref": scope["ref"], "changes": {
        "title": "Keep baseline", "rationale": "May already be enough", "critical_assumption": "Goal is met"}})
    page = host.call("query", collection="exploration_routes", limit=1)
    assert page["cursor"] is not None and page["total"] == 2
    host.call("exploration_route", model_call=True, fields={"scope_ref": scope["ref"],
        "route_ref": route["ref"], "reason": "clarify", "changes": {"title": "Check tail"}})
    assert "read_view_changed" in host.error("query", collection="exploration_routes", cursor=page["cursor"])


def test_schema12_readonly_and_migration_preserve_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(native_store, "SCHEMA_VERSION", 12)
    old = NativeStore(tmp_path)
    original = old.initialize("Existing project", "old-init")
    monkeypatch.setattr(native_store, "SCHEMA_VERSION", 13)
    readonly = NativeStore(tmp_path, readonly=True)
    assert readonly.exploration_page("exploration_scopes")["semantics"] == "legacy"
    assert not (old.meta / "schema-12-backup.sqlite3").exists()
    upgraded = NativeStore(tmp_path)
    assert upgraded.initialize("Existing project", "old-init") == original
    with upgraded._read() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 13
        assert db.execute("SELECT COUNT(*) FROM exploration_scopes").fetchone()[0] == 0
    with sqlite3.connect(old.meta / "schema-12-backup.sqlite3") as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 12


def test_failed_schema13_migration_rolls_back_and_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(native_store, "SCHEMA_VERSION", 12)
    old = NativeStore(tmp_path)
    old.initialize("Existing project", "init")
    monkeypatch.setattr(native_store, "SCHEMA_VERSION", 13)
    ensure = schema13.ensure_schema13
    def interrupted(db):
        ensure(db)
        raise RuntimeError("interrupted")
    monkeypatch.setattr(schema13, "ensure_schema13", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        NativeStore(tmp_path)
    with sqlite3.connect(old.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 12
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='exploration_scopes'").fetchone()
    monkeypatch.setattr(schema13, "ensure_schema13", ensure)
    assert NativeStore(tmp_path).db_path.exists()
    assert len(list(old.meta.glob("schema-12-backup-*.sqlite3"))) == 1


def test_schema13_to_14_preserves_exploration_and_read_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(native_store, 'SCHEMA_VERSION', 13)
    host = Host(tmp_path)
    host.open()
    scope, route = scope_and_route(host)
    page = host.call('query', collection='evidence', refs=[])
    end = page['cursor']['offset'] if page['cursor'] else page['total_chars']
    receipt = host.call('evidence_observed', plugin_instance_id='before-upgrade', pages=[{
        'snapshot_id': page['snapshot_id'], 'offset': page['offset'], 'end': end,
        'manifest_digest': page['manifest_digest'],
        'chunk_digest': hashlib.sha256(page['chunk'].encode()).hexdigest(),
    }])['receipt_ids'][0]
    old = host.service.stores[str(host.root)]
    monkeypatch.setattr(native_store, 'SCHEMA_VERSION', 14)
    readonly = NativeStore(host.root, readonly=True)
    with readonly._read() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 13
        assert db.execute('SELECT receipt_id FROM evidence_read_receipts').fetchone()[0] == receipt
    assert not (old.meta / 'schema-13-backup.sqlite3').exists()
    upgraded = NativeStore(host.root)
    with upgraded._read() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 14
        assert db.execute('SELECT receipt_id FROM evidence_read_receipts').fetchone()[0] == receipt
        assert db.execute('SELECT scope_id FROM exploration_scopes').fetchone()[0] == scope['scope_id']
        assert db.execute('SELECT route_id FROM exploration_routes').fetchone()[0] == route['route_id']
    with sqlite3.connect(old.meta / 'schema-13-backup.sqlite3') as backup:
        assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert backup.execute('PRAGMA user_version').fetchone()[0] == 13
        assert backup.execute('SELECT receipt_id FROM evidence_read_receipts').fetchone()[0] == receipt


def test_failed_schema14_migration_rolls_back_and_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(native_store, 'SCHEMA_VERSION', 13)
    host = Host(tmp_path)
    host.open()
    scope, _ = scope_and_route(host)
    old = host.service.stores[str(host.root)]
    monkeypatch.setattr(native_store, 'SCHEMA_VERSION', 14)
    ensure = schema14.ensure_schema14
    def interrupted(db):
        ensure(db)
        raise RuntimeError('interrupted')
    monkeypatch.setattr(schema14, 'ensure_schema14', interrupted)
    with pytest.raises(RuntimeError, match='interrupted'):
        NativeStore(host.root)
    with sqlite3.connect(old.db_path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 13
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='conclusion_drafts'").fetchone()
        assert db.execute('SELECT scope_id FROM exploration_scopes').fetchone()[0] == scope['scope_id']
    monkeypatch.setattr(schema14, 'ensure_schema14', ensure)
    assert NativeStore(host.root).db_path.exists()
    assert len(list(old.meta.glob('schema-13-backup-*.sqlite3'))) == 1
