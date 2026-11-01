"""Control stops settle only definitely unstarted E3 work, with durable wake facts."""
import json
import sqlite3

import pytest

from auto_research.native_store import NativeStore
from test_e3_multiroute import decision, setup, work
from test_opencode_host import Host


def mixed_work(tmp_path):
    host = Host(tmp_path)
    scope, routes = setup(host)
    host.call('runtime_budget_set', limits={'shared_work_budget':10, 'max_total_nodes':3,
        'max_wall_seconds':60, 'resource_capacities':{'gpu:fixture':2}}, reason='Bound control recovery fixture')
    choice = decision(host, scope, routes, work_batch=[
        work([routes[0]], key, resources={'gpu:fixture':1})
        for key in ('starting', 'unknown', 'queued', 'pending', 'waiting')])
    intents = choice['intent_ids']
    tasks = [host.call('exploration_materialize', intent_id=ident) for ident in intents[:3]]
    for task in tasks[:2]:
        assert host.call('node_launch_claim', task_id=task['task_id'], concurrency=3)['claimed']
    host.call('workflow', action='task_state', fields={'task_id':tasks[1]['task_id'], 'state':'unverified'})
    assert host.call('exploration_materialize', intent_id=intents[4])['state'] == 'waiting_budget'
    return host, intents, tasks


def counts(host):
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        return tuple(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                     for table in ('exploration_results','workflow_notifications','exploration_allocations'))


@pytest.mark.parametrize('stop_kind', ['user', 'deadline'])
def test_control_stop_settles_pending_and_queued_once_without_refunding_unknown(tmp_path, stop_kind):
    host, intents, tasks = mixed_work(tmp_path)
    if stop_kind == 'deadline':
        with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
            db.execute("UPDATE runtime_budgets SET deadline_at='2000-01-01T00:00:00+00:00'")
        assert host.call('runtime_budget_stop')['stopped']
    else:
        host.call('workflow', action='run', fields={'state':'stopping'})
    store = NativeStore(host.root)
    with store._read() as db:
        intent_states = {row['intent_id']:row['state'] for row in db.execute(
            "SELECT intent_id,state FROM workflow_intents WHERE kind='exploration_work'")}
        assert [intent_states[ident] for ident in intents] == ['materialized','materialized','cancelled','cancelled','cancelled']
        task_states = {row['task_id']:row['state'] for row in db.execute('SELECT task_id,state FROM exploration_tasks')}
        assert [task_states[task['task_id']] for task in tasks] == ['unverified','unverified','cancelled']
        cancelled = list(db.execute("SELECT * FROM exploration_results WHERE kind='cancelled'"))
        assert len(cancelled) == 3
        assert {json.loads(row['data'])['intent_id'] for row in cancelled} == set(intents[2:])
        notices = list(db.execute("SELECT * FROM workflow_notifications WHERE kind='exploration_result'"))
        assert len(notices) == 3
        assert sum(row['task_id'] is None for row in notices) == 2
        assert {row['reference'] for row in notices} == {row['result_id'] for row in cancelled}
        # Queued work has the intent settlement fact, rather than two cancellation facts.
        assert not db.execute('SELECT 1 FROM exploration_results WHERE source_key=?',
            (f"task:{tasks[2]['task_id']}:cancelled",)).fetchone()
    budget = store.runtime_budget_status('opencode','ses_main')['exploration']
    assert budget['allocated'] == budget['committed'] == 2
    assert budget['released'] == 3
    assert budget['reserved'] == 0
    assert budget['resources_held'] == {'gpu:fixture':2}
    assert budget['actual_usage'] is None and budget['actual_usage_status'] == 'unknown'
    before = counts(host)
    if stop_kind == 'deadline':
        assert not host.call('runtime_budget_stop')['stopped']
    else:
        host.call('workflow', action='run', fields={'state':'stopping'})
    assert counts(host) == before


def test_soft_pause_keeps_pending_queued_and_uncertain_allocations(tmp_path):
    host, intents, tasks = mixed_work(tmp_path)
    before = counts(host)
    host.call('workflow', action='run', fields={'state':'paused'})
    assert counts(host) == before
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert [db.execute('SELECT state FROM workflow_intents WHERE intent_id=?', (ident,)).fetchone()[0]
                for ident in intents] == ['materialized','materialized','materialized','pending','waiting_budget']
        assert db.execute('SELECT state FROM exploration_tasks WHERE task_id=?',
                          (tasks[2]['task_id'],)).fetchone()[0] == 'queued'
    budget = NativeStore(host.root).runtime_budget_status('opencode','ses_main')['exploration']
    assert budget['allocated'] == 5 and budget['reserved'] == 2
    assert budget['released'] == 0 and budget['resources_held'] == {'gpu:fixture':2}
