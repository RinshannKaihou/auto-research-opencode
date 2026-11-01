"""Failure-path regressions from the PR #9/#10 reviews."""
import os

import pytest

from auto_research import controller
from auto_research.errors import ConflictError
from test_runtime_control_repair import project


def node_at_work(tmp_path):
    host, store, node_id = project(tmp_path)
    task = store.workflow('opencode', 'ses_main', 'task', {'node_id': node_id}, 'task')
    store.workflow('opencode', 'ses_main', 'task_session', {'task_id': task['task_id'], 'session_id': 'ses_node'}, 'bind')
    host.call('open', session='ses_node', root=str(host.root), cwd=str(host.root), session_role='node_core', node_id=node_id)
    host.call('focus', session='ses_node', node_id=node_id, role='core', mode='auto')
    store.workflow('opencode', 'ses_main', 'task_state', {'task_id': task['task_id'], 'state': 'running'}, 'running')
    return host, store, task


@pytest.mark.parametrize('outcome', ['failed', 'interrupted'])
def test_unsuccessful_finish_turn_can_finish_again_without_restarting_node(tmp_path, outcome):
    host, store, task = node_at_work(tmp_path)
    first = store.node_finish_request('opencode', 'ses_node', 'finished', 'first result', 'turn-a', 'finish-a')
    store.runtime_execution_end('opencode', 'ses_node', 'end-a', outcome, 'end:ses_node:end-a', 'turn-a')
    assert store.node_finish_pending('opencode', 'ses_node') is None
    # Also repair the stuck pending form written by the previous version.
    with store._connection() as db:
        db.execute("UPDATE workflow_intents SET state='pending' WHERE intent_id=?", (first['intent_id'],))
    # The original receipt remains historical even though its pending intent was cancelled.
    assert store.node_finish_request('opencode', 'ses_node', 'finished', 'first result', 'turn-a', 'finish-a') == first
    second = store.node_finish_request('opencode', 'ses_node', 'finished', 'rechecked result', 'turn-b', 'finish-b')
    assert second['turn_token'] == 'turn-b' and second['intent_id'] != first['intent_id']
    store.runtime_execution_end('opencode', 'ses_node', 'end-b', 'succeeded', 'end:ses_node:end-b', 'turn-b')
    store.node_finish_commit('opencode', 'ses_node', second['intent_id'], 'turn-b', 'end-b', 'commit-b')
    with store._read() as db:
        assert db.execute('SELECT state FROM exploration_tasks WHERE task_id=?', (task['task_id'],)).fetchone()[0] == 'finished'
        assert db.execute('SELECT COUNT(*) FROM exploration_tasks').fetchone()[0] == 1
        assert db.execute('SELECT state FROM workflow_intents WHERE intent_id=?', (first['intent_id'],)).fetchone()[0] == 'cancelled'


def owner_token(result):
    return {key: result[key] for key in ('epoch', 'binding', 'fence')}


def test_pid_reuse_with_a_different_process_start_can_recover_cold(tmp_path, monkeypatch):
    from test_opencode_host import Host
    host = Host(tmp_path)
    host.open()
    identity = {'machine': 'local-machine', 'start': 'old-start'}
    monkeypatch.setattr(controller, 'process_identity', lambda _pid: dict(identity))
    old = host.call('controller_attach', epoch='old', binding='old', pid=os.getpid())
    host.call('workflow', action='run', fields={'state': 'running'}, control_owner=owner_token(old))
    identity['start'] = 'new-start'
    new = host.call('controller_attach', epoch='new', binding='new', pid=os.getpid())
    assert new['mode'] == 'cold' and new['phase'] == 'reconciling'


@pytest.mark.parametrize('alive', [True, None])
def test_possibly_live_owner_has_an_explicit_compare_and_swap_takeover(tmp_path, monkeypatch, alive):
    from test_opencode_host import Host
    host = Host(tmp_path)
    host.open()
    monkeypatch.setattr(controller, '_alive', lambda _pid: alive)
    monkeypatch.setattr(controller, 'process_identity', lambda _pid: None)
    old = host.call('controller_attach', epoch='old', binding='old', pid=os.getpid())
    host.call('workflow', action='run', fields={'state': 'running'}, control_owner=owner_token(old))
    candidate = host.call('controller_attach', epoch='new', binding='new', pid=os.getpid())
    assert candidate['mode'] == 'contended'
    # Recording a contender does not silently fence or pause the current owner.
    assert host.call('controller_check', control_owner=owner_token(old))['phase'] == 'ready'
    assert host.call('status', control_owner=owner_token(candidate))['workflow']['controller']['phase'] == 'recovery_required'
    adopted = host.call('controller_adopt', reason='operator checked prior host', control_owner=owner_token(candidate))
    assert host.call('controller_check', control_owner=owner_token(adopted))['phase'] == 'ready'
    assert host.call('status')['workflow']['run']['state'] == 'paused'
    assert 'fence_mismatch' in host.error('controller_check', control_owner=owner_token(old))


def test_takeover_candidate_cannot_override_a_subsequent_owner_change(tmp_path, monkeypatch):
    from test_opencode_host import Host
    host = Host(tmp_path)
    host.open()
    monkeypatch.setattr(controller, '_alive', lambda _pid: True)
    old = host.call('controller_attach', epoch='old', binding='old', pid=os.getpid())
    candidate = host.call('controller_attach', epoch='new', binding='new', pid=os.getpid())
    newer = host.call('controller_attach', epoch='old', binding='reattached', pid=os.getpid())
    host.call('controller_ready', control_owner=owner_token(newer))
    from auto_research.native_store import NativeStore
    with NativeStore(host.root)._read() as db:
        assert db.execute("SELECT state FROM workflow_intents WHERE kind='controller_candidate'").fetchone()[0] == 'superseded'
    assert 'adoption_not_authorized' in host.error('controller_adopt', reason='outdated check', control_owner=owner_token(candidate))


def test_unsent_create_releases_only_the_same_launch_claim(tmp_path):
    host, store, node = project(tmp_path)
    task = store.workflow('opencode', 'ses_main', 'task', {'node_id': node}, 'task')
    launch = store.node_launch_claim('opencode', 'ses_main', task['task_id'], 1, 'claim')
    store.node_launch_phase('opencode', 'ses_main', launch['intent_id'], 'creating', 'creating')
    assert store.runtime_outbound_not_sent('opencode', 'ses_main', launch['intent_id'], 'create', 'controller preflight refused', 'unsent')['rolled_back']
    retry = store.node_launch_claim('opencode', 'ses_main', task['task_id'], 1, 'claim-again')
    assert retry['claimed'] and retry['intent_id'] != launch['intent_id']
    with pytest.raises(ConflictError):
        store.runtime_outbound_not_sent('opencode', 'ses_main', launch['intent_id'], 'create', 'old check', 'stale-unsent')


def test_unsent_turn_refunds_its_reservation_but_a_bound_turn_cannot_be_refunded(tmp_path):
    host, store, node = project(tmp_path)
    task = store.workflow('opencode', 'ses_main', 'task', {'node_id': node}, 'task')
    launch = store.node_launch_claim('opencode', 'ses_main', task['task_id'], 1, 'claim')
    child = launch['session_id']
    store.node_launch_phase('opencode', 'ses_main', launch['intent_id'], 'created', 'created')
    host.call('open', session=child, root=str(host.root), cwd=str(host.root), session_role='node_core', node_id=node)
    host.call('focus', session=child, node_id=node, role='core', mode='auto')
    store.node_launch_phase('opencode', 'ses_main', launch['intent_id'], 'ready', 'ready')
    reserved = store.runtime_reserve('opencode', child, 'reserve', launch_intent_id=launch['intent_id'], message_id=launch['message_id'])
    assert reserved['allowed']
    store.runtime_execution_start('opencode', child, 'turn', 'start', 1)
    store.runtime_execution_bind('opencode', child, 'turn', launch['message_id'], 'bind', 2)
    with pytest.raises(ConflictError, match='may have started'):
        store.runtime_outbound_not_sent('opencode', 'ses_main', launch['intent_id'], 'synthetic', 'invalid unsent claim', 'refuse')
    # A separate, genuinely unsent coordinator wake can be retried without a
    # user recovery command or a second charge for its reserved turn.
    with store._connection() as db:
        db.execute('INSERT INTO workflow_notifications VALUES(?,?,?,?,?,?,?,?)',
                   ('notice', task['task_id'], 'ses_main', 'finished', None, '{}', 'delivered', 'now'))
    store.workflow('opencode', 'ses_main', 'session', {'waiting': [task['task_id']], 'pause_reason': 'wait'}, 'wait')
    wake = store.notification_wake_begin('opencode', 'ses_main', 'notice', 'wake')
    turn = store.runtime_reserve('opencode', 'ses_main', 'reserve-wake', wake_intent_id=wake['intent_id'], message_id=wake['message_id'])
    assert turn['allowed']
    store.runtime_outbound_not_sent('opencode', 'ses_main', wake['intent_id'], 'synthetic', 'preflight failed', 'undo-wake')
    assert store.runtime_budget_status('opencode', 'ses_main')['turns'] == 0
    again = store.notification_wake_begin('opencode', 'ses_main', 'notice', 'wake-again')
    assert again['send'] and again['message_id'] == wake['message_id']
    with pytest.raises(ConflictError):
        store.runtime_submitted('opencode', 'ses_main', turn['reservation_id'], 'late-submit')


def test_unsent_proof_cannot_overwrite_a_hard_stop_recovery_state(tmp_path):
    host, store, node = project(tmp_path)
    task = store.workflow('opencode', 'ses_main', 'task', {'node_id': node}, 'task')
    launch = store.node_launch_claim('opencode', 'ses_main', task['task_id'], 1, 'claim')
    store.workflow('opencode', 'ses_main', 'run', {'state': 'stopping'}, 'stop')
    with pytest.raises(ConflictError):
        store.runtime_outbound_not_sent('opencode', 'ses_main', launch['intent_id'], 'create', 'late preflight failure', 'undo')
    with store._read() as db:
        assert db.execute('SELECT state FROM exploration_tasks').fetchone()[0] == 'unverified'


def test_native_terminal_does_not_clear_unknown_external_exit_after_a_deadline(tmp_path):
    host, store, task = node_at_work(tmp_path)
    with store._connection() as db:
        db.execute("UPDATE exploration_tasks SET state='unverified',error='wall_clock_budget: exit not yet verified'")
    store.runtime_execution_end('opencode', 'ses_node', 'late-terminal', 'interrupted', 'late-end', 'old-turn')
    with store._read() as db:
        assert db.execute('SELECT state FROM exploration_tasks WHERE task_id=?', (task['task_id'],)).fetchone()[0] == 'unverified'


def test_unsent_standalone_turn_refund_rejects_a_bound_or_submitted_request(tmp_path):
    host, store, _ = project(tmp_path)
    first = store.runtime_reserve('opencode', 'ses_main', 'nudge', message_id='msg_nudge')
    assert store.runtime_turn_not_sent('opencode', 'ses_main', 'msg_nudge', 'refund')['refunded']
    assert store.runtime_budget_status('opencode', 'ses_main')['turns'] == 0
    assert store.runtime_turn_not_sent('opencode', 'ses_main', 'msg_nudge', 'refund-again')['refunded']
    with pytest.raises(ConflictError):
        store.runtime_submitted('opencode', 'ses_main', first['reservation_id'], 'late-submit')
    second = store.runtime_reserve('opencode', 'ses_main', 'next', message_id='msg_next')
    store.runtime_execution_start('opencode', 'ses_main', 'next-turn', 'start-next', 1)
    store.runtime_execution_bind('opencode', 'ses_main', 'next-turn', 'msg_next', 'bind-next', 2)
    with pytest.raises(ConflictError):
        store.runtime_turn_not_sent('opencode', 'ses_main', 'msg_next', 'bound-refund')
    store.runtime_reserve('opencode', 'ses_main', 'submitted', message_id='msg_submitted')
    with store._read() as db:
        rid = db.execute("SELECT reservation_id FROM runtime_budget_reservations WHERE operation_id='submitted'").fetchone()[0]
    store.runtime_submitted('opencode', 'ses_main', rid, 'submitted-confirm')
    with pytest.raises(ConflictError):
        store.runtime_turn_not_sent('opencode', 'ses_main', 'msg_submitted', 'submitted-refund')


def test_execution_view_ignores_prior_ambiguous_authorizations_but_retains_future_work(tmp_path):
    host, store, _ = project(tmp_path)
    store.runtime_reserve('opencode', 'ses_main', 'old', message_id='msg_old')
    store.runtime_execution_start('opencode', 'ses_main', 'current', 'start', 1, ambiguous=True)
    assert not store.runtime_execution_bind('opencode', 'ses_main', 'current', 'msg_old', 'ambiguous-bind', 2)['matched']
    assert not store.runtime_execution_view('opencode', 'ses_main')['has_unbound_turn']
    assert store.runtime_execution_view('opencode', 'ses_main', 'missing') is None
    store.runtime_reserve('opencode', 'ses_main', 'future', message_id='msg_future')
    assert store.runtime_execution_view('opencode', 'ses_main')['has_unbound_turn']
    store.runtime_turn_not_sent('opencode', 'ses_main', 'msg_future', 'refund-future')
    assert not store.runtime_execution_view('opencode', 'ses_main')['has_unbound_turn']
    with store._read() as db:
        assert db.execute("SELECT state FROM workflow_intents WHERE intent_id='execution-start:current'").fetchone()[0] == 'open'
