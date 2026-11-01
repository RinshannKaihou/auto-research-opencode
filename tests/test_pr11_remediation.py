"""Regression cases for PR 11 review findings."""

import argparse
import sqlite3

import pytest

from auto_research import native_store, schema15
from auto_research.maintenance_cli import execute
from test_opencode_host import Host


def test_generation_contract_changes_without_rewriting_previous_generation(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('exploration_policy', mode='on')
    host.call('exploration_policy', mode='off')
    assert 'cannot be downgraded' in host.error('conclusion_contract', contract='legacy-v1')
    host.call('workflow', action='run', fields={'state': 'running', 'new_generation': True})
    assert host.call('conclusion_contract')['conclusion_contract'] == 'legacy-v1'
    host.call('conclusion_contract', contract='evidence-v1')
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert db.execute('SELECT generation,contract FROM generation_conclusion_contracts ORDER BY generation').fetchall() == [
            (0, 'evidence-v1'), (1, 'evidence-v1')]


def test_schema15_preserves_notifications_and_migrates_current_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(native_store, 'SCHEMA_VERSION', 14)
    host = Host(tmp_path)
    host.open()
    host.call('exploration_policy', mode='on')
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        db.execute('INSERT INTO workflow_notifications VALUES(?,?,?,?,?,?,?,?)',
                   ('n1', 'task1', 'ses_main', 'finished', None, '{}', 'pending', 'time'))
    monkeypatch.setattr(native_store, 'SCHEMA_VERSION', 15)
    native_store.NativeStore(host.root)
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 15
        assert db.execute('SELECT * FROM workflow_notifications').fetchone() == (
            'n1', 'task1', 'ses_main', 'finished', None, '{}', 'pending', 'time')
        assert db.execute('SELECT generation,contract,origin FROM generation_conclusion_contracts').fetchone() == (
            0, 'evidence-v1', 'migrated_config')
        db.execute('INSERT INTO workflow_notifications VALUES(?,?,?,?,?,?,?,?)',
                   ('n2', None, 'ses_main', 'exploration', None, '{}', 'pending', 'time'))
    with sqlite3.connect(host.root / '.research/schema-14-backup.sqlite3') as backup:
        assert backup.execute('PRAGMA user_version').fetchone()[0] == 14
        assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_schema15_failure_rolls_back_and_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(native_store, 'SCHEMA_VERSION', 14)
    host = Host(tmp_path)
    host.open()
    monkeypatch.setattr(native_store, 'SCHEMA_VERSION', 15)
    original = schema15.ensure_schema15

    def interrupted(db):
        original(db)
        raise RuntimeError('interrupted migration')

    monkeypatch.setattr(schema15, 'ensure_schema15', interrupted)
    with pytest.raises(RuntimeError, match='interrupted'):
        native_store.NativeStore(host.root)
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 14
        assert next(row for row in db.execute('PRAGMA table_info(workflow_notifications)') if row[1] == 'task_id')[3]
    monkeypatch.setattr(schema15, 'ensure_schema15', original)
    native_store.NativeStore(host.root)


@pytest.mark.parametrize('version', [12, 13, 14])
def test_readonly_export_supports_older_versions(tmp_path, monkeypatch, version):
    monkeypatch.setattr(native_store, 'SCHEMA_VERSION', version)
    host = Host(tmp_path)
    host.open()
    monkeypatch.setattr(native_store, 'SCHEMA_VERSION', 15)
    out = tmp_path / 'export.json'
    result = execute(argparse.Namespace(command='export', project=host.root, output=out))
    assert result['schema_version'] == version
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
