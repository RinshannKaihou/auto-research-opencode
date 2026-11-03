"""B1: read-only work-grouped exploration_inbox over per-result obligations."""

import json

from test_opencode_host import Host
from test_e3_multiroute import setup, decision, work, publish, current, pending
from auto_research.native_store import NativeStore


def _rows(store):
    with store._read() as db:
        return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table}')]
                for table in ('exploration_results', 'exploration_decisions', 'workflow_intents',
                              'workflow_project', 'publications', 'attempts')}


def test_publication_and_finish_of_one_attempt_group_together(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work(routes)])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    host.call('workflow', action='task_session', fields={'task_id': task['task_id'], 'session_id': 'ses_node'})
    host.call('open', session='ses_node', root=str(host.root), cwd=str(host.root),
              session_role='node_core', node_id=task['node_id'])
    host.call('focus', session='ses_node', node_id=task['node_id'], role='core', mode='auto')
    publication = host.call('publish', session='ses_node', status='complete',
                            summary='Shared probe measurement', gaps=[], knowledge_refs=[],
                            items=[{'item_id': 'r', 'kind': 'text', 'content': {'text': 'Measurement'}}])
    ref = 'pub/' + publication['publication_id']
    # A finish that adds information beyond the publication stays its own
    # result (B2 keeps it) and joins the same attempt's work group.
    host.call('finish', session='ses_node', state='finished',
              details={'reason': 'Adds limits beyond the publication'})
    page = host.call('query', collection='exploration_inbox', scope_id=scope['scope_id'])
    assert page['semantics'] == 'exploration-inbox-v1'
    groups = [group for group in page['items'] if group['source'] == 'work']
    assert len(groups) == 1
    group = groups[0]
    assert group['identity_status'] == 'verified'
    assert group['work_identity']['intent_id'] == chosen['intent_ids'][0]
    assert group['members_total'] == 2
    assert set(group['kinds']) == {'published', 'finished'}
    assert group['publication_refs'] == [ref.rsplit('#', 1)[0]]
    assert group['pending_members'] == 2
    assert page['pending_groups_total'] == 1
    # The per-result obligation count is separate from the group count.
    results = host.call('query', collection='exploration_results', scope_id=scope['scope_id'])
    assert results['total'] == 2


def test_second_publication_joins_the_same_group(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work(routes)])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    host.call('workflow', action='task_session', fields={'task_id': task['task_id'], 'session_id': 'ses_node'})
    host.call('open', session='ses_node', root=str(host.root), cwd=str(host.root),
              session_role='node_core', node_id=task['node_id'])
    host.call('focus', session='ses_node', node_id=task['node_id'], role='core', mode='auto')
    first = host.call('publish', session='ses_node', status='complete', summary='First',
                      gaps=[], knowledge_refs=[], items=[{'item_id': 'r', 'kind': 'text',
                      'content': {'text': 'First'}}])
    second = host.call('publish', session='ses_node', status='complete', summary='Second',
                       gaps=[], knowledge_refs=[], items=[{'item_id': 'r', 'kind': 'text',
                       'content': {'text': 'Second'}}])
    host.call('finish', session='ses_node', state='finished',
              details={'reason': 'Second, with open limits noted'})
    page = host.call('query', collection='exploration_inbox', scope_id=scope['scope_id'])
    group = [group for group in page['items'] if group['source'] == 'work'][0]
    assert group['members_total'] == 3
    assert f'pub/{first["publication_id"]}' in group['publication_refs']
    assert f'pub/{second["publication_id"]}' in group['publication_refs']


def test_retried_node_attempts_stay_in_separate_groups(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work(routes)])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    publish(host, task, 'ses_node', 'First attempt')
    # A retry: the same node is focused again, creating a new live attempt.
    host.call('focus', session='ses_node', node_id=task['node_id'], role='core', mode='auto')
    host.call('publish', session='ses_node', status='complete', summary='Retry attempt',
              gaps=[], knowledge_refs=[], items=[{'item_id': 'r', 'kind': 'text',
              'content': {'text': 'Retry'}}])
    host.call('finish', session='ses_node', state='finished',
              details={'reason': 'Retry attempt, one gap remains'})
    page = host.call('query', collection='exploration_inbox', scope_id=scope['scope_id'])
    work_groups = [group for group in page['items'] if group['source'] == 'work']
    assert len(work_groups) == 2
    # The first attempt's verbatim-same finish was merged (publication only);
    # the retry's finish carried new information and stays its own result.
    assert sorted(group['members_total'] for group in work_groups) == [1, 2]
    attempts = {group['work_identity']['attempt_id'] for group in work_groups}
    assert len(attempts) == 2 and None not in attempts


def test_large_group_pages_all_members_and_ref_is_not_a_scientific_result(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work(routes)])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    host.call('workflow', action='task_session', fields={'task_id': task['task_id'], 'session_id': 'ses_node'})
    host.call('open', session='ses_node', root=str(host.root), cwd=str(host.root),
              session_role='node_core', node_id=task['node_id'])
    host.call('focus', session='ses_node', node_id=task['node_id'], role='core', mode='auto')
    for index in range(3):
        host.call('publish', session='ses_node', status='complete', summary=f'Part {index}',
                  gaps=[], knowledge_refs=[], items=[{'item_id': 'r', 'kind': 'text',
                  'content': {'text': f'Part {index}'}}])
    host.call('finish', session='ses_node', state='finished', details={'reason': 'Parts done'})
    page = host.call('query', collection='exploration_inbox', scope_id=scope['scope_id'])
    group = [group for group in page['items'] if group['source'] == 'work'][0]
    single = host.call('query', collection='exploration_inbox', ref=group['group_key'],
                       scope_id=scope['scope_id'], limit=2)
    collected = list(single['items'])
    cursor = single['cursor']
    while cursor is not None:
        nxt = host.call('query', collection='exploration_inbox', ref=group['group_key'],
                        scope_id=scope['scope_id'], limit=2, cursor=cursor)
        collected += nxt['items']
        cursor = nxt['cursor']
    assert len(collected) == group['members_total'] == 4
    # A group key is not a result ID and never satisfies the decide contract.
    snapshot = host.call('query', collection='evidence', refs=[], scope_id=scope['scope_id'])
    error = host.error('exploration_decide', model_call=True, fields={
        'scope_ref': scope['ref'], 'route_refs': [route['ref'] for route in current(host, scope)],
        'snapshot_id': snapshot['snapshot_id'], 'action': 'defer', 'reason': 'Close',
        'result_ids': [group['group_key']]})
    assert 'exploration_result_changed' in error


def test_members_arriving_after_a_read_are_not_silently_confirmed(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work(routes)])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    host.call('workflow', action='task_session', fields={'task_id': task['task_id'], 'session_id': 'ses_node'})
    host.call('open', session='ses_node', root=str(host.root), cwd=str(host.root),
              session_role='node_core', node_id=task['node_id'])
    host.call('focus', session='ses_node', node_id=task['node_id'], role='core', mode='auto')
    for summary in ('First part', 'Second part'):
        host.call('publish', session='ses_node', status='complete', summary=summary,
                  gaps=[], knowledge_refs=[], items=[{'item_id': 'r', 'kind': 'text',
                  'content': {'text': summary}}])
    stale = host.call('query', collection='exploration_inbox', scope_id=scope['scope_id'])
    stale_group = [group for group in stale['items'] if group['source'] == 'work'][0]
    stale_members = host.call('query', collection='exploration_inbox', ref=stale_group['group_key'],
                              scope_id=scope['scope_id'], limit=1)
    assert stale_members['cursor'] is not None
    host.call('finish', session='ses_node', state='finished', details={'reason': 'Parts done'})
    # The finish arrived after the read: the old member cursor no longer matches.
    refreshed = 'read_view_changed' in host.error('query', collection='exploration_inbox',
        ref=stale_group['group_key'], scope_id=scope['scope_id'], limit=1,
        cursor=stale_members['cursor'])
    assert refreshed
    # A decision citing only the pre-read members leaves the late member pending.
    snapshot = host.call('query', collection='evidence', refs=[], scope_id=scope['scope_id'])
    host.call('exploration_decide', model_call=True, fields={
        'scope_ref': scope['ref'], 'route_refs': [route['ref'] for route in current(host, scope)],
        'snapshot_id': snapshot['snapshot_id'], 'action': 'probe', 'reason': 'Partial read',
        'result_ids': stale_members['items']})
    page = host.call('query', collection='exploration_inbox', scope_id=scope['scope_id'])
    group = [group for group in page['items'] if group['source'] == 'work'][0]
    # The uncited read-time member and the late finish member both stay pending.
    assert group['pending_members'] == 2
    assert group['handled_members'] == 1
    assert group['members_total'] == 3


def test_two_cancelled_intents_stay_separate_verified_groups(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work([routes[0]], 'one'), work([routes[1]], 'two')])
    # Turning exploration off settles both unstarted works before materialization.
    host.call('exploration_policy', mode='off')
    page = host.call('query', collection='exploration_inbox', scope_id=scope['scope_id'])
    groups = [group for group in page['items'] if group['source'] == 'work']
    assert len(groups) == 2
    by_intent = {group['work_identity'].get('intent_id'): group for group in groups}
    assert set(by_intent) == set(chosen['intent_ids'])
    for group in groups:
        assert group['identity_status'] == 'verified'
        assert group['members_total'] == 1
        assert group['execution_state'] == 'settled:cancelled'
    assert page['pending_groups_total'] == 2


def test_retry_does_not_rewrite_the_historical_attempt_state(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, next_work=work(routes))
    task = host.call('exploration_materialize', intent_id=chosen['intent_id'])
    publish(host, task, 'ses_node', 'First attempt result')

    def work_groups():
        return {group['work_identity']['attempt_id']: group
                for group in host.call('query', collection='exploration_inbox',
                                       scope_id=scope['scope_id'])['items']
                if group['source'] == 'work'}

    before = work_groups()
    first_attempt = next(iter(before))
    assert before[first_attempt]['execution_state'] == 'finished'
    assert before[first_attempt]['attempt_state'] == 'finished'

    # Resume the same task under a second attempt and publish again while it runs.
    host.call('focus', session='ses_node', node_id=task['node_id'], role='core', mode='auto')
    retry = host.call('status', session='ses_node')['attempt']
    host.call('workflow', session='ses_node', action='task_resumed', fields={
        'task_id': task['task_id'], 'attempt_id': retry['attempt_id'],
        'retry_of': first_attempt})
    host.call('publish', session='ses_node', status='complete', summary='Retry result',
              gaps=[], knowledge_refs=[], items=[{'item_id': 'r', 'kind': 'text',
              'content': {'text': 'Retry'}}])
    during = work_groups()
    assert set(during) == {first_attempt, retry['attempt_id']}
    # The historical group keeps its own terminal state; only the retry group
    # shows the ongoing execution, with the shared task state listed apart.
    assert during[first_attempt]['execution_state'] == 'finished'
    assert during[first_attempt]['attempt_state'] == 'finished'
    assert during[retry['attempt_id']]['execution_state'] == 'open'
    assert during[retry['attempt_id']]['attempt_state'] == 'open'
    assert all(group['task_state'] == 'running' for group in during.values())

    host.call('finish', session='ses_node', state='finished',
              details={'reason': 'Retry complete with extra limits'})
    after = work_groups()
    assert after[first_attempt]['execution_state'] == 'finished'
    assert after[retry['attempt_id']]['execution_state'] == 'finished'


def test_unresolvable_identity_groups_one_result_each(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    chosen = decision(host, scope, routes, work_batch=[work(routes)])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    host.call('workflow', action='task_session', fields={'task_id': task['task_id'], 'session_id': 'ses_node'})
    host.call('open', session='ses_node', root=str(host.root), cwd=str(host.root),
              session_role='node_core', node_id=task['node_id'])
    host.call('focus', session='ses_node', node_id=task['node_id'], role='core', mode='auto')
    host.call('publish', session='ses_node', status='complete', summary='Shared measurement',
              gaps=[], knowledge_refs=[], items=[{'item_id': 'r', 'kind': 'text',
              'content': {'text': 'Measurement'}}])
    # A result whose recorded identity cannot be verified (neither intent nor
    # resolvable node binding) must not merge with any other work.
    store = NativeStore(host.root)
    with store._connection() as db:
        from auto_research.exploration_store import ExplorationStore
        ExplorationStore._exploration_result_in_scope(store, db, scope['scope_id'],
            'intent:exploration-work:ED-999:9:cancelled', 'cancelled', None,
            {'intent_id': 'exploration-work:ED-999:9', 'route_ids': []})
        db.commit()
    page = host.call('query', collection='exploration_inbox', scope_id=scope['scope_id'])
    unknown = [group for group in page['items'] if group['identity_status'] == 'unknown']
    assert len(unknown) == 1
    assert unknown[0]['members_total'] == 1
    assert unknown[0]['work_identity'].get('unresolved') is True
    known = [group for group in page['items'] if group['identity_status'] == 'verified']
    assert known and known[0]['members_total'] >= 1


def test_scope_state_changes_form_their_own_source_group_and_reads_do_not_write(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    # A pending result keeps the state change on the conservative path (B3
    # only skips the self-confirmation for scopes with nothing left to process).
    chosen = decision(host, scope, routes, work_batch=[work([routes[0]], 'measured')])
    task = host.call('exploration_materialize', intent_id=chosen['intent_ids'][0])
    publish(host, task, 'ses_node', 'A measurement')
    host.call('exploration_scope', model_call=True, fields={'scope_ref': scope['ref'],
        'reason': 'Park it', 'changes': {'state': 'deferred', 'reactivation_condition': 'New data'}})
    store = NativeStore(host.root)
    before = _rows(store)
    page = host.call('query', collection='exploration_inbox')
    after = _rows(store)
    assert before == after
    scoped = [group for group in page['items'] if group['source'] == 'scope']
    assert len(scoped) == 1
    assert scoped[0]['identity_status'] == 'source'
    assert scoped[0]['work_identity']['source_key'].startswith('state:')
    assert scoped[0]['execution_state'] == 'registered'
    unknown = 'Unknown exploration_inbox group' in host.error('query', collection='exploration_inbox',
                                                              ref='XG-doesnotexist')
    assert unknown


def test_inbox_lists_pending_first_then_source_order(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    selected = decision(host, scope, routes, work_batch=[work(routes, f'work-{i}') for i in range(3)])
    tasks = [host.call('exploration_materialize', intent_id=ident) for ident in selected['intent_ids']]
    for index in (2, 0, 1):
        publish(host, tasks[index], f'ses_{index}', f'Result {index}')
    groups = host.call('query', collection='exploration_inbox', scope_id=scope['scope_id'])['items']
    assert [group['work_identity']['intent_id'] for group in groups] == [
        selected['intent_ids'][index] for index in (2, 0, 1)]
    decision(host, scope, current(host, scope), action='defer', result_ids=groups[0]['result_ids'])
    page = host.call('query', collection='exploration_inbox', scope_id=scope['scope_id'], limit=1)
    first = page['items'][0]
    assert first['pending']
    assert first['work_identity']['intent_id'] == selected['intent_ids'][0]
    second = host.call('query', collection='exploration_inbox', scope_id=scope['scope_id'],
                       limit=1, cursor=page['cursor'])
    assert second['items'][0]['work_identity']['intent_id'] == selected['intent_ids'][1]
    third = host.call('query', collection='exploration_inbox', scope_id=scope['scope_id'],
                      limit=1, cursor=second['cursor'])
    assert not third['items'][0]['pending']
    assert third['items'][0]['work_identity']['intent_id'] == selected['intent_ids'][2]
