"""Multi-scope closure and complete, explicitly paged decision delivery."""

import hashlib
import json

import pytest

from auto_research.errors import ConflictError
from auto_research.evidence_conclusion import delivery_manifest
from auto_research.evidence_read import build_evidence_view, scope_work_basis
from test_context_policy import completed
from test_evidence_conclusion import fields
from test_exploration_foundation import scope_and_route
from test_opencode_host import Host


def another_scope(host):
    return host.call('exploration_scope', model_call=True, fields={'changes': {
        'objective': 'Independent throughput question', 'task_kind': 'selection'}})


def scope_judgment(scopes):
    return {'scope_refs': [item['ref'] for item in scopes], 'reason': 'Stop within the reported scope',
            'remaining_work': 'Unmeasured configurations remain outside this answer',
            'residual_uncertainty': 'No claim about every possible configuration',
            'reactivation_condition': 'A new workload or constraint requires another measurement'}


def test_project_evidence_contains_all_scopes_and_local_work_ignores_unrelated_changes(completed):
    host, store, _, _, claim, _ = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    scope, route = scope_and_route(host)
    route = host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'],
        'route_ref': route['ref'], 'reason': 'Bind the measured foundation',
        'changes': {'evidence_refs': [claim['ref']]}})
    def basis():
        with store._read() as db:
            return scope_work_basis(store, db, [], scope['scope_id'], [route['ref']])
    approved = basis()
    other = another_scope(host)
    host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'changes': {
        'title': 'Unselected idea', 'rationale': 'Keep an alternative', 'critical_assumption': 'Unknown'}})
    store._mutate('test.result', {}, 'new-result', lambda db: store._exploration_result_in_scope(
        db, scope['scope_id'], 'late-source', 'published', None, {'observation': 'arrived later'}))
    assert basis() == approved
    with store._read() as db:
        global_view = build_evidence_view(store, db, [])['view']
        local = build_evidence_view(store, db, [], scope_id=scope['scope_id'])['view']
    assert {item['ref'] for item in global_view['scopes']} == {scope['ref'], other['ref']}
    assert [item['ref'] for item in local['scopes']] == [scope['ref']]
    assert len(global_view['pending_results']) == 1
    store.revise_knowledge({'ref': claim['ref'], 'expected_revision': 1,
        'changes': {'status': 'retracted'}, 'reason': 'New evidence invalidates the source',
        'change_kind': 'retract', 'affected_scope_mode': 'versions', 'affected_scope': [claim['ref']]}, 'withdraw-e3')
    assert basis() != approved


def test_project_review_reconciliation_keeps_evidence_from_both_active_scopes(completed):
    host, store, _, _, claim, publication = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    host.call('exploration_policy', mode='on')
    second_claim = store.record_knowledge({'kind': 'claim', 'statement': 'A different measured claim',
        'visibility': 'project', 'evidence_refs': [f"pub/{publication['publication_id']}#report"]}, 'second-scope-claim')
    scopes = [host.call('exploration_scope', model_call=True, fields={'changes': {
        'objective': f'Question {index}', 'task_kind': 'selection'}}) for index in range(2)]
    for index, scope in enumerate(scopes):
        route = host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'changes': {
            'title': f'Candidate {index}', 'rationale': 'Evaluate a distinct question',
            'critical_assumption': 'The evidence applies',
            'evidence_refs': [[claim['ref'], second_claim['ref']][index]]}})
        snapshot = host.call('query', collection='evidence', refs=[], scope_id=scope['scope_id'])
        host.call('exploration_decide', model_call=True, fields={'scope_ref': scope['ref'],
            'snapshot_id': snapshot['snapshot_id'], 'route_refs': [route['ref']], 'action': 'probe',
            'reason': 'Consider this route', 'route_changes': [{'route_ref': route['ref'],
                'disposition': 'investigating', 'reason': 'The route is in use'}]})
    obligations = store.review_page('review_obligations')['items']
    active = {item['target_ref'] for item in obligations if item['scope_owner'] == 'project'
              and item['state'] != 'out_of_scope'}
    assert {claim['ref'], second_claim['ref']} <= active


def test_multi_scope_stop_is_complete_and_local_snapshot_cannot_close_project(tmp_path):
    host = Host(tmp_path)
    host.open()
    scope, route = scope_and_route(host)
    other = another_scope(host)
    store = host.service.stores[str(host.root)]
    page = host.service.evidence.page(store, refs=[], host='opencode', session='ses_main', scope_id=scope['scope_id'])
    local = host.service.evidence.get(store, page['snapshot_id'])
    with pytest.raises(ConflictError, match='project_evidence_required'):
        store.prepare_conclusion('opencode', 'ses_main', fields([], 'partial'), local, 'local-cannot-close')
    page = host.call('query', collection='evidence', refs=[])
    declaration = {**fields([], 'partial'), 'snapshot_id': page['snapshot_id']}
    assert 'Every current scope' in host.error('conclusion_prepare', fields=declaration, model_call=True)
    declaration['exploration_stop']['scope_judgments'] = [scope_judgment([scope, other])]
    prepared = host.call('conclusion_prepare', fields=declaration, model_call=True)
    assert set(prepared['exploration_stop']['considered_scope_refs']) == {scope['ref'], other['ref']}
    host.call('exploration_scope', model_call=True, fields={'scope_ref': other['ref'],
        'reason': 'Revise the independent objective', 'changes': {'objective': 'Updated throughput question'}})
    assert 'conclusion_basis_changed' in host.error('conclusion_prepare', fields=declaration, model_call=True)


def test_latest_choice_invalidates_project_draft_but_not_approved_work(completed):
    host, store, _, _, claim, _ = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    scope, route = scope_and_route(host)
    page = host.call('query', collection='evidence', refs=[claim['ref']])
    declaration = {**fields([claim['ref']]), 'snapshot_id': page['snapshot_id']}
    declaration['exploration_stop']['route_judgments'] = [{'route_refs': [route['ref']],
        'reason': 'An alternative is not needed for this narrow answer', 'reactivation_condition': 'A new constraint requires comparison'}]
    draft = host.call('conclusion_prepare', model_call=True, fields=declaration)
    with store._read() as db:
        work_before = scope_work_basis(store, db, [claim['ref']], scope['scope_id'], [route['ref']])
    fresh = host.call('query', collection='evidence', refs=[claim['ref']])
    choice = host.call('exploration_decide', model_call=True, fields={'scope_ref': scope['ref'],
        'snapshot_id': fresh['snapshot_id'], 'route_refs': [route['ref']], 'action': 'propose_stop',
        'reason': 'A later explicit stopping choice', 'provisional': True, 'missing_results': ['The unrun alternative remains unknown']})
    with store._read() as db:
        view = build_evidence_view(store, db, [claim['ref']])
        assert scope_work_basis(store, db, [claim['ref']], scope['scope_id'], [route['ref']]) == work_before
    assert view['view']['scope_decisions'][0]['decision_id'] == choice['decision_id']
    assert 'conclusion_basis_changed' in host.error('conclusion_prepare', model_call=True, fields=declaration)
    # Both old and new complete packages remain readable; an old layout cannot authorize a new basis.
    assert host.call('query', collection='evidence', draft_id=draft['draft_id'])['draft_id'] == draft['draft_id']


def test_narrowing_retains_excluded_core_risks_in_subsequent_views(completed):
    host, store, _, _, claim, _ = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    host.call('conclusion_contract', contract='evidence-v1')
    page = host.call('query', collection='evidence', refs=[claim['ref']])
    host.call('conclusion_prepare', model_call=True, fields={**fields([claim['ref']]), 'snapshot_id': page['snapshot_id']})
    page = host.call('query', collection='evidence', refs=[])
    narrowed = {**fields([], 'partial'), 'snapshot_id': page['snapshot_id'],
        'scope_change': {'reason': 'Deliver only a narrower answer', 'excluded_impact': 'The original result remains outside this answer'}}
    draft = host.call('conclusion_prepare', model_call=True, fields=narrowed)
    with store._read() as db:
        before = build_evidence_view(store, db, [])
    assert before['view']['scope_exclusions'][0]['removed'] == [claim['ref']]
    assert claim['ref'] in before['view']['retained_core_refs']
    store.revise_knowledge({'ref': claim['ref'], 'expected_revision': 1,
        'changes': {'status': 'retracted'}, 'reason': 'Counterexample arrives outside the narrowed answer',
        'change_kind': 'retract', 'affected_scope_mode': 'versions', 'affected_scope': [claim['ref']]}, 'excluded-withdraw')
    with store._read() as db:
        after = build_evidence_view(store, db, [])
    assert before['basis_digest'] != after['basis_digest']
    assert any(item['current']['status'] == 'retracted' for item in after['view']['records'])
    saved = host.service.evidence.draft_snapshot(store, draft['draft_id'])
    assert saved['view']['scope_exclusions'][0]['reason'] == narrowed['scope_change']['reason']


def test_closed_scope_declared_counterevidence_remains_in_project_scientific_graph(completed):
    host, store, _, _, claim, _ = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    scope, route = scope_and_route(host)
    host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'route_ref': route['ref'],
        'reason': 'Preserve the declared observation', 'changes': {'evidence_refs': [claim['ref']]}})
    host.call('exploration_scope', model_call=True, fields={'scope_ref': scope['ref'],
        'reason': 'Set aside this scope while preserving its evidence',
        'changes': {'state': 'closed', 'reactivation_condition': 'New workload or counterevidence'}})
    another_scope(host)
    with store._read() as db:
        before = build_evidence_view(store, db, [])
    assert before['view']['roots'] == []
    assert any(item['ref'] == claim['ref'] for item in before['view']['records'])
    store.revise_knowledge({'ref': claim['ref'], 'expected_revision': 1, 'changes': {'status': 'retracted'},
        'reason': 'A relevant counterexample arrives', 'change_kind': 'retract', 'affected_scope_mode': 'versions',
        'affected_scope': [claim['ref']]}, 'closed-scope-withdraw')
    with store._read() as db:
        after = build_evidence_view(store, db, [])
    assert before['basis_digest'] != after['basis_digest']
    assert any(item['current']['status'] == 'retracted' for item in after['view']['records'])


@pytest.mark.parametrize('kind', ['route', 'scope'])
def test_removing_declared_scientific_refs_retains_history_and_later_withdrawal_risks(completed, kind):
    host, store, _, _, claim, _ = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    scope, route = scope_and_route(host)
    if kind == 'route':
        owner = host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'route_ref': route['ref'],
            'reason': 'Declare the counterexample', 'changes': {'evidence_refs': [claim['ref']]}})
        host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'route_ref': owner['ref'],
            'reason': 'Narrow the route but preserve the counterexample history', 'changes': {'evidence_refs': []}})
    else:
        owner = host.call('exploration_scope', model_call=True, fields={'scope_ref': scope['ref'],
            'reason': 'Declare the baseline observation', 'changes': {'baseline_refs': [claim['ref']]}})
        host.call('exploration_scope', model_call=True, fields={'scope_ref': owner['ref'],
            'reason': 'Use a narrower baseline without erasing its risks', 'changes': {'baseline_refs': []}})
    with store._read() as db:
        before = build_evidence_view(store, db, [])
    excluded = before['view']['exploration_exclusions']
    assert len(excluded) == 1 and excluded[0]['kind'] == kind
    assert excluded[0]['removed_refs'] == [claim['ref']]
    assert excluded[0]['before_ref'] == owner['ref'] and excluded[0]['reason_status'] == 'recorded'
    assert any(item['ref'] == claim['ref'] for item in before['view']['records'])
    store.revise_knowledge({'ref': claim['ref'], 'expected_revision': 1, 'changes': {'status': 'retracted'},
        'reason': 'Counterevidence invalidates a historical declared source', 'change_kind': 'retract',
        'affected_scope_mode': 'versions', 'affected_scope': [claim['ref']]}, 'removed-source-withdraw')
    with store._read() as db:
        after = build_evidence_view(store, db, [])
        local = build_evidence_view(store, db, [], scope_id=scope['scope_id'])['view']
    assert before['basis_digest'] != after['basis_digest']
    assert any(item['target'] == claim['ref'] for item in after['view']['risks'])
    assert local['exploration_exclusions'] == []


def test_explicit_large_decision_manifest_survives_restart_and_requires_every_byte(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('focus', node_id=None, role='planner', mode='manual')
    source = host.call('publish', status='complete', summary='data', gaps=[], knowledge_refs=[],
        items=[{'item_id': 'data', 'kind': 'text', 'content': {'measured': 7}}])
    claim = host.call('memory_write', action='record', fields={'kind': 'claim', 'statement': 'Measured: ' + '结果🙂' * 4500,
        'visibility': 'project', 'evidence_refs': [f"pub/{source['publication_id']}#data"]})['ref']
    host.call('conclusion_contract', contract='evidence-v1')
    page = host.call('query', collection='evidence', refs=[claim])
    declaration = {**fields([claim]), 'snapshot_id': page['snapshot_id']}
    assert 'decision_package_too_large' in host.error('conclusion_prepare', model_call=True, fields=declaration)
    draft = host.call('conclusion_prepare', model_call=True, fields={**declaration, 'delivery_mode': 'paged'})
    manifest = draft['delivery_manifest']
    assert manifest['total_bytes'] > 32000 and len(manifest['pages']) > 2
    store = host.service.stores[str(host.root)]
    final = host.call('publish', status='complete', summary='final', gaps=[], knowledge_refs=[claim],
        items=[{'item_id': 'report', 'kind': 'text', 'content': {'text': 'Independent summary of the measured result'}}],
        draft_id=draft['draft_id'], report_item_id='report')
    first = host.call('query', collection='evidence', draft_id=draft['draft_id'], limit=16384)
    # Cache loss models the persistent backend restart path, including its cursor.
    host.service.evidence._snapshots.clear()
    pages, chunks, page = [], [], first
    while True:
        assert page['delivery_manifest'] == manifest
        chunks.append(page['chunk'])
        pages.append({'snapshot_id': page['snapshot_id'], 'draft_id': draft['draft_id'], 'part': 'decision',
            'offset': page['offset'], 'end': page['end'], 'manifest_digest': page['manifest_digest'],
            'chunk_digest': hashlib.sha256(page['chunk'].encode()).hexdigest()})
        if not page['cursor']:
            break
        page = host.call('query', collection='evidence', cursor=page['cursor'], limit=16384)
    body = ''.join(chunks).encode()
    assert delivery_manifest(body) == manifest
    for layout in manifest['pages']:
        assert layout['end'] - layout['offset'] <= 16384
        assert hashlib.sha256(body[layout['offset']:layout['end']]).hexdigest() == layout['digest']
        body[layout['offset']:layout['end']].decode('utf-8')
    args = {'draft_id': draft['draft_id'], 'final_ref': f"pub/{final['publication_id']}#report", 'summary': 'Measured within the declared conditions'}
    partial = host.call('evidence_observed', pages=pages[:-1], plugin_instance_id='e3-plugin')['receipt_ids']
    assert 'Complete evidence' in host.error('conclude', fields=args, delivery_receipts=partial, plugin_instance_id='e3-plugin')
    complete = host.call('evidence_observed', pages=pages, plugin_instance_id='e3-plugin')['receipt_ids']
    assert host.call('conclude', fields=args, delivery_receipts=complete, plugin_instance_id='e3-plugin')['outcome'] == 'answered'
