"""B2: mechanical finish confirmations stop duplicating published obligations."""

import json

from test_opencode_host import Host
from test_e3_multiroute import setup, decision, work, current, pending
from auto_research.native_store import NativeStore


def _node_session(host, task, session='ses_node'):
    host.call('workflow', action='task_session', fields={'task_id': task['task_id'], 'session_id': session})
    host.call('open', session=session, root=str(host.root), cwd=str(host.root),
              session_role='node_core', node_id=task['node_id'])
    host.call('focus', session=session, node_id=task['node_id'], role='core', mode='auto')


def _publish(host, session, summary, status='complete'):
    return host.call('publish', session=session, status=status, summary=summary, gaps=[],
                     knowledge_refs=[], items=[{'item_id': 'r', 'kind': 'text',
                     'content': {'text': summary}}])


def _events(host, kind):
    store = NativeStore(host.root)
    with store._read() as db:
        return [json.loads(row['data']) for row in db.execute(
            'SELECT data FROM events WHERE kind=? ORDER BY event_id', (kind,))]


def test_verbatim_confirmation_merges_and_keeps_the_snapshot_valid(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work(routes)])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    _node_session(host, task)
    instructions = host.call('memory_context', session='ses_node', profile='opencode-auto',
                             policy='memory-r1-v1')['text']
    assert 'copy the last complete publication summary verbatim into research_finish.summary' in instructions
    assert 'Publish new evidence' in instructions
    _publish(host, 'ses_node', 'Measured result')
    snapshot = host.call('query', collection='evidence', refs=[], scope_id=scope['scope_id'])
    pending_before = pending(host, scope)
    host.call('finish', session='ses_node', state='finished', details={'reason': 'Measured result'})
    # No additional pending finish obligation appeared.
    assert pending(host, scope) == pending_before
    results = host.call('query', collection='exploration_results', scope_id=scope['scope_id'])
    assert [item['kind'] for item in results['items']] == ['published']
    # Execution facts and the auditable merge verdict remain queryable.
    assert len(_events(host, 'attempt.finished')) == 1
    merges = _events(host, 'exploration.finish_confirmed')
    assert len(merges) == 1
    assert merges[0]['rule'] == 'finish-merge-v1'
    assert merges[0]['attempt_id'].startswith('A-')
    assert merges[0]['task_id'] == task['task_id']
    assert merges[0]['scope_ids'] == [scope['scope_id']]
    assert merges[0]['publication_id']
    # The confirmation alone does not invalidate the pre-finish snapshot.
    host.call('exploration_decide', model_call=True, fields={
        'scope_ref': scope['ref'], 'route_refs': [route['ref'] for route in current(host, scope)],
        'snapshot_id': snapshot['snapshot_id'], 'action': 'probe', 'reason': 'Proceed',
        'result_ids': pending_before})
    # Replaying the same finish cannot merge or notify twice: no live attempt.
    replay = host.error('finish', session='ses_node', state='finished',
                        details={'reason': 'Measured result'})
    assert replay


def test_distinct_paths_keep_the_independent_finish_result(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work(routes)])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    _node_session(host, task)
    seen = set()

    def kinds_after(finish_details, *, summary='S', status='complete', publish=True, state='finished'):
        nonlocal seen
        host.call('focus', session='ses_node', node_id=task['node_id'], role='core', mode='auto')
        if publish:
            _publish(host, 'ses_node', summary, status=status)
        host.call('finish', session='ses_node', state=state, details=finish_details)
        results = host.call('query', collection='exploration_results', scope_id=scope['scope_id'])
        fresh = [item for item in results['items'] if item['result_id'] not in seen]
        seen.update(item['result_id'] for item in fresh)
        return [item['kind'] for item in fresh]

    assert kinds_after({'reason': 'Different wording than the publication'}) == ['published', 'finished']
    assert kinds_after({'reason': 'S', 'note': 'extra payload'}) == ['published', 'finished']
    assert kinds_after({'reason': 'S'}, publish=False) == ['finished']
    assert kinds_after({'reason': 'S'}, state='stopped') == ['published', 'stopped']
    assert kinds_after({'reason': 'S'}, status='partial') == ['published', 'finished']
    assert _events(host, 'exploration.finish_confirmed') == []


def test_cross_attempt_publication_cannot_substitute(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work(routes)])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    _node_session(host, task)
    _publish(host, 'ses_node', 'First attempt summary')
    host.call('finish', session='ses_node', state='finished', details={'reason': 'First attempt summary'})
    assert len(_events(host, 'exploration.finish_confirmed')) == 1
    # A retried attempt with no publication of its own keeps the original path.
    host.call('focus', session='ses_node', node_id=task['node_id'], role='core', mode='auto')
    host.call('finish', session='ses_node', state='finished', details={'reason': 'First attempt summary'})
    assert len(_events(host, 'exploration.finish_confirmed')) == 1
    results = host.call('query', collection='exploration_results', scope_id=scope['scope_id'])
    assert [item['kind'] for item in results['items']] == ['published', 'finished']


def test_pure_confirmation_after_handling_publishes_no_new_todo(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work(routes)])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    _node_session(host, task)
    _publish(host, 'ses_node', 'Handled result')
    snapshot = host.call('query', collection='evidence', refs=[], scope_id=scope['scope_id'])
    host.call('exploration_decide', model_call=True, fields={
        'scope_ref': scope['ref'], 'route_refs': [route['ref'] for route in current(host, scope)],
        'snapshot_id': snapshot['snapshot_id'], 'action': 'probe', 'reason': 'Handle',
        'result_ids': pending(host, scope)})
    assert pending(host, scope) == []
    host.call('finish', session='ses_node', state='finished', details={'reason': 'Handled result'})
    assert pending(host, scope) == []
    store = NativeStore(host.root)
    with store._read() as db:
        notifications = db.execute("SELECT COUNT(*) FROM workflow_notifications WHERE state='pending' "
                                   "AND reference IN (SELECT result_id FROM exploration_results "
                                   "WHERE scope_id=?)", (scope['scope_id'],)).fetchone()[0]
    assert notifications == 0
    # The autonomous segment still completed normally.
    with store._read() as db:
        pause = db.execute("SELECT pause_reason FROM workflow_sessions WHERE session_id='ses_node'").fetchone()[0]
    assert pause == 'finished'
