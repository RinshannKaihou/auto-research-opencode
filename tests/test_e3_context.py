"""E3 summaries retain concurrent and deferred scopes with complete expansion indexes."""
from auto_research import context_policy, workbench_read
from auto_research.native_store import NativeStore
from test_context_policy import sections
from test_exploration_foundation import scope_and_route
from test_opencode_host import Host
from test_e3_multiroute import setup, decision, work, publish, current, pending


def test_context_and_board_keep_all_scopes_and_expansion_paths(tmp_path):
    host = Host(tmp_path)
    host.open()
    first, route = scope_and_route(host)
    second = host.call('exploration_scope', model_call=True, fields={'changes':{
        'objective':'Independent direction', 'task_kind':'discovery'}})
    deferred = host.call('exploration_scope', model_call=True, fields={
        'scope_ref':first['ref'], 'reason':'Wait for representative workload',
        'changes':{'state':'deferred', 'reactivation_condition':'A new workload is available'}})
    small = sections(host.call('memory_context', policy='memory-r1-v1', max_chars=900))['exploration']
    assert small['scopes_total'] == 2
    assert small['active_scopes_total'] == 1
    assert small['details_omitted']
    assert small['scopes'] == {'collection':'exploration_scopes'}
    assert small['routes'] == {'collection':'exploration_routes'}
    large = sections(host.call('memory_context', policy='memory-r1-v1', max_chars=12000))['exploration']
    assert not large['details_omitted']
    scopes = {item['ref']:item for item in large['details']['scope_sample']}
    assert scopes[deferred['ref']]['state'] == 'deferred'
    assert scopes[second['ref']]['state'] == 'active'
    assert large['details']['budget']['actual_usage_status'] == 'unknown'
    assert route['ref'] in [item['ref'] for item in large['details']['route_sample']]
    store = NativeStore(host.root)
    with store._read() as db:
        overview = context_policy.exploration_overview(store, db, db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()[0])
    assert overview['scopes_total'] == 2
    assert overview['deferred_scopes_total'] == 1
    assert overview['routes_total'] == 1
    summary = workbench_read.summary(store, 'opencode', 'ses_main')
    assert summary['exploration']['scopes_total'] == 2
    assert summary['exploration']['budget']['actual_usage_status'] == 'unknown'
    assert summary['exploration']['routes'][0]['scope_id'] == first['scope_id']


def test_overview_counts_real_expected_gaps_and_only_live_materialized_work(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work([routes[0]], 'fast'), work([routes[1]], 'slow')])
    fast, slow = [host.call('exploration_materialize', intent_id=ident) for ident in chosen['intent_ids']]
    publish(host, fast, 'ses_fast', 'Fast result only')
    # The test helper closes a work segment; emulate the dedicated host finish
    # task state as the actual host does (the intent itself stays materialized).
    host.call('workflow', action='task_state', fields={'task_id': fast['task_id'], 'state': 'finished'})
    decision(host, scope, current(host, scope), expected_results=chosen['intent_ids'],
             provisional=True, result_ids=pending(host, scope))
    summary = workbench_read.summary(NativeStore(host.root), 'opencode', 'ses_main')['exploration']
    assert summary['pending_work_total'] == 1
    assert summary['missing_results_total'] == summary['unacknowledged_missing_total'] == 1
    assert summary['latest_decisions'][0]['missing_results_total'] == 1
    assert summary['latest_decisions'][0]['declared_missing_results_total'] == 0
    decision(host, scope, current(host, scope), action='defer', reason='Await the slow result')
    summary = workbench_read.summary(NativeStore(host.root), 'opencode', 'ses_main')['exploration']
    assert summary['missing_results_total'] == 1 and summary['unacknowledged_missing_total'] == 0


def test_provisional_scope_counts_are_not_limited_to_three_samples(tmp_path):
    host = Host(tmp_path)
    first, routes = setup(host)
    decision(host, first, routes)
    for index in range(4):
        new_scope = host.call('exploration_scope', model_call=True, fields={'changes': {
            'objective': f'Independent direction {index}', 'task_kind': 'selection'}})
        if index < 3:
            decision(host, new_scope, [])
    # The fifth scope is beyond the overview's sample.
    last = host.call('query', collection='exploration_scopes')['items'][-1]
    route = host.call('exploration_route', model_call=True, fields={'scope_ref': last['ref'], 'changes': {
        'title': 'Late comparison', 'rationale': 'Needs evidence', 'critical_assumption': 'Unknown'}})
    decision(host, last, [route], provisional=True, missing_results=['Await independent measurement'])
    summary = workbench_read.summary(NativeStore(host.root), 'opencode', 'ses_main')['exploration']
    assert len(summary['latest_decisions']) <= 3
    assert all(not item['provisional'] for item in summary['latest_decisions'])
    assert summary['provisional_scopes_total'] == 1
