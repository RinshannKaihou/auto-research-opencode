"""Regression coverage for research control and dispatch recovery."""
import sqlite3
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from auto_research.errors import ConflictError, ResearchControlError, ResearchExecutionUnverified
from auto_research.native_store import NativeStore
from auto_research import schema12
from test_opencode_host import Host


def project(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call("focus", node_id=None, role="planner", mode="auto")
    host.call("workflow", action="run", fields={"state": "running"})
    node = host.call("propose", question="Q?", why_now="now", plan="measure", root_reason="initial")
    return host, NativeStore(host.root), node["node_id"]


def test_paused_dispatch_is_non_retryable_until_explicit_resume(tmp_path):
    host, store, node_id = project(tmp_path)
    host.call("workflow", action="run", fields={"state": "paused"})
    host.call("workflow", action="session", fields={"pause_reason": "cold"})
    for attempt in range(3):
        with pytest.raises(ResearchControlError) as caught:
            store.workflow("opencode", "ses_main", "task", {"node_id": node_id}, f"blocked-{attempt}")
        error = caught.value
        assert error.code == "research_control_paused"
        assert error.details["run_state"] == "paused"
        assert error.details["pause_reason"] == "cold"
        assert error.details["control_revision"] is not None
        assert error.details["retryable"] is False
        assert error.details["recovery"] == "explicit_resume"
        assert error.details["allow_fallback"] is False
        assert "/research resume" in str(error)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM exploration_tasks").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM runtime_budget_reservations").fetchone()[0] == 0
    host.call("workflow", action="run", fields={"state": "running"})
    host.call("workflow", action="session", fields={"pause_reason": None})
    task = store.workflow("opencode", "ses_main", "task", {"node_id": node_id}, "accepted")
    assert task["state"] == "queued"
    assert task["node_id"] == node_id


def test_session_stop_blocks_dispatch_even_when_project_runs(tmp_path):
    host, store, node_id = project(tmp_path)
    host.call("workflow", action="session", fields={"pause_reason": "native_stop"})
    with pytest.raises(ResearchControlError) as caught:
        store.workflow("opencode", "ses_main", "task", {"node_id": node_id}, "blocked")
    assert caught.value.details["pause_reason"] == "native_stop"
    host.call("workflow", action="session", fields={"pause_reason": "wait"})
    task = store.workflow("opencode", "ses_main", "task", {"node_id": node_id}, "accepted")
    assert task["state"] == "queued"


def test_schema11_migration_preserves_history_and_original_receipts(tmp_path):
    host, store, node_id = project(tmp_path)
    task = store.workflow("opencode", "ses_main", "task", {"node_id": node_id}, "original-dispatch")
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TRIGGER control_project_revision")
        db.execute("DROP TRIGGER control_session_revision")
        db.execute("DROP TRIGGER control_session_insert_revision")
        db.execute("DROP TRIGGER control_session_delete_revision")
        db.execute("ALTER TABLE workflow_project DROP COLUMN control_revision")
        db.execute("PRAGMA user_version=11")
        event_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    read_only = NativeStore(host.root, readonly=True)
    assert read_only.control_state("opencode", "ses_main")["schema_version"] == 11
    upgraded = NativeStore(host.root)
    assert upgraded.workflow("opencode", "ses_main", "task", {"node_id": node_id}, "original-dispatch") == task
    with upgraded._read() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 12
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == event_count
        assert db.execute("SELECT control_revision FROM workflow_project").fetchone()[0] == 0
    with sqlite3.connect(store.meta / "schema-11-backup.sqlite3") as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 11
        assert backup.execute("SELECT COUNT(*) FROM events").fetchone()[0] == event_count
    schema12.migrate_schema12(store.db_path)


def test_control_revision_tracks_run_and_session_changes(tmp_path):
    host, store, _ = project(tmp_path)
    def revision():
        with store._read() as db:
            return db.execute("SELECT control_revision FROM workflow_project").fetchone()[0]
    initial = revision()
    host.call("workflow", action="run", fields={"state": "paused"})
    assert revision() == initial + 1
    host.call("workflow", action="session", fields={"pause_reason": "cold"})
    assert revision() == initial + 2
    host.call("workflow", action="session", fields={"pause_reason": "cold"})
    assert revision() == initial + 2
    host.call("workflow", action="run", fields={"state": "running"})
    assert revision() == initial + 3


def test_pause_atomically_blocks_new_turn_and_preserves_old_receipt(tmp_path):
    host, store, _ = project(tmp_path)
    first = store.runtime_reserve('opencode', 'ses_main', 'first-turn')
    host.call('workflow', action='run', fields={'state': 'paused'})
    # Replaying a receipt preserves its historical response; it is not a new
    # authorization to send another message to the host.
    assert store.runtime_reserve('opencode', 'ses_main', 'first-turn') == first
    with pytest.raises(ResearchControlError):
        store.runtime_reserve('opencode', 'ses_main', 'new-turn')
    with store._read() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_budget_reservations').fetchone()[0] == 1


def test_model_propose_control_checked_in_the_write_transaction(tmp_path):
    host, store, _ = project(tmp_path)
    arguments = dict(question='next?', why_now='now', plan='measure', root_reason='new',
                     enforce_protocol=True, control_actor=('opencode', 'ses_main'))
    first = store.propose(request_id='proposal-replay', **arguments)
    host.call('workflow', action='session', fields={'pause_reason': 'native_stop'})
    assert store.propose(request_id='proposal-replay', **arguments) == first
    with pytest.raises(ResearchControlError):
        store.propose(request_id='new-proposal', **arguments)
    with store._read() as db:
        assert db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0] == 2


def test_uncertain_notification_is_not_silently_delivered_or_resent(tmp_path):
    host, store, node_id = project(tmp_path)
    task = store.workflow('opencode', 'ses_main', 'task', {'node_id': node_id}, 'task')
    with store._connection() as db:
        db.execute("INSERT INTO workflow_notifications VALUES(?,?,?,?,?,?,?,?)",
                   ('notice-1', task['task_id'], 'ses_main', 'finished', None, '{}', 'pending', 'now'))
    first = store.notification_begin('opencode', 'ses_main', 'notice-1', 'first-begin', 'report')
    assert first['send'] is True
    assert first['message_id'].startswith('msg_')
    # A lost response from the host is not proof that delivery failed.
    second = store.notification_begin('opencode', 'ses_main', 'notice-1', 'next-begin', 'report')
    assert second['send'] is False and second['message_id'] == first['message_id']
    with store._read() as db:
        assert db.execute("SELECT state FROM workflow_notifications WHERE notification_id='notice-1'").fetchone()[0] == 'pending'
    confirmed = store.notification_confirm('opencode', 'ses_main', first['intent_id'], first['message_id'], 'confirm')
    assert confirmed['state'] == 'delivered'
    assert store.notification_confirm('opencode', 'ses_main', first['intent_id'], first['message_id'], 'confirm') == confirmed


def test_user_checked_missing_notification_reuses_same_message_and_content(tmp_path):
    host, store, node_id = project(tmp_path)
    task = store.workflow('opencode', 'ses_main', 'task', {'node_id': node_id}, 'task')
    with store._connection() as db:
        db.execute('INSERT INTO workflow_notifications VALUES(?,?,?,?,?,?,?,?)',
                   ('notice-2', task['task_id'], 'ses_main', 'finished', None, '{}', 'pending', 'now'))
    first = store.notification_begin('opencode', 'ses_main', 'notice-2', 'begin', 'original report')
    assert store.recovery_resolve_delivery('opencode', 'ses_main', 'notice-2', 'report', 'not_sent', 'checked host inbox', 'checked')['state'] == 'retryable'
    retry = store.notification_begin('opencode', 'ses_main', 'notice-2', 'retry-begin', 'changed text')
    assert retry['send'] and retry['message_id'] == first['message_id']
    assert retry['text'] == 'original report'
    assert store.notification_confirm('opencode', 'ses_main', retry['intent_id'], retry['message_id'], 'confirmed')['state'] == 'delivered'


def test_launch_claim_atomically_reserves_a_concurrency_slot(tmp_path):
    host, store, first_node = project(tmp_path)
    second_node = host.call('propose', question='other?', why_now='now', plan='measure', root_reason='initial')['node_id']
    first = store.workflow('opencode', 'ses_main', 'task', {'node_id': first_node}, 'task-a')
    second = store.workflow('opencode', 'ses_main', 'task', {'node_id': second_node}, 'task-b')
    def claim(task):
        return store.node_launch_claim('opencode', 'ses_main', task['task_id'], 1, 'claim-' + task['task_id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [first, second]))
    assert sum(result['claimed'] for result in results) == 1
    assert {result.get('reason') for result in results if not result['claimed']} == {'capacity'}
    winner = next(result for result in results if result['claimed'])
    assert winner['session_id'].startswith('ses_') and winner['message_id'].startswith('msg_')
    again = store.node_launch_claim('opencode', 'ses_main', winner['task_id'], 1, 'another-claim')
    assert again == {'claimed': False, 'reason': 'already_claimed', 'state': 'starting'}


def test_specialist_launch_replay_cannot_create_a_second_execution(tmp_path):
    host, store, node_id = project(tmp_path)
    fields = {
        'parent_session_id': 'ses_main', 'node_id': node_id, 'purpose': 'review',
        'label': 'independent check', 'prompt': 'check the report', 'inputs': [],
        'launch': {'tools': ['read'], 'deadline_at': 2000000000000,
                   'session_id': 'ses_specialist_stable', 'message_id': 'msg_specialist_stable'},
    }
    first = store.specialist_create(fields, 'specialist-op')
    intent = first['launch']['intent_id']
    claim = store.specialist_launch_phase('opencode', 'ses_main', intent, 'creating', 'first-claim')
    duplicate = store.specialist_launch_phase('opencode', 'ses_main', intent, 'creating', 'second-claim')
    assert claim['claimed'] is True and duplicate['claimed'] is False
    assert store.specialist_cancel_unstarted('opencode', 'ses_main', first['task_id'], 'cancel-claimed') == {
        'cancelled': False, 'state': 'starting', 'launch_state': 'creating'}
    assert duplicate['session_id'] == fields['launch']['session_id']
    assert duplicate['message_id'] == fields['launch']['message_id']
    # Retrying the original user intent preserves the original deadline and ID;
    # it does not authorize a second session after a lost create response.
    replay = store.specialist_create({**fields, 'launch': {**fields['launch'], 'deadline_at': 2000001000000}}, 'specialist-op')
    assert replay == first
    with pytest.raises(ConflictError):
        store.specialist_create({**fields, 'launch': {**fields['launch'], 'tools': ['read', 'grep']}}, 'specialist-op')
    with store._read() as db:
        assert db.execute('SELECT COUNT(*) FROM specialist_tasks').fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM workflow_intents WHERE kind='specialist_launch'").fetchone()[0] == 1


def test_specialist_cancellation_and_binding_require_the_expected_launch_phase(tmp_path):
    _, store, node_id = project(tmp_path)
    fields = {
        'parent_session_id': 'ses_main', 'node_id': node_id, 'purpose': 'review',
        'label': 'review', 'prompt': 'check', 'inputs': [], 'fanout_limit': 2,
        'launch': {'tools': ['read'], 'deadline_at': 2000000000000,
                   'session_id': 'ses_prepared', 'message_id': 'msg_prepared'},
    }
    prepared = store.specialist_create(fields, 'prepared')
    assert store.specialist_cancel_unstarted('opencode', 'ses_main', prepared['task_id'], 'cancel')['cancelled']
    with pytest.raises(ConflictError):
        store.specialist_bind({'task_id': prepared['task_id'], 'parent_session_id': 'ses_main',
                               'session_id': 'ses_prepared'}, 'late-bind')
    later = store.specialist_create({**fields, 'launch': {**fields['launch'], 'session_id': 'ses_next'}}, 'next')
    with pytest.raises(ConflictError):
        store.specialist_bind({'task_id': later['task_id'], 'parent_session_id': 'ses_main',
                               'session_id': 'ses_next'}, 'before-create')
    store.specialist_finish({'task_id': later['task_id'], 'state': 'incomplete', 'exit_verified': True}, 'settle')
    with pytest.raises(ConflictError):
        store.specialist_finish({'task_id': later['task_id'], 'state': 'completed', 'exit_verified': True}, 'late-settle')


def test_generic_task_updates_cannot_bypass_launch_or_user_recovery(tmp_path):
    host, store, node_id = project(tmp_path)
    task = store.workflow('opencode', 'ses_main', 'task', {'node_id': node_id}, 'task')
    launch = store.node_launch_claim('opencode', 'ses_main', task['task_id'], 1, 'claim')
    host.call('open', session=launch['session_id'], root=str(host.root), cwd=str(host.root),
              session_role='node_core', node_id=node_id)
    for target in ('running', 'finished', 'failed', 'cancelled', 'queued'):
        with pytest.raises(ConflictError):
            store.workflow('opencode', 'ses_main', 'task_state', {'task_id': task['task_id'], 'state': target}, 'invalid-' + target)
    store.workflow('opencode', 'ses_main', 'task_state', {'task_id': task['task_id'], 'state': 'unverified'}, 'unknown')
    assert host.call('status')['workflow']['unverified']['tasks'] == [{'task_id': task['task_id'], 'node_id': node_id}]
    assert '执行状态未核实' in host.error('runtime_reserve', session=launch['session_id'])
    with pytest.raises(ResearchExecutionUnverified) as caught:
        store.runtime_reserve('opencode', launch['session_id'], 'blocked-reservation')
    assert caught.value.details['recovery'] == 'inspect_execution'
    assert caught.value.details['task_id'] == task['task_id']
    assert caught.value.details['controller_phase'] == 'unrecorded'
    assert caught.value.details['allow_fallback'] is False
    for action, fields in [('task_state', {'task_id': task['task_id'], 'state': 'failed'}),
                           ('task_retry', {'task_id': task['task_id'], 'absent_verified': True}),
                           ('task_verified_failed', {'task_id': task['task_id']})]:
        with pytest.raises(ConflictError):
            store.workflow('opencode', 'ses_main', action, fields, 'bypass-' + action)


def test_launch_delivery_auth_and_confirmation_survive_pause(tmp_path):
    host, store, node_id = project(tmp_path)
    task = store.workflow('opencode', 'ses_main', 'task', {'node_id': node_id}, 'task')
    launch = store.node_launch_claim('opencode', 'ses_main', task['task_id'], 1, 'claim')
    intent = launch['intent_id']
    store.node_launch_phase('opencode', 'ses_main', intent, 'creating', 'creating')
    store.node_launch_phase('opencode', 'ses_main', intent, 'created', 'created')
    child = launch['session_id']
    host.call('open', session=child, root=str(host.root), cwd=str(host.root), session_role='node_core', node_id=node_id)
    host.call('focus', session=child, node_id=node_id, role='core', mode='auto')
    store.node_launch_phase('opencode', 'ses_main', intent, 'ready', 'ready')
    reservation = store.runtime_reserve('opencode', child, 'reserve', launch_intent_id=intent)
    assert reservation['allowed']
    host.call('workflow', action='run', fields={'state': 'paused'})
    # An authorization obtained before pause can be settled as a fact, but
    # the same launch cannot authorize a second host submission.
    with pytest.raises(ConflictError, match='not authorized'):
        store.runtime_reserve('opencode', child, 'reserve-again', launch_intent_id=intent)
    submitted = store.node_launch_submitted('opencode', 'ses_main', intent, launch['message_id'], str(host.root), 'submitted')
    assert submitted['state'] == 'submitted'
    with store._read() as db:
        assert db.execute('SELECT state FROM exploration_tasks WHERE task_id=?', (task['task_id'],)).fetchone()[0] == 'running'
        assert db.execute('SELECT COUNT(*) FROM runtime_budget_reservations').fetchone()[0] == 1


def test_controller_reattach_fences_old_binding_without_pausing(tmp_path):
    host = Host(tmp_path)
    host.open()
    first = host.call('controller_attach', epoch='process-a', binding='binding-a', pid=os.getpid())
    old = {key: first[key] for key in ('epoch', 'binding', 'fence')}
    host.call('workflow', action='run', fields={'state': 'running'}, control_owner=old)
    newer = host.call('controller_attach', epoch='process-a', binding='binding-b', pid=os.getpid())
    current = {key: newer[key] for key in ('epoch', 'binding', 'fence')}
    assert newer['mode'] == 'reattach' and newer['phase'] == 'reconciling' and newer['fence'] > first['fence']
    assert 'controller_fence_mismatch' in host.error('workflow', action='session', fields={'pause_reason': 'fault'}, control_owner=old)
    host.call('controller_ready', control_owner=current)
    assert host.call('controller_check', control_owner=current)['phase'] == 'ready'
    assert host.call('status')['workflow']['run']['state'] == 'running'
    # Another realm cannot quietly steal a still-live process's controller.
    candidate = host.call('controller_attach', epoch='other', binding='other', pid=os.getpid())
    assert candidate['phase'] == 'recovery_required' and candidate['mode'] == 'contended'
    assert host.call('controller_check', control_owner=current)['phase'] == 'ready'


def test_legacy_running_owner_is_unknown_not_a_fabricated_cold_restart(tmp_path):
    host, _, _ = project(tmp_path)
    attached = host.call('controller_attach', epoch='process-a', binding='binding-a', pid=os.getpid())
    assert attached['phase'] == 'recovery_required'
    assert attached['mode'] == 'legacy_unknown'
    state = host.call('status')['workflow']
    assert state['run']['state'] == 'running' and state['controller']['phase'] == 'recovery_required'
    token = {key: attached[key] for key in ('epoch', 'binding', 'fence')}
    assert 'recovery_required' in host.error('controller_check', control_owner=token)
    assert 'adoption_not_authorized' in host.error('controller_adopt', reason='checked')
    adopted = host.call('controller_adopt', reason='operator inspected the old host', control_owner=token)
    assert adopted['phase'] == 'ready' and adopted['run_state'] == 'paused'
    assert host.call('status')['workflow']['run']['state'] == 'paused'
    assert host.call('controller_check', control_owner=token)['phase'] == 'ready'


def test_dead_owner_is_a_cold_restart_that_requires_reconciliation(tmp_path):
    host = Host(tmp_path)
    host.open()
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        first = host.call('controller_attach', epoch='old-process', binding='old', pid=child.pid)
        old = {key: first[key] for key in ('epoch', 'binding', 'fence')}
        host.call('workflow', action='run', fields={'state': 'running'}, control_owner=old)
        child.terminate()
        child.wait(timeout=5)
        attached = host.call('controller_attach', epoch='new-process', binding='new', pid=os.getpid())
        assert attached['mode'] == 'cold' and attached['phase'] == 'reconciling'
        # A second attachment before reconciliation completes must continue the
        # cold recovery, rather than quietly treating it as a warm reload.
        attached = host.call('controller_attach', epoch='new-process', binding='newer', pid=os.getpid())
        assert attached['mode'] == 'cold' and attached['phase'] == 'reconciling'
        token = {key: attached[key] for key in ('epoch', 'binding', 'fence')}
        assert 'reconciling' in host.error('controller_check', control_owner=token)
        host.call('workflow', action='run', fields={'state': 'paused'}, control_owner=token)
        host.call('controller_ready', control_owner=token)
        assert host.call('controller_check', control_owner=token)['phase'] == 'ready'
        assert host.call('status')['workflow']['run']['state'] == 'paused'
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_uncertain_launch_requires_human_exit_check_before_new_launch(tmp_path):
    host, store, node_id = project(tmp_path)
    task = store.workflow('opencode', 'ses_main', 'task', {'node_id': node_id}, 'task')
    launch = store.node_launch_claim('opencode', 'ses_main', task['task_id'], 1, 'first-claim')
    store.node_launch_phase('opencode', 'ses_main', launch['intent_id'], 'creating', 'creating')
    store.workflow('opencode', 'ses_main', 'task_state', {'task_id': task['task_id'], 'state': 'unverified'}, 'uncertain')
    with pytest.raises(ConflictError, match='verified failed'):
        store.recovery_retry_task('opencode', 'ses_main', task['task_id'], 'retry', 'premature')
    assert store.recovery_confirm_exit('opencode', 'ses_main', task['task_id'], 'inspected host', 'verified')['state'] == 'failed'
    assert store.recovery_retry_task('opencode', 'ses_main', task['task_id'], 'new attempt', 'retry')['state'] == 'queued'
    again = store.node_launch_claim('opencode', 'ses_main', task['task_id'], 1, 'next-claim')
    assert again['launch_no'] == 2
    assert again['message_id'] != launch['message_id']
    with store._read() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='runtime.task_exit_confirmed'").fetchone()[0] == 1


def test_distinct_terminal_events_for_same_execution_are_settled_once(tmp_path):
    host, _, _ = project(tmp_path)
    first = host.call('runtime_execution_end', turn='event-one', outcome='succeeded', execution_token='execution-1')
    duplicate = host.call('runtime_execution_end', turn='event-two', outcome='succeeded', execution_token='execution-1')
    assert first['claimed'] is True
    assert duplicate['claimed'] is False


def test_pause_drain_requires_the_exact_delivered_message_and_current_turn(tmp_path):
    host, store, node_id = project(tmp_path)
    task = store.workflow('opencode', 'ses_main', 'task', {'node_id': node_id}, 'task')
    store.workflow('opencode', 'ses_main', 'task_session', {'task_id': task['task_id'], 'session_id': 'ses_node'}, 'bind')
    host.call('open', session='ses_node', root=str(host.root), cwd=str(host.root), session_role='node_core', node_id=node_id)
    host.call('focus', session='ses_node', node_id=node_id, role='core', mode='auto')
    store.workflow('opencode', 'ses_main', 'task_state', {'task_id': task['task_id'], 'state': 'running'}, 'running')
    host.call('runtime_reserve', session='ses_node', message_id='msg_authorized')
    host.call('runtime_execution_start', session='ses_node', execution_token='turn-a', event_seq=2)
    assert host.call('runtime_execution_bind', session='ses_node', execution_token='turn-a', message_id='msg_other', event_seq=3)['matched'] is False
    host.call('workflow', action='run', fields={'state': 'paused'})
    assert '不能开始新工作' in host.error('runtime_drain_check', session='ses_node', execution_token='turn-a')
    assert host.call('runtime_execution_bind', session='ses_node', execution_token='turn-a', message_id='msg_authorized', event_seq=4)['matched'] is True
    assert host.call('runtime_drain_check', session='ses_node', execution_token='turn-a')['allowed'] is True
    assert host.call('runtime_execution_bind', session='ses_node', execution_token='turn-a', message_id='msg_new_user_input', event_seq=5)['matched'] is False
    assert '不能开始新工作' in host.error('runtime_drain_check', session='ses_node', execution_token='turn-a')
    host.call('runtime_execution_end', session='ses_node', turn='end-a', outcome='succeeded', execution_token='turn-a', event_seq=6)
    assert '不能开始新工作' in host.error('runtime_drain_check', session='ses_node', execution_token='turn-a')
    host.call('runtime_execution_start', session='ses_node', execution_token='turn-b', event_seq=7)
    assert host.call('runtime_execution_bind', session='ses_node', execution_token='turn-b', message_id='msg_authorized', event_seq=4)['matched'] is False
    assert '不能开始新工作' in host.error('runtime_drain_check', session='ses_node', execution_token='turn-b')
