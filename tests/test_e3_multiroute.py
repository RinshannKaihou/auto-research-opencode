"""Selections are versioned scientific choices, independent of execution speed."""

import sqlite3
import json
from pathlib import Path

import pytest

from test_context_policy import completed
from test_opencode_host import Host
from auto_research.evidence_read import scope_work_basis
from test_evidence_conclusion import fields as conclusion_fields
from test_r2_runtime import link, obligations, report


def setup(host):
    host.open()
    host.call('workflow', action='run', fields={'state': 'running'})
    host.call('exploration_policy', mode='on')
    scope = host.call('exploration_scope', model_call=True, fields={'changes': {
        'objective': 'Choose a bounded implementation', 'task_kind': 'selection'}})
    routes = [host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'changes': {
        'title': title, 'rationale': 'Test a different mechanism', 'critical_assumption': 'Must measure this'}})
        for title in ('A', 'B')]
    return scope, routes


def decision(host, scope, routes, **fields):
    page = host.call('query', collection='evidence', refs=[], scope_id=scope['scope_id'])
    return host.call('exploration_decide', model_call=True, fields={
        'scope_ref': scope['ref'], 'route_refs': [route['ref'] for route in routes],
        'snapshot_id': page['snapshot_id'], 'action': 'probe', 'reason': 'An informative next step', **fields})


def work(routes, key='probe', **fields):
    return {'key': key, 'route_refs': [route['ref'] for route in routes], 'question': key,
        'why_now': 'Resolve a choice', 'plan': 'Bounded measurement', 'root_reason': 'Independent measurement', **fields}


def publish(host, task, session, content):
    host.call('workflow', action='task_session', fields={'task_id': task['task_id'], 'session_id': session})
    host.call('open', session=session, root=str(host.root), cwd=str(host.root), session_role='node_core', node_id=task['node_id'])
    host.call('focus', session=session, node_id=task['node_id'], role='core', mode='auto')
    result = host.call('publish', session=session, status='complete', summary=content, gaps=[], knowledge_refs=[],
        items=[{'item_id': 'result', 'kind': 'text', 'content': {'text': content}}])
    host.call('finish', session=session, state='finished', details={'reason': content})
    return 'pub/' + result['publication_id'] + '#result'


def current(host, scope):
    return host.call('query', collection='exploration_routes', scope_id=scope['scope_id'])['items']


def pending(host, scope):
    return [item['result_id'] for item in host.call('query', collection='exploration_results', scope_id=scope['scope_id'])['items']
            if item['handled_by_decision'] is None]


def test_shared_probe_one_intent_node_and_reservation_and_new_candidate_does_not_cancel(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    host.call('runtime_budget_set', limits={'shared_work_budget': 1}, reason='one shared probe')
    chosen = decision(host, scope, routes, work_batch=[work(routes)])
    assert len(chosen['intent_ids']) == 1
    # A new candidate is not a dependency of this already approved probe.
    host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'changes': {
        'title': 'C', 'rationale': 'Later alternative', 'critical_assumption': 'Unknown'}})
    task = host.call('exploration_materialize', intent_id=chosen['intent_id'])
    assert task['state'] == 'materialized'
    assert host.call('node_launch_claim', task_id=task['task_id'], concurrency=2)['claimed']
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM exploration_bindings').fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM runtime_budget_reservations WHERE kind='exploration_work'").fetchone()[0] == 1


def test_async_fast_choice_is_provisional_and_late_evidence_revises_without_cancelling_slow(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work([routes[0]], 'fast'), work([routes[1]], 'slow')])
    fast, slow = [host.call('exploration_materialize', intent_id=ident) for ident in chosen['intent_ids']]
    fast_ref = publish(host, fast, 'ses_fast', 'A helps on the initial workload')
    routes = current(host, scope)
    provisional = decision(host, scope, routes, expected_results=chosen['intent_ids'], provisional=True,
        result_ids=pending(host, scope), route_changes=[{'route_ref': routes[0]['ref'], 'disposition': 'adopted', 'reason': 'Fast bounded result'}])
    assert len(provisional['expected_missing']) == 1
    assert host.call('exploration_task_check', task_id=slow['task_id'])['allowed']
    slow_ref = publish(host, slow, 'ses_slow', 'B performs better on the target workload')
    revised = decision(host, scope, current(host, scope), expected_results=chosen['intent_ids'],
        result_ids=pending(host, scope), revises_decision_id=provisional['decision_id'], action='redirect')
    assert revised['expected_missing'] == []
    history = host.call('query', collection='exploration_decisions')['items']
    assert history[1]['data']['provisional'] is True
    assert history[2]['data']['revises_decision_id'] == history[1]['decision_id']
    assert fast_ref != slow_ref


def test_incomparable_results_cannot_be_ranked_and_failure_is_missing_evidence(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work([routes[0]], 'A'), work([routes[1]], 'B')])
    tasks = [host.call('exploration_materialize', intent_id=ident) for ident in chosen['intent_ids']]
    refs = [publish(host, task, f'ses_{index}', f'result {index}') for index, task in enumerate(tasks)]
    conditions = [{'baseline_ref': refs[0], 'workload': load, 'metric': 'p99', 'unit': 'ms',
        'environment': 'device-1', 'repeats': 3, 'uncertainty': 'wide'} for load in ('dev', 'production')]
    snapshot = host.call('query', collection='evidence', refs=[], scope_id=scope['scope_id'])
    fields = {'scope_ref': scope['ref'], 'route_refs': [route['ref'] for route in current(host, scope)],
        'snapshot_id': snapshot['snapshot_id'], 'action': 'redirect', 'reason': 'Compare measurements',
        'comparisons': [{'result_refs': refs, 'conditions': conditions, 'ranking': True, 'reason': 'Different loads'}]}
    assert 'incomparable_results' in host.error('exploration_decide', model_call=True, fields=fields)
    fields['comparisons'][0]['ranking'] = False
    result = host.call('exploration_decide', model_call=True, fields=fields)
    assert result['comparisons'][0]['status'] == 'incomparable'
    assert result['comparisons'][0]['differences'] == ['workload']


def test_combination_is_a_new_experiment_and_deferred_route_requires_changed_conditions(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, next_work=work(routes))
    shared = host.call('exploration_materialize', intent_id=chosen['intent_id'])
    baseline = publish(host, shared, 'ses_calibrate', 'Frozen standalone baseline and calibration')
    routes = current(host, scope)
    combo = host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'changes': {
        'title': 'A+B', 'rationale': 'Interaction must be measured', 'critical_assumption': 'May interfere',
        'component_refs': [route['ref'] for route in routes], 'baseline_refs': [baseline]}})
    combined = decision(host, scope, [*routes, combo], action='combine', result_ids=pending(host, scope),
        next_work=work([combo], 'combined', work_kind='combine', inputs=[baseline]))
    experiment = host.call('exploration_materialize', intent_id=combined['intent_id'])
    regression = publish(host, experiment, 'ses_combo', 'Combination regresses; standalone baseline retained')
    routes = current(host, scope)
    deferred = decision(host, scope, routes, action='defer', result_ids=pending(host, scope), route_changes=[{
        'route_ref': routes[0]['ref'], 'disposition': 'deferred', 'reason': 'Low concurrency is insufficient',
        'reactivation_condition': 'Higher concurrency changes the tradeoff'}])
    routes = current(host, scope)
    snap = host.call('query', collection='evidence', refs=[], scope_id=scope['scope_id'])
    fields = {'scope_ref': scope['ref'], 'route_refs': [route['ref'] for route in routes], 'snapshot_id': snap['snapshot_id'],
        'action': 'restore', 'reason': 'Restore baseline', 'route_changes': [{
            'route_ref': routes[0]['ref'], 'disposition': 'investigating', 'reason': 'New concurrency evidence'}],
        'next_work': work([routes[0]], 'restore', work_kind='restore', inputs=[baseline])}
    assert 'changed_conditions' in host.error('exploration_decide', model_call=True, fields=fields)
    fields['reactivation_evidence'] = {routes[0]['ref']: {'changed_conditions': 'Measured target now has higher concurrency',
        'evidence_refs': [regression]}}
    restored = host.call('exploration_decide', model_call=True, fields=fields)
    task = host.call('exploration_materialize', intent_id=restored['intent_id'])
    assert task['node_id'] not in {shared['node_id'], experiment['node_id']}
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM publications').fetchone()[0] == 2
    assert deferred['intent_id'] is None


def test_candidate_identity_and_work_keys_prevent_duplicate_investment(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    fields = {'scope_ref': scope['ref'], 'changes': {'title': 'Mechanism C', 'rationale': 'Known mechanism',
        'critical_assumption': 'Unknown', 'candidate_key': 'cache-batch'}}
    candidate = host.call('exploration_route', model_call=True, fields=fields)
    fields['changes']['title'] = 'Renamed C'
    assert candidate['ref'] in host.error('exploration_route', model_call=True, fields=fields)
    selected = decision(host, scope, routes, work_batch=[work(routes, 'shared-once')])
    routes = current(host, scope)
    error = host.error('exploration_decide', model_call=True, fields={
        'scope_ref': scope['ref'], 'route_refs': [route['ref'] for route in routes],
        'snapshot_id': host.call('query', collection='evidence', refs=[], scope_id=scope['scope_id'])['snapshot_id'],
        'action': 'probe', 'reason': 'Accidental duplicate', 'work_batch': [work(routes[:2], 'shared-once')]})
    assert selected['intent_id'] in error
    assert host.call('query', collection='exploration_intents')['total'] == 1


@pytest.mark.parametrize('input_mode', ['inputs', 'predecessor', 'anchor'])
@pytest.mark.parametrize('stage', ['before_materialize', 'before_claim'])
def test_work_input_retraction_invalidates_unstarted_intent(completed, input_mode, stage):
    host, store, source_node, _, claim, publication = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    host.call('exploration_policy', mode='on')
    scope = host.call('exploration_scope', model_call=True, fields={'changes': {
        'objective': 'Validate the chosen input', 'task_kind': 'selection'}})
    route = host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'changes': {
        'title': 'Use published input', 'rationale': 'Measure against the fixed input',
        'critical_assumption': 'The input is supported'}})
    # The whole publication carries the declared claim. The individual item
    # intentionally does not inherit publication-level knowledge_refs.
    published_ref = f"pub/{publication['publication_id']}"
    proposal = work([route])
    if input_mode == 'inputs':
        proposal['inputs'] = [published_ref]
    elif input_mode == 'predecessor':
        del proposal['root_reason']
        proposal['predecessors'] = [{'node_id': source_node, 'relation_type': 'depends_on',
                                     'rationale': 'Use the published predecessor result',
                                     'input_refs': [published_ref]}]
    else:
        proposal.update(strategy='anchor', anchor_ref=published_ref)
    chosen = decision(host, scope, [route], next_work=proposal)
    task = host.call('exploration_materialize', intent_id=chosen['intent_id']) if stage == 'before_claim' else None
    store.revise_knowledge({'ref': claim['ref'], 'expected_revision': 1,
        'changes': {'status': 'retracted'}, 'reason': 'The input basis is invalid',
        'change_kind': 'retract', 'affected_scope_mode': 'versions', 'affected_scope': [claim['ref']]},
        'retract-work-input')
    if task:
        result = host.call('node_launch_claim', task_id=task['task_id'], concurrency=2)
        assert result['claimed'] is False and result['reason'] == 'exploration_basis_changed'
    else:
        result = host.call('exploration_materialize', intent_id=chosen['intent_id'])
        assert result['state'] == 'stale'
    results = host.call('query', collection='exploration_results', scope_id=scope['scope_id'])['items']
    settled = [item for item in results if item['kind'] == 'stale' and item['data'].get('intent_id') == chosen['intent_id']]
    assert len(settled) == 1
    with store._read() as db:
        reservation = db.execute("SELECT r.state FROM runtime_budget_reservations r JOIN exploration_allocations a "
                                 "USING(reservation_id) WHERE a.intent_id=?", (chosen['intent_id'],)).fetchone()
        assert reservation['state'] == 'released'
        assert db.execute('SELECT COUNT(*) FROM workflow_notifications WHERE reference=?',
                          (settled[0]['result_id'],)).fetchone()[0] == 1


def test_unchanged_work_input_remains_startable_after_materialization(completed):
    host, store, _, _, _, publication = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    host.call('exploration_policy', mode='on')
    scope = host.call('exploration_scope', model_call=True, fields={'changes': {
        'objective': 'Use an existing result', 'task_kind': 'selection'}})
    route = host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'changes': {
        'title': 'Measure again', 'rationale': 'Check the result', 'critical_assumption': 'Input remains valid'}})
    chosen = decision(host, scope, [route], next_work=work([route], inputs=[f"pub/{publication['publication_id']}"]))
    other = store.record_knowledge({'kind': 'hypothesis', 'statement': 'An unrelated question'}, 'other-question')
    store.revise_knowledge({'ref': other['ref'], 'expected_revision': 1,
        'changes': {'statement': 'A changed but still unrelated question'}, 'reason': 'Clarify the unrelated idea',
        'change_kind': 'reword', 'affected_scope_mode': 'none'}, 'revise-other-question')
    task = host.call('exploration_materialize', intent_id=chosen['intent_id'])
    assert host.call('node_launch_claim', task_id=task['task_id'], concurrency=2)['claimed']


def test_parallel_work_sharing_one_fixed_input_is_not_invalidated_by_sibling_materialization(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    shared = decision(host, scope, routes, next_work=work(routes, 'shared'))
    shared_task = host.call('exploration_materialize', intent_id=shared['intent_id'])
    source = publish(host, shared_task, 'ses_shared', 'Frozen shared probe')
    routes = current(host, scope)
    selected = decision(host, scope, routes, result_ids=pending(host, scope), work_batch=[
        work([routes[0]], 'fast', inputs=[source]), work([routes[1]], 'slow', inputs=[source])])
    fast, slow = [host.call('exploration_materialize', intent_id=ident) for ident in selected['intent_ids']]
    assert fast['state'] == slow['state'] == 'materialized'
    assert host.call('node_launch_claim', task_id=fast['task_id'], concurrency=2)['claimed']
    publish(host, fast, 'ses_fast', 'The fast sibling returned')
    assert host.call('node_launch_claim', task_id=slow['task_id'], concurrency=2)['claimed']


def test_changed_work_question_invalidates_its_unstarted_intent(completed):
    host, store, _, _, _, _ = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    host.call('exploration_policy', mode='on')
    scope = host.call('exploration_scope', model_call=True, fields={'changes': {
        'objective': 'Follow a fixed question', 'task_kind': 'selection'}})
    route = host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'changes': {
        'title': 'Question probe', 'rationale': 'Measure this question', 'critical_assumption': 'Question is current'}})
    question = store.record_knowledge({'kind': 'open_question', 'statement': 'How does the old workload behave?'}, 'question')
    chosen = decision(host, scope, [route], next_work=work([route], question_ref=question['ref']))
    store.revise_knowledge({'ref': question['ref'], 'expected_revision': 1,
        'changes': {'statement': 'How does the new workload behave?'}, 'reason': 'The question changed',
        'change_kind': 'reword', 'affected_scope_mode': 'none'}, 'revise-question')
    assert host.call('exploration_materialize', intent_id=chosen['intent_id'])['state'] == 'stale'


def test_new_review_challenge_still_invalidates_work_input(completed):
    host, store, _, _, claim, publication = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    host.call('exploration_policy', mode='on')
    scope = host.call('exploration_scope', model_call=True, fields={'changes': {
        'objective': 'Use a reviewed input', 'task_kind': 'selection'}})
    route = host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'changes': {
        'title': 'Input probe', 'rationale': 'Measure against this input', 'critical_assumption': 'Its claim holds'}})
    chosen = decision(host, scope, [route], next_work=work([route], inputs=[f"pub/{publication['publication_id']}"]))
    host.call('review_scope_set', refs=[claim['ref']], reason='Review the input claim')
    task = host.call('exploration_materialize', intent_id=chosen['intent_id'])
    assert task['state'] == 'materialized'
    obligation = next(item for item in obligations(store)
                      if item['target_ref'] == claim['ref'] and item['scope_owner'] == 'project')
    link(host, obligation, report(host), result='challenges')
    outcome = host.call('node_launch_claim', task_id=task['task_id'], concurrency=2)
    assert outcome['claimed'] is False and outcome['reason'] == 'exploration_basis_changed'


@pytest.mark.parametrize('input_in_snapshot', [False, True])
@pytest.mark.parametrize('stage', ['before_materialize', 'before_claim'])
def test_development_v1_pending_work_requires_reapproval_if_input_was_unbound(completed, input_in_snapshot, stage):
    host, store, _, _, _, publication = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    host.call('exploration_policy', mode='on')
    scope = host.call('exploration_scope', model_call=True, fields={'changes': {
        'objective': 'Check legacy work', 'task_kind': 'selection'}})
    route = host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'changes': {
        'title': 'Existing route', 'rationale': 'Preserve safe work', 'critical_assumption': 'Input applies'}})
    source = f"pub/{publication['publication_id']}"
    page = host.call('query', collection='evidence', refs=[source] if input_in_snapshot else [], scope_id=scope['scope_id'])
    chosen = host.call('exploration_decide', model_call=True, fields={
        'scope_ref': scope['ref'], 'route_refs': [route['ref']], 'snapshot_id': page['snapshot_id'],
        'action': 'probe', 'reason': 'Approve work', 'next_work': work([route], inputs=[source])})
    task = host.call('exploration_materialize', intent_id=chosen['intent_id']) if stage == 'before_claim' else None
    # Development-only E3 round-1 format, NOT a schema15 authorization.
    with store._connection() as db:
        row = db.execute('SELECT details FROM workflow_intents WHERE intent_id=?', (chosen['intent_id'],)).fetchone()
        details = json.loads(row['details'])
        details['basis_mode'] = 'scope-work-v1'
        details.pop('work_input_refs')
        details['basis_digest'] = scope_work_basis(store, db, details['roots'], details['scope_id'], details['route_refs'])
        db.execute('UPDATE workflow_intents SET details=? WHERE intent_id=?', (json.dumps(details), chosen['intent_id']))
        db.commit()
    if task:
        result = host.call('node_launch_claim', task_id=task['task_id'], concurrency=2)
        assert result['claimed'] is input_in_snapshot
        if not input_in_snapshot:
            assert result['reason'] == 'exploration_basis_changed'
    else:
        result = host.call('exploration_materialize', intent_id=chosen['intent_id'])
        assert result['state'] == ('materialized' if input_in_snapshot else 'stale')


@pytest.mark.parametrize('input_in_snapshot', [False, True])
@pytest.mark.parametrize('stage', ['before_materialize', 'before_claim'])
def test_real_schema15_intents_require_explicit_format_reapproval(tmp_path, monkeypatch, input_in_snapshot, stage):
    from auto_research import native_store
    from auto_research.native_store import NativeStore
    from auto_research.evidence_read import _hash
    fixture = json.loads((Path(__file__).parent / 'fixtures/schema15-exploration-intents.json').read_text())
    case = next(case for case in fixture['cases'] if case['input_in_snapshot'] == input_in_snapshot)
    old_details = json.loads(case['intent']['details'])
    assert 'basis_mode' not in old_details
    assert _hash(case['view']) == old_details['basis_digest']
    assert fixture['source_commit'] == '31fd90da8a7634619a1a42b23a62b680f483294d'
    monkeypatch.setattr(native_store, 'SCHEMA_VERSION', 15)
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, next_work=work([routes[0]]))
    task = host.call('exploration_materialize', intent_id=chosen['intent_id']) if stage == 'before_claim' else None
    if task:
        old_details.update(task_id=task['task_id'], node_id=task['node_id'])
    store = NativeStore(host.root)
    with store._connection() as db:
        db.execute('UPDATE workflow_intents SET details=? WHERE intent_id=?', (json.dumps(old_details), chosen['intent_id']))
    monkeypatch.setattr(native_store, 'SCHEMA_VERSION', 16)
    store = NativeStore(host.root)
    host.service.stores[str(host.root)] = store
    if task:
        result = host.call('node_launch_claim', task_id=task['task_id'], concurrency=2)
        assert not result['claimed']
    else:
        result = host.call('exploration_materialize', intent_id=chosen['intent_id'])
        assert result['state'] == 'stale'
    assert result['reason'] == 'exploration_basis_format_upgraded'
    with store._read() as db:
        row = db.execute('SELECT state,details FROM workflow_intents WHERE intent_id=?', (chosen['intent_id'],)).fetchone()
        assert row['state'] == 'stale'
        assert json.loads(row['details'])['basis_digest'] == old_details['basis_digest']
        assert db.execute('SELECT COUNT(*) FROM exploration_results').fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM workflow_notifications WHERE kind='exploration_result'").fetchone()[0] == 1


def test_budget_wait_hint_is_emitted_once_per_transition(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    # Occupy the one-node ceiling without starting an exploration task.
    host.call('propose', question='Existing node', why_now='Initial work', plan='Measure', root_reason='Prior work')
    host.call('runtime_budget_set', limits={'max_total_nodes': 1}, reason='One node ceiling')
    chosen = decision(host, scope, routes, next_work=work([routes[0]]))
    first = host.call('exploration_materialize', intent_id=chosen['intent_id'])
    second = host.call('exploration_materialize', intent_id=chosen['intent_id'])
    assert first['state'] == second['state'] == 'waiting_budget'
    assert first['notify_waiting'] and not second['notify_waiting']


def test_missing_expected_result_survives_later_stop_and_is_reported(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    initial = decision(host, scope, routes, work_batch=[work([routes[0]], 'fast'), work([routes[1]], 'slow')])
    fast, slow = [host.call('exploration_materialize', intent_id=ident) for ident in initial['intent_ids']]
    publish(host, fast, 'ses_fast', 'Only the fast route returned evidence')
    current_routes = current(host, scope)
    provisional = decision(host, scope, current_routes, expected_results=initial['intent_ids'], provisional=True,
        result_ids=pending(host, scope), route_changes=[{'route_ref': current_routes[0]['ref'],
            'disposition': 'adopted', 'reason': 'Provisional choice while awaiting the slow route'}])
    assert provisional['expected_missing'][0]['intent_id'] == initial['intent_ids'][1]
    page = host.call('query', collection='evidence', refs=[], scope_id=scope['scope_id'])
    assert 'Expected evidence is missing' in host.error('exploration_decide', model_call=True, fields={
        'scope_ref': scope['ref'], 'route_refs': [route['ref'] for route in current(host, scope)],
        'snapshot_id': page['snapshot_id'], 'action': 'deepen', 'reason': 'Too early to decide definitively'})

    # The slow task is cancelled, not refuted. The stop decision deliberately
    # omits expected_results and must still retain the unresolved expectation.
    host.call('exploration_policy', mode='off')
    stop = decision(host, scope, current(host, scope), action='propose_stop', result_ids=pending(host, scope),
                    reason='The unavailable slow comparison remains a limitation')
    assert [item['intent_id'] for item in stop['expected_missing']] == [initial['intent_ids'][1]]
    assert stop['expected_missing'][0]['state'] == 'cancelled'
    assert stop['expected_missing'][0]['acknowledgement_reason'] == 'The unavailable slow comparison remains a limitation'
    project_page = host.call('query', collection='evidence', refs=[])
    draft = host.call('conclusion_prepare', model_call=True, fields={
        **conclusion_fields([], 'partial'), 'snapshot_id': project_page['snapshot_id']})
    missing = draft['exploration_stop']['missing_expected_results']
    assert missing[0]['missing_results'][0]['intent_id'] == initial['intent_ids'][1]


def test_late_publication_resolves_inherited_expectation_without_redeclaring_it(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    initial = decision(host, scope, routes, work_batch=[work([routes[0]], 'fast'), work([routes[1]], 'slow')])
    fast, slow = [host.call('exploration_materialize', intent_id=ident) for ident in initial['intent_ids']]
    publish(host, fast, 'ses_fast', 'Fast result')
    provisional = decision(host, scope, current(host, scope), expected_results=initial['intent_ids'],
        provisional=True, result_ids=pending(host, scope))
    assert len(provisional['expected_missing']) == 1
    publish(host, slow, 'ses_slow', 'Late result')
    revised = decision(host, scope, current(host, scope), action='redirect', result_ids=pending(host, scope),
                       revises_decision_id=provisional['decision_id'])
    assert revised['expected_missing'] == []
    latest = host.call('query', collection='exploration_decisions')['items'][-1]['data']
    assert initial['intent_ids'][1] in latest['expected_results']
    assert latest['received_results'][0]['results']


def test_deferred_missing_work_does_not_make_future_unrelated_work_permanently_provisional(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    initial = decision(host, scope, routes, work_batch=[work([routes[0]], 'fast'), work([routes[1]], 'slow')])
    fast = host.call('exploration_materialize', intent_id=initial['intent_ids'][0])
    publish(host, fast, 'ses_fast', 'Fast result')
    decision(host, scope, current(host, scope), expected_results=initial['intent_ids'], provisional=True,
             result_ids=pending(host, scope))
    deferred = decision(host, scope, current(host, scope), action='defer',
        reason='The slow measurement is no longer available', supersede_intent_ids=[initial['intent_ids'][1]])
    assert deferred['expected_missing'][0]['state'] == 'superseded'
    assert deferred['expected_missing'][0]['acknowledged_by_decision'] == deferred['decision_id']
    routes = current(host, scope)
    continued = decision(host, scope, routes, action='probe', reason='Continue a separate bounded question',
                         next_work=work([routes[0]], 'follow-up'))
    assert continued['intent_id']
    assert continued['expected_missing'][0]['acknowledged_by_decision'] == deferred['decision_id']


@pytest.mark.parametrize(('field', 'value'), [
    ('workload', 'unknown'), ('environment', {}), ('unit', ''), ('uncertainty', 'unknown'),
])
def test_matching_unknown_conditions_do_not_become_comparable(tmp_path, field, value):
    host = Host(tmp_path)
    scope, routes = setup(host)
    selected = decision(host, scope, routes, work_batch=[work([routes[0]], 'A'), work([routes[1]], 'B')])
    tasks = [host.call('exploration_materialize', intent_id=ident) for ident in selected['intent_ids']]
    refs = [publish(host, task, f'ses_{index}', f'Observed {index}') for index, task in enumerate(tasks)]
    conditions = {'baseline_ref': refs[0], 'workload': 'production', 'metric': 'p99', 'unit': 'ms',
                  'environment': 'device-1', 'repeats': 3, 'uncertainty': 'interval available'}
    conditions[field] = value
    snapshot = host.call('query', collection='evidence', refs=[], scope_id=scope['scope_id'])
    fields = {'scope_ref': scope['ref'], 'route_refs': [item['ref'] for item in current(host, scope)],
              'snapshot_id': snapshot['snapshot_id'], 'action': 'redirect', 'reason': 'Assess comparability',
              'comparisons': [{'result_refs': refs, 'conditions': [conditions, dict(conditions)],
                               'ranking': True, 'reason': 'Check before ranking'}]}
    assert 'incomparable_results' in host.error('exploration_decide', model_call=True, fields=fields)
    fields['comparisons'][0]['ranking'] = False
    result = host.call('exploration_decide', model_call=True, fields=fields)
    assert result['comparisons'][0]['status'] == 'incomparable'
