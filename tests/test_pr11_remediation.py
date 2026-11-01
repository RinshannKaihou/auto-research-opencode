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


@pytest.mark.parametrize('source_kind', ['publication', 'snapshot'])
@pytest.mark.parametrize('same_content', [True, False])
def test_directory_child_and_final_file_compare_actual_bytes(tmp_path, source_kind, same_content):
    host = Host(tmp_path)
    host.open()
    host.call('focus', node_id=None, role='planner', mode='manual')
    bundle = host.root / 'bundle'
    bundle.mkdir()
    (bundle / 'source.txt').write_text('Frozen measurements', encoding='utf-8')
    if source_kind == 'publication':
        source = host.call('publish', status='complete', summary='bundle', gaps=[], knowledge_refs=[],
            items=[{'item_id':'bundle','kind':'directory','source_path':'bundle'}])
        ref = f"pub/{source['publication_id']}#bundle/source.txt"
    else:
        source = host.call('snapshot', paths=['bundle'])
        ref = f"{source['snapshot_id']}#bundle/source.txt"
    claim = host.call('memory_write', action='record', fields={'kind':'claim','statement':'Measured',
        'visibility':'project','evidence_refs':[ref]})
    host.call('conclusion_contract', contract='evidence-v1')
    deliver_material(host, ref)
    view = host.call('query', collection='evidence', refs=[claim['ref']])
    draft = host.call('conclusion_prepare', model_call=True, fields={**fields([claim['ref']]),'snapshot_id':view['snapshot_id']})
    (host.root / 'final.txt').write_text('Frozen measurements' if same_content else 'Distinct final report', encoding='utf-8')
    publication = host.call('publish', status='complete', summary='final', gaps=[], knowledge_refs=[claim['ref']],
        items=[{'item_id':'report','kind':'report','source_path':'final.txt'}], draft_id=draft['draft_id'], report_item_id='report')
    args = {'fields':{'draft_id':draft['draft_id'],'final_ref':f"pub/{publication['publication_id']}#report",'summary':'Answer'},
            'delivery_receipts':deliver_draft(host,draft['draft_id']), 'plugin_instance_id':'plugin-1'}
    if same_content:
        assert 'cannot be treated as evidence for its own core claims' in host.error('conclude', **args)
    else:
        assert host.call('conclude', **args)['outcome'] == 'answered'


def test_review_cannot_copy_a_directory_child_as_an_independent_report(tmp_path):
    from test_r2_runtime import obligations
    host = Host(tmp_path)
    host.open()
    host.call('focus', node_id=None, role='planner', mode='manual')
    bundle = host.root / 'bundle'
    bundle.mkdir()
    (bundle / 'data.txt').write_text('Measured source', encoding='utf-8')
    source = host.call('publish', status='complete', summary='bundle', gaps=[], knowledge_refs=[],
        items=[{'item_id':'bundle','kind':'directory','source_path':'bundle'}])
    ref = f"pub/{source['publication_id']}#bundle/data.txt"
    claim = host.call('memory_write', action='record', fields={'kind':'claim','statement':'Measured',
        'visibility':'project','evidence_refs':[ref]})
    host.call('review_scope_set', refs=[claim['ref']], reason='review')
    store = host.service.stores[str(host.root)]
    obligation = next(row for row in obligations(store) if row['target_ref'] == claim['ref'])
    review = host.call('publish', status='complete', summary='review', gaps=[], knowledge_refs=[],
        items=[{'item_id':'review','kind':'text','source_path':'bundle/data.txt'}])
    error = host.error('relate', contract_version='r2-v1', source_ref=f"pub/{review['publication_id']}#review",
        target_ref=claim['ref'], obligation_id=obligation['obligation_id'], label='reviews', note='same source',
        result='supports', complete=True, review_type='citation_scope')
    assert 'already a source' in error


def test_publish_replay_and_invalid_input_do_not_freeze_an_appendix(completed, monkeypatch):
    from auto_research.artifacts import ArtifactStore
    host, _, _, _, claim, _ = completed
    host.call('conclusion_contract', contract='evidence-v1')
    view = host.call('query', collection='evidence', refs=[claim['ref']])
    draft = host.call('conclusion_prepare', model_call=True, fields={**fields([claim['ref']]),'snapshot_id':view['snapshot_id']})
    original = ArtifactStore.freeze
    frozen = []
    def freeze(self, path, allowed_root):
        frozen.append(str(path))
        return original(self, path, allowed_root)
    monkeypatch.setattr(ArtifactStore, 'freeze', freeze)
    args = {'model_call':True,'status':'complete','summary':'Final','gaps':[],'knowledge_refs':[claim['ref']],
            'items':[{'item_id':'report','kind':'text','content':{'text':'Final report'}}],
            'draft_id':draft['draft_id'],'report_item_id':'report','operation_id':'publish-once'}
    first = host.call('publish', **args)
    assert host.call('publish', **args) == first and len(frozen) == 1
    assert 'complete report item' in host.error('publish', **{**args,'status':'partial','operation_id':'bad-publish'})
    assert len(frozen) == 1


def test_traversal_limit_is_visible_and_cannot_authorize_answered(completed, monkeypatch):
    from auto_research import evidence_read
    host, _, _, _, claim, _ = completed
    host.call('conclusion_contract', contract='evidence-v1')
    monkeypatch.setattr(evidence_read, 'MAX_EVIDENCE_REFS', 1)
    page = host.call('query', collection='evidence', refs=[claim['ref']], limit=16384)
    view = json.loads(page['chunk'])
    assert not view['graph_complete']
    assert any(item['reason'] == 'evidence_traversal_limit' for item in view['limitations'])
    assert 'truncated' in host.error('conclusion_prepare', model_call=True,
        fields={**fields([claim['ref']]),'snapshot_id':page['snapshot_id']})
    partial = host.call('conclusion_prepare', model_call=True,
        fields={**fields([], 'partial'),'snapshot_id':page['snapshot_id']})
    assert partial['outcome'] == 'partial' and partial['evidence_closure']['status'] == 'unknown'


def test_review_event_limit_preserves_incompleteness(completed, monkeypatch):
    from auto_research import evidence_read
    from test_r2_runtime import obligations, link, report
    host, store, _, _, claim, _ = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    host.call('review_scope_set', refs=[claim['ref']], reason='review')
    obligation = next(row for row in obligations(store) if row['target_ref'] == claim['ref'])
    link(host, obligation, report(host), result='challenges', complete=False)
    link(host, obligation, report(host), result='challenges', complete=False)
    monkeypatch.setattr(evidence_read, 'MAX_EVIDENCE_REVIEWS', 1)
    page = host.call('query', collection='evidence', refs=[claim['ref']], limit=16384)
    view = json.loads(page['chunk'])
    assert not view['graph_complete']
    assert any(item['kind'] == 'review_challenge' for item in view['risks'])
    assert any(item['reason'] == 'evidence_traversal_limit' and item['detail']['kind'] == 'review_events'
               for item in view['limitations'])


def test_edge_limit_reports_overflow_without_rejecting_the_exact_boundary(tmp_path, monkeypatch):
    from auto_research import evidence_read
    host = Host(tmp_path)
    host.open()
    host.call('focus', node_id=None, role='planner', mode='manual')
    source = host.call('publish', status='complete', summary='basis', gaps=[], knowledge_refs=[],
        items=[{'item_id':'data','kind':'text','content':{'text':'measurements'}}])
    source_ref = f"pub/{source['publication_id']}#data"
    refs = [host.call('memory_write', action='record', fields={'kind':'claim','visibility':'project',
             'statement':f'Basis {i}', 'evidence_refs':[source_ref]})['ref'] for i in range(2)]
    root = host.call('memory_write', action='record', fields={'kind':'claim','visibility':'project',
                     'statement':'Combined', 'dependencies':refs, 'evidence_refs':[source_ref]})['ref']
    monkeypatch.setattr(evidence_read, 'MAX_EVIDENCE_EDGES', 1)
    truncated = json.loads(host.call('query',collection='evidence',refs=[root],limit=16384)['chunk'])
    assert any(item['reason']=='evidence_traversal_limit' and item['detail']['kind']=='edges' for item in truncated['limitations'])
    monkeypatch.setattr(evidence_read, 'MAX_EVIDENCE_EDGES', 5)
    complete = json.loads(host.call('query',collection='evidence',refs=[root],limit=16384)['chunk'])
    assert len(complete['edges']) == 5
    assert not any(item['reason']=='evidence_traversal_limit' and item['detail']['kind']=='edges' for item in complete['limitations'])


def test_exploration_context_discloses_omitted_details(tmp_path):
    from test_exploration_foundation import scope_and_route
    from test_context_policy import sections
    host = Host(tmp_path)
    host.open()
    scope_and_route(host)
    small = sections(host.call('memory_context',policy='memory-r1-v1',max_chars=900))['exploration']
    large = sections(host.call('memory_context',policy='memory-r1-v1',max_chars=2400))['exploration']
    assert small['details_omitted'] and small['routes']['collection'] == 'exploration_routes'
    assert not large['details_omitted'] and large['details']['route_sample']


def test_new_main_cannot_inherit_previous_main_material_snapshot(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('focus', node_id=None, role='planner', mode='manual')
    (host.root / 'data.txt').write_text('material available to the first main')
    source = host.call('publish', status='complete', summary='data', gaps=[], knowledge_refs=[],
        items=[{'item_id':'data','kind':'text','source_path':'data.txt'}])
    ref = f"pub/{source['publication_id']}#data"
    claim = host.call('memory_write', action='record', fields={'kind':'claim','statement':'Measured result',
        'visibility':'project','evidence_refs':[ref]})['ref']
    host.call('conclusion_contract', contract='evidence-v1')
    deliver_material(host, ref)
    old = host.call('query', collection='evidence', refs=[claim])
    host.call('takeover_main', session='ses_new', root=str(host.root), cwd=str(host.root))
    declaration = {**fields([claim]), 'snapshot_id':old['snapshot_id']}
    assert 'evidence_snapshot_session_changed' in host.error('conclusion_prepare', session='ses_new',
        fields=declaration, model_call=True)
    fresh = host.call('query', session='ses_new', collection='evidence', refs=[claim])
    assert 'Core evidence' in host.error('conclusion_prepare', session='ses_new', model_call=True,
        fields={**declaration, 'snapshot_id':fresh['snapshot_id']})


def test_decision_package_keeps_full_basis_in_draft_without_replaying_every_record(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('focus', node_id=None, role='planner', mode='manual')
    source = host.call('publish', status='complete', summary='data', gaps=[], knowledge_refs=[],
        items=[{'item_id':'data','kind':'text','content':{'text':'measured value'}}])
    ref = f"pub/{source['publication_id']}#data"
    dependencies = [host.call('memory_write',action='record',fields={'kind':'hypothesis',
        'statement':f'Background {index}: ' + 'context ' * 150,'visibility':'project'})['ref']
        for index in range(22)]
    claim = host.call('memory_write',action='record',fields={'kind':'claim','statement':'Measured result',
        'visibility':'project','evidence_refs':[ref],'dependencies':dependencies})['ref']
    host.call('conclusion_contract',contract='evidence-v1')
    view = host.call('query',collection='evidence',refs=[claim],limit=16384)
    assert view['total_chars'] > 32000
    draft = host.call('conclusion_prepare',model_call=True,
        fields={**fields([claim]),'snapshot_id':view['snapshot_id']})
    page = host.call('query',collection='evidence',draft_id=draft['draft_id'],limit=16384)
    assert page['total_bytes'] < 32000
    package = json.loads(page['chunk'])
    assert [record['ref'] for record in package['records']] == [claim]
    assert len(package['edges']) == len(dependencies) + 1
    assert package['materials'][0]['inline_content'] == {'text':'measured value'}
    with sqlite3.connect(host.root/'.research/state.sqlite3') as db:
        saved = json.loads(db.execute('SELECT data FROM conclusion_drafts WHERE draft_id=?',(draft['draft_id'],)).fetchone()[0])
    assert len(saved['view']['records']) == len(dependencies) + 1
    final = host.call('publish',status='complete',summary='final',gaps=[],knowledge_refs=[claim],
        items=[{'item_id':'report','kind':'text','content':{'text':'Independent summary'}}],
        draft_id=draft['draft_id'],report_item_id='report')
    conclusion = host.call('conclude',fields={'draft_id':draft['draft_id'],
        'final_ref':f"pub/{final['publication_id']}#report",'summary':'Measured result'},
        delivery_receipts=deliver_draft(host,draft['draft_id']),plugin_instance_id='plugin-1')
    assert conclusion['outcome'] == 'answered'


def test_large_inline_copy_cannot_support_its_own_final_report(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('focus',node_id=None,role='planner',mode='manual')
    content = {'text':'Measurements reported independently. ' * 100}
    source = host.call('publish',status='complete',summary='data',gaps=[],knowledge_refs=[],
        items=[{'item_id':'data','kind':'text','content':content}])
    ref = f"pub/{source['publication_id']}#data"
    claim = host.call('memory_write',action='record',fields={'kind':'claim','statement':'Measured result',
        'visibility':'project','evidence_refs':[ref]})['ref']
    host.call('conclusion_contract',contract='evidence-v1')
    deliver_material(host,ref)
    view = host.call('query',collection='evidence',refs=[claim])
    draft = host.call('conclusion_prepare',model_call=True,fields={**fields([claim]),'snapshot_id':view['snapshot_id']})
    final = host.call('publish',status='complete',summary='copied final',gaps=[],knowledge_refs=[claim],
        items=[{'item_id':'report','kind':'text','content':content}],draft_id=draft['draft_id'],report_item_id='report')
    message = host.error('conclude',fields={'draft_id':draft['draft_id'],
        'final_ref':f"pub/{final['publication_id']}#report",'summary':'Copied'},
        delivery_receipts=deliver_draft(host,draft['draft_id']),plugin_instance_id='plugin-1')
    assert 'own core claims' in message


def test_new_draft_freezes_new_material_coverage_and_needs_its_own_receipts(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('focus',node_id=None,role='planner',mode='manual')
    (host.root/'data.txt').write_text('The measured source material')
    source = host.call('publish',status='complete',summary='data',gaps=[],knowledge_refs=[],
        items=[{'item_id':'data','kind':'text','source_path':'data.txt'}])
    ref = f"pub/{source['publication_id']}#data"
    claim = host.call('memory_write',action='record',fields={'kind':'claim','statement':'Measured result',
        'visibility':'project','evidence_refs':[ref]})['ref']
    host.call('conclusion_contract',contract='evidence-v1')
    before = host.call('query',collection='evidence',refs=[claim])
    old = host.call('conclusion_prepare',model_call=True,
        fields={**fields([claim],'partial'),'snapshot_id':before['snapshot_id']})
    deliver_material(host,ref)
    after = host.call('query',collection='evidence',refs=[claim])
    new = host.call('conclusion_prepare',model_call=True,
        fields={**fields([claim]),'snapshot_id':after['snapshot_id']})
    old_page = host.call('query',collection='evidence',draft_id=old['draft_id'],limit=16384)
    new_page = host.call('query',collection='evidence',draft_id=new['draft_id'],limit=16384)
    assert not json.loads(old_page['chunk'])['materials'][0]['material_read']['complete']
    assert json.loads(new_page['chunk'])['materials'][0]['material_read']['complete']
    final = host.call('publish',status='complete',summary='final',gaps=[],knowledge_refs=[claim],
        items=[{'item_id':'report','kind':'text','content':{'text':'Independent final summary'}}],
        draft_id=new['draft_id'],report_item_id='report')
    old_receipts, new_receipts = deliver_draft(host,old['draft_id']), deliver_draft(host,new['draft_id'])
    args = {'draft_id':new['draft_id'],'final_ref':f"pub/{final['publication_id']}#report",'summary':'Measured'}
    assert 'Complete evidence' in host.error('conclude',fields=args,delivery_receipts=old_receipts,plugin_instance_id='plugin-1')
    assert host.call('conclude',fields=args,delivery_receipts=old_receipts+new_receipts,
                     plugin_instance_id='plugin-1')['outcome'] == 'answered'


def test_material_coverage_unions_duplicate_and_overlapping_ranges(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('focus',node_id=None,role='planner',mode='manual')
    (host.root/'data.txt').write_text('0123456789')
    source = host.call('publish',status='complete',summary='data',gaps=[],knowledge_refs=[],
        items=[{'item_id':'data','kind':'text','source_path':'data.txt'}])
    ref = f"pub/{source['publication_id']}#data"
    first = host.call('query',collection='evidence',material_ref=ref,limit=6)
    overlap = host.call('query',collection='evidence',cursor={**first['cursor'],'offset':3},limit=5)
    def observed(page):
        return {**evidence_page(page),'part':'material','material_ref':ref,'model_message_id':'step'}
    host.call('evidence_observed',pages=[observed(first),observed(first),observed(overlap)],plugin_instance_id='plugin-1')
    partial = json.loads(host.call('query',collection='evidence',refs=[ref],limit=16384)['chunk'])['materials'][0]
    assert partial['material_read'] == {'complete':False,'covered_bytes':8}
    last = host.call('query',collection='evidence',cursor=overlap['cursor'],limit=4)
    host.call('evidence_observed',pages=[observed(last)],plugin_instance_id='plugin-1')
    complete = json.loads(host.call('query',collection='evidence',refs=[ref],limit=16384)['chunk'])['materials'][0]
    assert complete['material_read'] == {'complete':True,'covered_bytes':10}


def test_oversized_decision_package_reports_size_and_scope_counts(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('focus',node_id=None,role='planner',mode='manual')
    source = host.call('publish',status='complete',summary='data',gaps=[],knowledge_refs=[],
        items=[{'item_id':'data','kind':'text','content':{'text':'measured value'}}])
    claim = host.call('memory_write',action='record',fields={'kind':'claim','statement':'Measured '+('x'*33000),
        'visibility':'project','evidence_refs':[f"pub/{source['publication_id']}#data"]})['ref']
    host.call('conclusion_contract',contract='evidence-v1')
    view = host.call('query',collection='evidence',refs=[claim])
    error = host.error('conclusion_prepare',model_call=True,
        fields={**fields([claim]),'snapshot_id':view['snapshot_id']})
    assert 'decision_package_too_large' in error and 'limit 32000' in error
    assert '"records": 1' in error and '"materials": 1' in error
    assert 'narrow the declared scope explicitly' in error
    with sqlite3.connect(host.root/'.research/state.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM conclusion_drafts').fetchone()[0] == 0
