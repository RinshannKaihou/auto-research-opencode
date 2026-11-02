"""A1: standing exploration strategy and interface consistency for activation."""

import json

from test_context_policy import sections
from test_opencode_host import Host
from test_e3_multiroute import setup, decision, work, publish, current, pending
from auto_research.native_store import NativeStore


def _strategy(block):
    return block.get('strategy') if isinstance(block, dict) else None


def test_strategy_reaches_main_with_scopes_and_condenses_under_pressure(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work([routes[0]], 'first', work_kind='deepen')])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    publish(host, task, 'ses_node', 'First bounded measurement')
    large = sections(host.call('memory_context', policy='memory-r1-v1', max_chars=12000))
    strategy = _strategy(large['exploration'])
    assert strategy and strategy['version'] == 'exploration-strategy-v1'
    rules = ' '.join(strategy['rules'])
    for phrase in ('large_commit', 'smaller runnable probe', 'alternatives', 'why one path suffices',
                   'new_result', 'not automatically counter-evidence', 'stop_check',
                   'stopping with reasons'):
        assert phrase in rules
    assert 'not gates' in strategy['note']
    small = sections(host.call('memory_context', policy='memory-r1-v1', max_chars=900))
    condensed = _strategy(small['exploration'])
    # The actionable brief and the untried count survive independently of the
    # route samples; bare codes only remain as the last-resort fallback.
    assert condensed and condensed.get('brief') and 'Probe decision-changing unknowns' in condensed['brief']
    assert small['exploration']['untried_total'] >= 0
    assert small['exploration']['details_omitted'] is True


def test_strategy_reaches_main_with_zero_scopes_and_stays_off_otherwise(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('workflow', action='run', fields={'state': 'running'})
    host.call('exploration_policy', mode='on')
    view = sections(host.call('memory_context', policy='memory-r1-v1', max_chars=12000))
    assert view['exploration']['scopes_total'] == 0
    strategy = _strategy(view['exploration'])
    assert strategy and any('register the choice question' in rule for rule in strategy['rules'])
    host.call('exploration_policy', mode='off')
    view = sections(host.call('memory_context', policy='memory-r1-v1', max_chars=12000))
    assert 'exploration' not in view or _strategy(view.get('exploration')) is None
    # node_core never sees the coordinator strategy.
    host.call('exploration_policy', mode='on')
    scope = host.call('exploration_scope', model_call=True, fields={'changes': {
        'objective': 'Pick one', 'task_kind': 'selection'}})
    route = host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'changes': {
        'title': 'A', 'rationale': 'One option', 'critical_assumption': 'None'}})
    chosen = decision(host, scope, [route], next_work=work([route]))
    task = host.call('exploration_materialize', intent_id=chosen['intent_id'])
    host.call('workflow', action='task_session', fields={'task_id': task['task_id'], 'session_id': 'ses_node'})
    host.call('open', session='ses_node', root=str(host.root), cwd=str(host.root),
              session_role='node_core', node_id=task['node_id'])
    host.call('focus', session='ses_node', node_id=task['node_id'], role='core', mode='auto')
    node_view = sections(host.call('memory_context', session='ses_node',
                                   policy='memory-r1-v1', max_chars=12000))
    assert _strategy(node_view.get('exploration')) is None


def test_main_conclusion_wording_follows_contract(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('workflow', action='run', fields={'state': 'running'})
    legacy = sections(host.call('memory_context', profile='opencode-auto',
                                policy='memory-r1-v1', max_chars=12000))
    assert 'outcome (answered/partial/unresolved)' in legacy['role_instructions']
    assert 'research_prepare_conclusion' not in legacy['role_instructions']
    host.call('exploration_policy', mode='on')
    view = sections(host.call('memory_context', profile='opencode-auto',
                              policy='memory-r1-v1', max_chars=12000))
    assert view['role_instructions'].count('research_prepare_conclusion') >= 1
    assert 'draft_id' in view['role_instructions']
    assert 'outcome (answered/partial/unresolved)' not in view['role_instructions']


def test_bindings_record_actual_work_kind(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[
        work([routes[0]], 'deep', work_kind='deepen'), work([routes[1]], 'probe')])
    for ident in chosen['intent_ids']:
        host.call('exploration_materialize', intent_id=ident)
    store = NativeStore(host.root)
    with store._read() as db:
        rows = {row['role_in_route'] for row in db.execute(
            'SELECT role_in_route FROM exploration_bindings')}
    assert rows == {'deepen', 'probe'}


def test_untried_candidates_are_not_displaced_by_excluded_routes(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('workflow', action='run', fields={'state': 'running'})
    host.call('exploration_policy', mode='on')
    scope = host.call('exploration_scope', model_call=True, fields={'changes': {
        'objective': 'Choose a mechanism', 'task_kind': 'selection'}})
    titles = ['A', 'B', 'C', 'D', 'E']
    routes = [host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'changes': {
        'title': title, 'rationale': 'Candidate', 'critical_assumption': f'Assumption of {title}'}})
        for title in titles]
    snapshot = host.call('query', collection='evidence', refs=[], scope_id=scope['scope_id'])
    host.call('exploration_decide', model_call=True, fields={
        'scope_ref': scope['ref'], 'route_refs': [route['ref'] for route in routes],
        'snapshot_id': snapshot['snapshot_id'], 'action': 'redirect', 'reason': 'Narrow the field',
        'route_changes': [{'route_ref': routes[index]['ref'], 'disposition': 'excluded',
                           'reason': 'Dominated', 'reactivation_condition': 'New constraint'}
                          for index in range(3)]})
    view = sections(host.call('memory_context', policy='memory-r1-v1', max_chars=12000))
    exploration = view['exploration']
    assert exploration['meta']['untried_total'] == 2
    assert exploration['meta']['route_status_counts'] == {'excluded': 3, 'untried': 2}
    sample = exploration['details']['route_sample']
    sampled_titles = [item['title'] for item in sample]
    assert {'D', 'E'} <= set(sampled_titles)
    # Untried candidates rank ahead of the excluded routes in the sample order.
    assert set(sampled_titles[:2]) == {'D', 'E'}
    assert all('untried_candidate' in item['selection_reasons']
               for item in sample if item['title'] in {'D', 'E'})
    assert exploration['meta']['omitted']['routes'] == 1


def test_pending_risk_on_invested_route_reaches_the_priority_zone(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work([routes[0]], 'invested')])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    publish(host, task, 'ses_node', 'Early measurement')
    # A registered challenge against the evidence behind the invested route.
    store = NativeStore(host.root)
    with store._connection() as db:
        from auto_research.exploration_store import ExplorationStore
        ExplorationStore._exploration_result_in_scope(store, db, scope['scope_id'],
            'review-event:challenge-1', 'review_challenge', 'challenge-1',
            {'node_id': task['node_id'], 'route_ids': [routes[0]['route_id']], 'summary': 'challenged'})
        db.commit()
    view = sections(host.call('memory_context', policy='memory-r1-v1', max_chars=12000))
    exploration = view['exploration']
    invested_ref = current(host, scope)[0]['ref']
    invested = [item for item in exploration['details']['route_sample']
                if item['ref'] == invested_ref]
    assert invested and 'pending_results_on_invested_route' in invested[0]['selection_reasons']
    assert any(item['kind'] == 'review_challenge' and item['result_id'].startswith('XR-')
               for item in exploration['meta']['result_sample'])


def test_multi_scope_pending_todo_is_not_shadowed_by_the_latest_scope(tmp_path):
    host = Host(tmp_path)
    first, routes = setup(host)
    chosen = decision(host, first, routes, work_batch=[work([routes[0]], 'measured')])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    publish(host, task, 'ses_node', 'Result for the first scope')
    host.call('exploration_scope', model_call=True, fields={'changes': {
        'objective': 'Latest direction', 'task_kind': 'discovery'}})
    view = sections(host.call('memory_context', policy='memory-r1-v1', max_chars=12000))
    exploration = view['exploration']
    assert exploration['scopes_total'] == 2
    scope_sample = exploration['details']['scope_sample']
    by_scope = {item['scope_id']: item for item in scope_sample}
    assert by_scope[first['scope_id']]['pending_results_total'] >= 1
    assert 'pending_todo_first' in by_scope[first['scope_id']]['selection_reasons']
    latest = [item for item in scope_sample if item['scope_id'] != first['scope_id']]
    assert latest and latest[0]['objective'].startswith('Latest direction')


def test_single_delivery_route_renders_without_forced_branching(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('workflow', action='run', fields={'state': 'running'})
    host.call('exploration_policy', mode='on')
    scope = host.call('exploration_scope', model_call=True, fields={'changes': {
        'objective': 'Deliver the report', 'task_kind': 'delivery'}})
    host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'changes': {
        'title': 'Write it', 'rationale': 'Direct delivery', 'critical_assumption': 'None'}})
    view = sections(host.call('memory_context', policy='memory-r1-v1', max_chars=12000))
    exploration = view['exploration']
    assert exploration['routes_total'] == 1
    assert 'notable_alternatives' not in exploration['details']['scope_sample'][0]


def test_context_reads_do_not_mutate_exploration_state(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    decision(host, scope, routes, work_batch=[work([routes[0]], 'pending')])
    store = NativeStore(host.root)
    with store._read() as db:
        before = {table: db.execute(f'SELECT * FROM {table}').fetchall()
                  for table in ('exploration_scopes', 'exploration_routes', 'exploration_results',
                                'exploration_decisions', 'workflow_intents', 'workflow_project')}
    for _ in range(2):
        host.call('memory_context', policy='memory-r1-v1', max_chars=12000)
    with store._read() as db:
        after = {table: db.execute(f'SELECT * FROM {table}').fetchall()
                 for table in ('exploration_scopes', 'exploration_routes', 'exploration_results',
                               'exploration_decisions', 'workflow_intents', 'workflow_project')}
    assert before == after


def test_role_and_control_outrank_exploration_samples(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work([routes[0]], 'first')])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    publish(host, task, 'ses_node', 'First measurement')
    for budget in (5000, 6000, 12000):
        blocks = sections(host.call('memory_context', profile='opencode-auto',
                                    policy='memory-r1-v1', max_chars=budget))
        assert 'role_instructions' in blocks, budget
        assert 'control_instructions' in blocks, budget
        assert 'exploration' in blocks, budget


def test_brief_strategy_and_untried_count_survive_without_details(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work([routes[0]], 'first')])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    publish(host, task, 'ses_node', 'First measurement')
    for budget in (900, 3000):
        blocks = sections(host.call('memory_context', profile='opencode-auto',
                                    policy='memory-r1-v1', max_chars=budget))
        exploration = blocks['exploration']
        assert exploration['strategy'].get('brief'), budget
        assert 'Probe decision-changing unknowns' in exploration['strategy']['brief']
        assert exploration['untried_total'] == 1, budget


def test_long_route_titles_never_displace_instructions_or_strategy(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    for index in range(4):
        host.call('exploration_route', model_call=True, fields={'scope_ref': scope['ref'], 'changes': {
            'title': 'T' * 3100, 'rationale': 'Probe this mechanism', 'critical_assumption': 'Requires measurement'}})
    blocks = sections(host.call('memory_context', profile='opencode-auto',
                                policy='memory-r1-v1', max_chars=12000))
    exploration = blocks['exploration']
    assert 'role_instructions' in blocks and 'control_instructions' in blocks
    assert exploration['strategy'].get('brief') or exploration['strategy'].get('rules')
    assert exploration['untried_total'] == 6
    if not exploration['details_omitted']:
        sampled = exploration['details']['route_sample']
        assert all(len(item['title']) <= 101 for item in sampled)
        assert any(item.get('title_truncated') for item in sampled)
