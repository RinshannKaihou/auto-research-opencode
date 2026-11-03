"""B3: a pure scope state action by the coordinator needs no self-confirmation."""

import json

from test_opencode_host import Host
from test_e3_multiroute import setup, decision, work, publish, current, pending
from auto_research.native_store import NativeStore


def _events(host, kind):
    store = NativeStore(host.root)
    with store._read() as db:
        return [json.loads(row['data']) for row in db.execute(
            'SELECT data FROM events WHERE kind=? ORDER BY event_id', (kind,))]


def _close(host, scope, **extra):
    return host.call('exploration_scope', model_call=True, fields={
        'scope_ref': scope['ref'], 'reason': 'Park this direction',
        'changes': {'state': 'closed', 'reactivation_condition': 'A new constraint arrives'}, **extra})


def test_pure_close_records_credential_instead_of_self_confirmation(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    closed = _close(host, scope)
    assert closed['state'] == 'closed'
    results = host.call('query', collection='exploration_results', scope_id=scope['scope_id'])
    assert results['items'] == []
    credentials = _events(host, 'exploration.scope_state_expressed')
    assert len(credentials) == 1
    assert credentials[0]['rule'] == 'scope-state-expressed-v1'
    assert credentials[0]['scope_ref'] == closed['ref']
    assert credentials[0]['before_ref'] == scope['ref']
    assert credentials[0]['reason'] == 'Park this direction'
    # Routes and history remain; the close did not delete candidates.
    assert len(current(host, scope)) == 2
    # An older snapshot still fails the scope revision CAS fence.
    snapshot = host.call('query', collection='evidence', refs=[], scope_id=scope['scope_id'])
    stale = host.error('exploration_decide', model_call=True, fields={
        'scope_ref': scope['ref'], 'route_refs': [route['ref'] for route in current(host, scope)],
        'snapshot_id': snapshot['snapshot_id'], 'action': 'defer', 'reason': 'Closed'})
    assert 'exploration_scope_changed' in stale


def test_conservative_paths_keep_the_state_change_result(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    # Pending results on the scope keep the original obligation.
    chosen = decision(host, scope, routes, work_batch=[work([routes[0]], 'measured')])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    publish(host, task, 'ses_node', 'A measurement')
    _close(host, scope)
    kinds = [item['kind'] for item in host.call('query', collection='exploration_results',
                                                scope_id=scope['scope_id'])['items']]
    assert 'scope_state_changed' in kinds

    # A second scope: unstarted scheduled work also keeps the result.
    host.call('exploration_scope', model_call=True, fields={'changes': {
        'objective': 'Second direction', 'task_kind': 'selection'}})
    second = host.call('query', collection='exploration_scopes')['items'][-1]
    second_routes = [host.call('exploration_route', model_call=True, fields={
        'scope_ref': second['ref'], 'changes': {'title': 'A2', 'rationale': 'x', 'critical_assumption': 'y'}})]
    decision(host, {'ref': second['ref'], 'scope_id': second['scope_id']}, second_routes,
             next_work=work(second_routes))
    intent = host.call('query', collection='exploration_intents')['items'][-1]
    assert intent['state'] == 'pending'
    host.call('exploration_scope', model_call=True, fields={
        'scope_ref': second['ref'], 'reason': 'Defer', 'changes': {
            'state': 'deferred', 'reactivation_condition': 'Budget frees'}})
    kinds = [item['kind'] for item in host.call('query', collection='exploration_results',
                                                scope_id=second['scope_id'])['items']]
    assert 'scope_state_changed' in kinds

    # Reactivation (deferred -> active) is never self-expressed.
    deferred_ref = host.call('query', collection='exploration_scopes')['items'][-1]['ref']
    host.call('exploration_scope', model_call=True, fields={
        'scope_ref': deferred_ref, 'reason': 'Back', 'changed_conditions': 'Budget freed',
        'changes': {'state': 'active'}})
    store = NativeStore(host.root)
    with store._read() as db:
        latest = json.loads(db.execute('SELECT data FROM exploration_scopes WHERE scope_id=? '
                                       'ORDER BY revision DESC LIMIT 1', (second['scope_id'],)).fetchone()[0])
    assert latest['state'] == 'active'
    kinds = [item['kind'] for item in host.call('query', collection='exploration_results',
                                                scope_id=second['scope_id'])['items']]
    assert kinds.count('scope_state_changed') == 2


def test_goal_change_alongside_state_change_stays_conservative(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    host.call('exploration_scope', model_call=True, fields={
        'scope_ref': scope['ref'], 'reason': 'Reword and close',
        'changes': {'objective': 'A different objective', 'state': 'closed',
                    'reactivation_condition': 'A new constraint arrives'}})
    kinds = [item['kind'] for item in host.call('query', collection='exploration_results',
                                                scope_id=scope['scope_id'])['items']]
    assert sorted(kinds) == ['scope_changed', 'scope_state_changed']
    assert _events(host, 'exploration.scope_state_expressed') == []
