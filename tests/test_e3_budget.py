"""Shared exploration allocations and declared resource exclusion."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from auto_research import native_store, schema16
from auto_research.errors import BudgetError, ConflictError, ValidationError
from auto_research.controller import CURRENT_MODEL_CALL
from auto_research.native_store import NativeStore
from auto_research.runtime_budget import (materialize_exploration_work,
    release_exploration_work, reserve_exploration_work)
from test_opencode_host import Host


def project(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('focus', node_id=None, role='planner', mode='auto')
    host.call('workflow', action='run', fields={'state': 'running'})
    return host, NativeStore(host.root)


def reserve(store, intent, scope='ES-001', amount=1, resources=None, task_id=None):
    details = {'scope_id': scope, 'generation': 0,
               'next_work': {'budget_amount': amount, 'resources': resources or {}},
               'route_refs': ['route-a', 'route-b']}

    def work(db):
        result = reserve_exploration_work(store, db, intent, details)
        if task_id:
            materialize_exploration_work(store, db, intent, task_id)
        return result
    return store._mutate('test.allocate', details, 'allocate:' + intent, work)


def task(host, store, key):
    node = host.call('propose', question=key, why_now='compare', plan='measure', root_reason='independent probe')
    return store.workflow('opencode', 'ses_main', 'task', {'node_id': node['node_id']}, 'task:' + key)


def finish_claimed(host, store, task, launch):
    child = launch['session_id']
    host.call('open', session=child, root=str(host.root), cwd=str(host.root),
              session_role='node_core', node_id=task['node_id'])
    host.call('focus', session=child, node_id=task['node_id'], role='core', mode='auto')
    finish = store.node_finish_request('opencode', child, 'finished', 'Measured synthetic result', 'turn', 'finish')
    store.runtime_execution_end('opencode', child, 'end', 'succeeded', f'end:{child}:end', 'turn')
    return store.node_finish_commit('opencode', child, finish['intent_id'], 'turn', 'end', 'commit')


def test_normal_finish_resource_wait_is_durable_deduplicated_and_recoverable(tmp_path):
    host, store = project(tmp_path)
    store.runtime_budget_set('opencode', 'ses_main', {'resource_capacities': {'gpu:0': 1}}, 'one GPU', 'limit')
    first, second = task(host, store, 'first'), task(host, store, 'second')
    reserve(store, 'first', resources={'gpu:0': 1}, task_id=first['task_id'])
    reserve(store, 'second', resources={'gpu:0': 1}, task_id=second['task_id'])
    launch = store.node_launch_claim('opencode', 'ses_main', first['task_id'], 2, 'launch')
    finish_claimed(host, store, first, launch)
    assert store.runtime_budget_status('opencode', 'ses_main')['exploration']['resource_leases'][0]['resource_state'] == 'uncertain'
    blocked = store.node_launch_claim('opencode', 'ses_main', second['task_id'], 2, 'blocked')
    notice_id = blocked['notification_id']
    assert blocked['blockers'][0]['task_id'] == first['task_id']
    # A cold backend retries the same wait and reserves no additional notice.
    store = NativeStore(host.root)
    again = store.node_launch_claim('opencode', 'ses_main', second['task_id'], 2, 'blocked-again')
    assert again['notification_id'] == notice_id
    with store._read() as db:
        notices = list(db.execute("SELECT * FROM workflow_notifications WHERE kind='resource_wait'"))
        assert len(notices) == 1
        import json
        body = json.loads(notices[0]['payload'])
        assert body['recovery_commands'][0].startswith('/research recover resource ' + first['task_id'])
    delivery = store.notification_begin('opencode', 'ses_main', notice_id, 'notice', 'User must verify external exit')
    assert delivery['send']
    store.notification_confirm('opencode', 'ses_main', delivery['intent_id'], delivery['message_id'], 'delivered')
    assert not store.notification_begin('opencode', 'ses_main', notice_id, 'repeat', 'User must verify external exit')['send']
    store.runtime_resources_release('opencode', 'ses_main', first['task_id'], 'Checked GPU process exit', 'release')
    assert store.node_launch_claim('opencode', 'ses_main', second['task_id'], 2, 'next')['claimed']
    assert store.runtime_budget_status('opencode', 'ses_main')['exploration']['allocated'] == 2


def test_finished_lease_can_be_released_after_main_takeover(tmp_path):
    host, store = project(tmp_path)
    store.runtime_budget_set('opencode', 'ses_main', {'resource_capacities': {'gpu:0': 1}}, 'one GPU', 'limit')
    first = task(host, store, 'before-takeover')
    reserve(store, 'old-main-work', resources={'gpu:0': 1}, task_id=first['task_id'])
    launch = store.node_launch_claim('opencode', 'ses_main', first['task_id'], 2, 'launch')
    finish_claimed(host, store, first, launch)
    host.call('workflow', action='run', fields={'state': 'stopped'})
    host.call('takeover_main', session='ses_successor', root=str(host.root), cwd=str(host.root))
    with pytest.raises(ConflictError, match='current main'):
        store.runtime_resources_release('opencode', 'ses_main', first['task_id'], 'Detached caller', 'denied-old')
    result = store.runtime_resources_release('opencode', 'ses_successor', first['task_id'], 'Checked GPU process exit', 'release-new')
    assert result['resources_released'] and not result['work_budget_released']
    host.call('focus', session='ses_successor', node_id=None, role='planner', mode='auto')
    host.call('workflow', session='ses_successor', action='run', fields={'state': 'running'})
    node = host.call('propose', session='ses_successor', question='After takeover', why_now='Resume', plan='Measure', root_reason='New test')
    second = store.workflow('opencode', 'ses_successor', 'task', {'node_id': node['node_id']}, 'next-task')
    reserve(store, 'new-main-work', resources={'gpu:0': 1}, task_id=second['task_id'])
    assert store.node_launch_claim('opencode', 'ses_successor', second['task_id'], 2, 'next-claim')['claimed']


def test_empty_resource_requests_never_hold_a_lease_including_old_rows(tmp_path):
    host, store = project(tmp_path)
    queued = task(host, store, 'no-resources')
    reserve(store, 'empty', task_id=queued['task_id'])
    launch = store.node_launch_claim('opencode', 'ses_main', queued['task_id'], 2, 'launch')
    assert store.runtime_budget_status('opencode', 'ses_main')['exploration']['resource_leases'] == []
    # Repair the old-build form as part of normal completion, and hide it in
    # read projections even before the task finishes.
    with store._connection() as db:
        db.execute("UPDATE exploration_allocations SET resource_state='held'")
    assert store.runtime_budget_status('opencode', 'ses_main')['exploration']['resource_leases'] == []
    finish_claimed(host, store, queued, launch)
    with store._read() as db:
        assert db.execute('SELECT resource_state FROM exploration_allocations').fetchone()[0] == 'released'


def test_earlier_generation_task_is_rejected_before_resource_wait(tmp_path):
    host, store = project(tmp_path)
    store.runtime_budget_set('opencode', 'ses_main', {'resource_capacities': {'gpu:0': 1}}, 'one GPU', 'limit')
    first, old = task(host, store, 'first'), task(host, store, 'old')
    reserve(store, 'first', resources={'gpu:0': 1}, task_id=first['task_id'])
    reserve(store, 'old', resources={'gpu:0': 1}, task_id=old['task_id'])
    assert store.node_launch_claim('opencode', 'ses_main', first['task_id'], 2, 'first-claim')['claimed']
    with store._connection() as db:
        db.execute('UPDATE exploration_tasks SET generation=-1 WHERE task_id=?', (old['task_id'],))
    with pytest.raises(ConflictError, match='earlier research generation'):
        store.node_launch_claim('opencode', 'ses_main', old['task_id'], 2, 'old-claim')
    with store._read() as db:
        assert db.execute("SELECT COUNT(*) FROM workflow_notifications WHERE kind='resource_wait'").fetchone()[0] == 0


def test_concurrent_scopes_share_one_atomic_budget_and_unknown_usage(tmp_path):
    _, store = project(tmp_path)
    store.runtime_budget_set('opencode', 'ses_main', {'shared_work_budget': 3}, 'bounded pilot', 'limit')

    def allocate(index):
        try:
            reserve(store, f'work-{index}', f'ES-00{index}', amount=2)
            return 'allocated'
        except BudgetError:
            return 'budget'

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(allocate, [1, 2])) == ['allocated', 'budget']
    status = store.runtime_budget_status('opencode', 'ses_main')['exploration']
    assert status['allocated'] == status['reserved'] == 2
    assert len(status['allocations']) == 1
    assert status['actual_usage'] is None and status['actual_usage_status'] == 'unknown'
    assert status['allocations'][0]['actual_usage'] is None


def test_shared_probe_replay_reserves_once_and_releases_only_unstarted(tmp_path):
    host, store = project(tmp_path)
    store.runtime_budget_set('opencode', 'ses_main', {'shared_work_budget': 1}, 'single shared probe', 'limit')
    first = reserve(store, 'shared')
    assert reserve(store, 'shared') == first
    summary = store.runtime_budget_status('opencode', 'ses_main')['exploration']
    assert summary['allocated'] == 1 and len(summary['allocations']) == 1
    with pytest.raises(BudgetError):
        reserve(store, 'duplicate-for-other-route')
    released = store._mutate('test.release', {}, 'release',
                            lambda db: release_exploration_work(store, db, 'shared'))
    assert released['released']
    queued = task(host, store, 'after-release')
    reserve(store, 'new', task_id=queued['task_id'])
    launch = store.node_launch_claim('opencode', 'ses_main', queued['task_id'], 3, 'launch')
    assert launch['claimed']
    cannot_release = store._mutate('test.release', {}, 'uncertain-release',
                                  lambda db: release_exploration_work(store, db, 'new'))
    assert cannot_release == {'released': False, 'reason': 'work_may_have_started'}
    summary = store.runtime_budget_status('opencode', 'ses_main')['exploration']
    assert summary['committed'] == 1 and summary['actual_usage'] is None


def test_resource_lease_survives_unknown_exit_and_is_shared_between_scopes(tmp_path):
    host, store = project(tmp_path)
    store.runtime_budget_set('opencode', 'ses_main',
        {'shared_work_budget': 5, 'resource_capacities': {'gpu:0': 1}}, 'serialize measurement', 'limit')
    first, second = task(host, store, 'fast'), task(host, store, 'slow')
    reserve(store, 'fast', resources={'gpu:0': 1}, task_id=first['task_id'])
    reserve(store, 'slow', 'ES-002', resources={'gpu:0': 1}, task_id=second['task_id'])
    assert store.node_launch_claim('opencode', 'ses_main', first['task_id'], 4, 'first')['claimed']
    blocked = store.node_launch_claim('opencode', 'ses_main', second['task_id'], 4, 'blocked')
    assert not blocked['claimed'] and blocked['reason'] == 'waiting_resource'
    host.call('workflow', action='task_state', fields={'task_id': first['task_id'], 'state': 'unverified'})
    assert not store.node_launch_claim('opencode', 'ses_main', second['task_id'], 4, 'unknown-blocked')['claimed']
    with pytest.raises(ConflictError, match='reconcile unverified'):
        store.runtime_resources_release('opencode', 'ses_main', first['task_id'], 'not yet checked', 'bad-release')
    store.recovery_confirm_exit('opencode', 'ses_main', first['task_id'], 'checked host and GPU processes', 'exit')
    assert store.node_launch_claim('opencode', 'ses_main', second['task_id'], 4, 'second')['claimed']
    status = store.runtime_budget_status('opencode', 'ses_main')['exploration']
    assert status['resources_held'] == {'gpu:0': 1}
    # Exit verification releases the lease, not unobserved expenditure.
    assert status['allocated'] == status['committed'] == 2
    assert status['actual_usage'] is None


def test_definite_not_sent_releases_resource_but_retains_work_for_retry(tmp_path):
    host, store = project(tmp_path)
    store.runtime_budget_set('opencode', 'ses_main', {'resource_capacities': {'cpu:measurement': 1}}, 'isolate CPU', 'limit')
    first, second = task(host, store, 'a'), task(host, store, 'b')
    reserve(store, 'a', resources={'cpu:measurement': 1}, task_id=first['task_id'])
    reserve(store, 'b', resources={'cpu:measurement': 1}, task_id=second['task_id'])
    launch = store.node_launch_claim('opencode', 'ses_main', first['task_id'], 4, 'first')
    store.runtime_outbound_not_sent('opencode', 'ses_main', launch['intent_id'], 'create', 'host API not called', 'unsent')
    assert store.node_launch_claim('opencode', 'ses_main', second['task_id'], 4, 'second')['claimed']
    status = store.runtime_budget_status('opencode', 'ses_main')['exploration']
    assert status['committed'] == 2 and status['resources_held'] == {'cpu:measurement': 1}


@pytest.mark.parametrize('limits', [
    {'shared_work_budget': 0}, {'shared_work_budget': float('nan')}, {'shared_work_budget': True},
    {'resource_capacities': {'gpu': 0}}, {'resource_capacities': {'gpu': True}},
    {'resource_capacities': []},
])
def test_invalid_budget_configuration_is_rejected(tmp_path, limits):
    _, store = project(tmp_path)
    with pytest.raises(ValidationError):
        store.runtime_budget_set('opencode', 'ses_main', limits, 'invalid', 'invalid')


def test_requested_resources_require_explicit_capacity(tmp_path):
    _, store = project(tmp_path)
    with pytest.raises(ValidationError, match='explicit sufficient capacity'):
        reserve(store, 'gpu', resources={'gpu:0': 1})
    with store._read() as db:
        assert db.execute("SELECT COUNT(*) FROM runtime_budget_reservations WHERE kind='exploration_work'").fetchone()[0] == 0


def test_zero_work_units_cannot_bypass_shared_budget(tmp_path):
    _, store = project(tmp_path)
    store.runtime_budget_set('opencode', 'ses_main', {'shared_work_budget': 1}, 'one work item', 'limit')
    with pytest.raises(ValidationError, match='finite positive'):
        reserve(store, 'free-probe', amount=0)
    with store._read() as db:
        assert db.execute('SELECT COUNT(*) FROM exploration_allocations').fetchone()[0] == 0


def test_model_cannot_attest_resource_exit(tmp_path):
    _, store = project(tmp_path)
    token = CURRENT_MODEL_CALL.set({'host': 'opencode', 'session': 'ses_main', 'execution_token': None})
    try:
        with pytest.raises(ValidationError, match='host/operator-only'):
            store.runtime_resources_release('opencode', 'ses_main', 'any', 'model says exited', 'denied')
    finally:
        CURRENT_MODEL_CALL.reset(token)


def test_schema16_readonly_migration_preservation_and_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(native_store, 'SCHEMA_VERSION', 15)
    host, store = project(tmp_path)
    assert reserve(store, 'old')['semantics'] == 'legacy'
    with sqlite3.connect(store.db_path) as db:
        rows = {table: db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall() for table in
                ['requests', 'events', 'runtime_budget_reservations', 'workflow_intents']}
    monkeypatch.setattr(native_store, 'SCHEMA_VERSION', 16)
    NativeStore(host.root, readonly=True)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 15
    ensure = schema16.ensure_schema16
    def interrupted(db):
        ensure(db)
        raise RuntimeError('migration interrupted')
    monkeypatch.setattr(schema16, 'ensure_schema16', interrupted)
    with pytest.raises(RuntimeError, match='migration interrupted'):
        NativeStore(host.root)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 15
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='exploration_allocations'").fetchone()
    monkeypatch.setattr(schema16, 'ensure_schema16', ensure)
    upgraded = NativeStore(host.root)
    with upgraded._read() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 16
        assert db.execute('SELECT COUNT(*) FROM exploration_allocations').fetchone()[0] == 0
        for table, values in rows.items():
            assert [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')] == values
    with sqlite3.connect(store.meta / 'schema-15-backup.sqlite3') as backup:
        assert backup.execute('PRAGMA user_version').fetchone()[0] == 15
        assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
