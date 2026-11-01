"""Regression cases for PR 11 review findings."""

import argparse
import hashlib
import json
import sqlite3

import pytest

from auto_research import native_store, schema15
from auto_research.maintenance_cli import execute
from test_opencode_host import Host
from test_context_policy import completed
from test_evidence_conclusion import prepare_and_publish, fields, deliver_draft, deliver_material
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


def test_completed_project_can_disable_exploration_for_next_generation(completed):
    host, _, _, _, claim, _ = completed
    host.call('exploration_policy', mode='on')
    draft, publication = prepare_and_publish(host, claim)
    result = host.call('conclude', fields={'draft_id':draft['draft_id'],
        'final_ref':f"pub/{publication['publication_id']}#report",'summary':'Completed'},
        delivery_receipts=deliver_draft(host, draft['draft_id']), plugin_instance_id='plugin-1')
    policy = host.call('exploration_policy', mode='off')
    assert policy['effective_for'] == 'next_generation' and policy['conclusion_contract'] == 'evidence-v1'
    host.call('workflow', action='run', fields={'state':'running','new_generation':True})
    assert host.call('conclusion_contract')['conclusion_contract'] == 'legacy-v1'
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert db.execute('SELECT conclusion_id FROM project_conclusions WHERE generation=0').fetchone()[0] == result['conclusion_id']


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
            'part': page.get('part', 'manifest'), 'offset': page['offset'], 'end': page['end'],
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


def test_large_material_can_be_delivered_across_requests_without_large_draft(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('focus', node_id=None, role='planner', mode='manual')
    text = ''.join(f'{i:06d} workload=synthetic sample={i*31%65521} latency_ns={i*97%99991}\n' for i in range(40000))
    (host.root / 'measurements.txt').write_text(text, encoding='utf-8')
    source = host.call('publish', status='complete', summary='measurements', gaps=[], knowledge_refs=[],
        items=[{'item_id':'data', 'kind':'text', 'source_path':'measurements.txt'}])
    ref = f"pub/{source['publication_id']}#data"
    claim = host.call('memory_write', action='record', fields={'kind':'claim','statement':'Measured synthetic workload',
        'visibility':'project','evidence_refs':[ref]})
    host.call('conclusion_contract', contract='evidence-v1')
    view = host.call('query', collection='evidence', refs=[claim['ref']])
    declaration = {**fields([claim['ref']]), 'snapshot_id':view['snapshot_id']}
    assert 'Core evidence' in host.error('conclusion_prepare', fields=declaration, model_call=True)
    first = host.call('query', collection='evidence', material_ref=ref, limit=16384)
    partial = {'snapshot_id':first['snapshot_id'], 'part':'material', 'material_ref':ref,
               'offset':first['offset'],'end':first['end'],'manifest_digest':first['manifest_digest'],
               'chunk_digest':hashlib.sha256(first['chunk'].encode()).hexdigest()}
    assert host.call('evidence_observed', pages=[partial], plugin_instance_id='plugin-1')['rejected']
    host.call('evidence_observed', pages=[{**partial,'model_message_id':'step-partial'}], plugin_instance_id='plugin-1')
    incomplete = host.call('query', collection='evidence', refs=[claim['ref']])
    assert 'Core evidence' in host.error('conclusion_prepare', model_call=True,
        fields={**declaration,'snapshot_id':incomplete['snapshot_id']})
    assert deliver_material(host, ref) == text
    view = host.call('query', collection='evidence', refs=[claim['ref']])
    draft = host.call('conclusion_prepare', fields={**declaration, 'snapshot_id':view['snapshot_id']}, model_call=True)
    page = host.call('query', collection='evidence', draft_id=draft['draft_id'], limit=16384)
    assert page['total_bytes'] <= 32000
    assert text[:100] not in page['chunk']
    final = host.call('publish', status='complete', summary='final', gaps=[], knowledge_refs=[claim['ref']],
        items=[{'item_id':'report','kind':'text','content':{'text':'Measured synthetic workload'}}],
        draft_id=draft['draft_id'], report_item_id='report')
    receipts = deliver_draft(host, draft['draft_id'])
    result = host.call('conclude', fields={'draft_id':draft['draft_id'],
        'final_ref':f"pub/{final['publication_id']}#report",'summary':'Measured workload'},
        delivery_receipts=receipts, plugin_instance_id='plugin-1')
    assert result['outcome'] == 'answered'


def test_material_unicode_pagination_and_generation_isolation(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('focus', node_id=None, role='planner', mode='manual')
    text = '温度😀\r\n下一行🌡\r\n'
    (host.root / 'data.txt').write_bytes(text.encode())
    source = host.call('publish', status='complete', summary='data', gaps=[], knowledge_refs=[],
        items=[{'item_id':'data','kind':'text','source_path':'data.txt'}])
    ref = f"pub/{source['publication_id']}#data"
    claim = host.call('memory_write', action='record', fields={'kind':'claim','statement':'Temperature',
        'visibility':'project','evidence_refs':[ref]})
    host.call('conclusion_contract', contract='evidence-v1')
    assert deliver_material(host, ref, limit=7) == text
    ready = host.call('query', collection='evidence', refs=[claim['ref']])
    host.call('conclusion_prepare', model_call=True, fields={**fields([claim['ref']]), 'snapshot_id':ready['snapshot_id']})
    host.call('workflow', action='run', fields={'state':'running','new_generation':True})
    host.call('conclusion_contract', contract='evidence-v1')
    fresh = host.call('query', collection='evidence', refs=[claim['ref']])
    assert 'Core evidence' in host.error('conclusion_prepare', model_call=True,
        fields={**fields([claim['ref']]), 'snapshot_id':fresh['snapshot_id']})


def test_large_inline_material_is_separate_from_decision_package(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('focus', node_id=None, role='planner', mode='manual')
    content = {'text': '温度😀\r\n' * 12000}
    source = host.call('publish', status='complete', summary='inline data', gaps=[], knowledge_refs=[],
        items=[{'item_id':'data','kind':'text','content':content}])
    ref = f"pub/{source['publication_id']}#data"
    claim = host.call('memory_write', action='record', fields={'kind':'claim','statement':'Measured',
        'visibility':'project','evidence_refs':[ref]})
    host.call('conclusion_contract', contract='evidence-v1')
    assert json.loads(deliver_material(host, ref, limit=127)) == content
    view = host.call('query', collection='evidence', refs=[claim['ref']])
    draft = host.call('conclusion_prepare', model_call=True, fields={**fields([claim['ref']]),'snapshot_id':view['snapshot_id']})
    page = host.call('query', collection='evidence', draft_id=draft['draft_id'])
    assert page['total_bytes'] < 32000 and content['text'][:100] not in page['chunk']
