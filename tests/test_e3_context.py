"""E3 summaries retain concurrent and deferred scopes with complete expansion indexes."""
from auto_research import context_policy, workbench_read
from auto_research.native_store import NativeStore
from test_context_policy import sections
from test_exploration_foundation import scope_and_route
from test_opencode_host import Host


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
