"""Regression cases for PR 11 review findings."""

import argparse
import hashlib
import sqlite3

import pytest

from auto_research import native_store, schema15
from auto_research.maintenance_cli import execute
from test_opencode_host import Host
from test_context_policy import completed
from test_evidence_conclusion import prepare_and_publish
from auto_research.service import NativeService


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


def evidence_page(page):
    return {'snapshot_id': page['snapshot_id'], 'draft_id': page.get('draft_id'),
            'offset': page['offset'], 'end': page['cursor']['offset'] if page['cursor'] else page['total_chars'],
            'manifest_digest': page['manifest_digest'],
            'chunk_digest': hashlib.sha256(page['chunk'].encode()).hexdigest()}


def test_expired_page_does_not_poison_valid_draft_and_restart(completed, tmp_path):
    host, _, _, _, claim, _ = completed
    draft, _ = prepare_and_publish(host, claim)
    old = host.call('query', collection='evidence', refs=[])
    for _ in range(17):
        host.call('query', collection='evidence', refs=[])
    current = host.call('query', collection='evidence', draft_id=draft['draft_id'], limit=127)
    pages = [evidence_page(old), evidence_page(current)]
    observed = host.call('evidence_observed', pages=pages, plugin_instance_id='p')
    assert len(observed['receipt_ids']) == 1
    assert observed['rejected'] == [{'index': 0, 'reason': 'evidence_snapshot_expired'}]
    host.service = NativeService(tmp_path / 'registry.sqlite3')
    observed = host.call('evidence_observed', pages=pages, plugin_instance_id='p')
    assert len(observed['receipt_ids']) == 1 and observed['rejected'][0]['index'] == 0
    second = host.call('query', collection='evidence', cursor=current['cursor'], limit=127)
    assert second['draft_id'] == draft['draft_id'] and second['offset'] == 127
