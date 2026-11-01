"""Unstarted exploration work must settle as a coordinator-visible result."""

import json
import sqlite3

from test_exploration_decisions import decided
from test_opencode_host import Host
from auto_research.service import NativeService


def start(host):
    host.open()
    host.call('workflow', action='run', fields={'state':'running'})
    return decided(host)


def notices(host):
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute('SELECT * FROM workflow_notifications ORDER BY created_at')]


def deliver_notice(host, notice):
    delivery = host.call('notification_begin', notification_id=notice['notification_id'], text='Exploration result')
    host.call('notification_confirm', intent_id=delivery['intent_id'], message_id=delivery['message_id'])
    return host.call('notification_wake_begin', notification_id=notice['notification_id'])


def test_stale_queued_work_notifies_and_wakes_waiting_main(tmp_path):
    host = Host(tmp_path)
    scope, _, _, decision = start(host)
    work = host.call('exploration_materialize', intent_id=decision['intent_id'])
    host.call('workflow', action='session', fields={'pause_reason':'wait','waiting':[work['task_id']]})
    host.call('exploration_route', model_call=True, fields={'scope_ref':scope['ref'], 'route_ref':decision['route_refs'][0],
        'reason':'Changed premise', 'changes':{'critical_assumption':'A different workload'}})
    claim = host.call('node_launch_claim', task_id=work['task_id'], concurrency=1)
    assert claim == {'claimed':False,'reason':'exploration_basis_changed','state':'cancelled'}
    results = host.call('query', collection='exploration_results')['items']
    assert len(results) == 1 and results[0]['kind'] == 'stale'
    assert host.call('node_launch_claim', task_id=work['task_id'], concurrency=1)['state'] == 'cancelled'
    assert len(notices(host)) == 1
    wake = deliver_notice(host, notices(host)[0])
    host.call('notification_wake_confirm', intent_id=wake['intent_id'], message_id=wake['message_id'])
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert db.execute("SELECT pause_reason FROM workflow_sessions WHERE session_id='ses_main'").fetchone()[0] is None
        assert db.execute('SELECT COUNT(*) FROM workflow_intents WHERE kind=\'node_launch\'').fetchone()[0] == 0


def test_stale_unmaterialized_intent_has_no_fake_task_and_can_wake(tmp_path):
    host = Host(tmp_path)
    scope, _, _, decision = start(host)
    host.call('exploration_route', model_call=True, fields={'scope_ref':scope['ref'],
        'changes':{'title':'Another idea','rationale':'Alternative','critical_assumption':'May work'}})
    host.call('workflow', action='session', fields={'pause_reason':'wait','waiting':['other-task']})
    outcome = host.call('exploration_materialize', intent_id=decision['intent_id'])
    assert outcome['state'] == 'stale' and outcome['task_id'] is None
    notice = notices(host)[0]
    assert notice['task_id'] is None and json.loads(notice['payload'])['result_id'] == outcome['result_id']
    delivery = host.call('notification_begin', notification_id=notice['notification_id'], text='Exploration result')
    host.call('notification_confirm', intent_id=delivery['intent_id'], message_id=delivery['message_id'])
    host.service = NativeService(tmp_path / 'registry.sqlite3')
    state = host.call('status')
    assert any(item['notification_id'] == notice['notification_id'] for item in state['workflow']['notifications'])
    wake = host.call('notification_wake_begin', notification_id=notice['notification_id'])
    host.call('notification_wake_confirm', intent_id=wake['intent_id'], message_id=wake['message_id'])


def test_off_cancels_queued_work_but_allows_closing_decision(tmp_path):
    host = Host(tmp_path)
    scope, _, _, decision = start(host)
    work = host.call('exploration_materialize', intent_id=decision['intent_id'])
    host.call('exploration_policy', mode='off')
    result = host.call('query', collection='exploration_results')['items'][0]
    assert result['kind'] == 'cancelled' and result['data']['task_id'] == work['task_id']
    view = host.call('query', collection='evidence', refs=[])
    closing = {'scope_ref':scope['ref'], 'snapshot_id':view['snapshot_id'], 'route_refs':decision['route_refs'],
               'result_ids':[result['result_id']], 'action':'propose_stop', 'reason':'Exploration was stopped'}
    assert host.call('exploration_decide', model_call=True, fields=closing)['intent_id'] is None
    view = host.call('query', collection='evidence', refs=[])
    assert 'only closing decisions' in host.error('exploration_decide', model_call=True,
        fields={**closing, 'snapshot_id':view['snapshot_id'], 'action':'probe', 'result_ids':[]})


def test_obsolete_notice_cannot_wake_after_result_was_considered(tmp_path):
    host = Host(tmp_path)
    scope, _, _, decision = start(host)
    host.call('workflow', action='session', fields={'pause_reason':'wait','waiting':['other-task']})
    host.call('exploration_policy', mode='off')
    notice = notices(host)[0]
    result = host.call('query', collection='exploration_results')['items'][0]
    delivery = host.call('notification_begin', notification_id=notice['notification_id'], text='Exploration result')
    host.call('notification_confirm', intent_id=delivery['intent_id'], message_id=delivery['message_id'])
    view = host.call('query', collection='evidence', refs=[])
    host.call('exploration_decide', model_call=True, fields={'scope_ref':scope['ref'], 'snapshot_id':view['snapshot_id'],
        'route_refs':decision['route_refs'],'result_ids':[result['result_id']],'action':'propose_stop','reason':'Considered'})
    assert 'not waiting' in host.error('notification_wake_begin', notification_id=notice['notification_id'])


def test_invalid_new_and_existing_intents_do_not_block_other_work(tmp_path):
    host = Host(tmp_path)
    _, _, fields, decision = start(host)
    current = host.call('query', collection='evidence', refs=[])
    invalid = {**fields, 'snapshot_id':current['snapshot_id'], 'route_refs':decision['route_refs'],
               'next_work':{**fields['next_work'], 'route_refs':decision['route_refs'], 'strategy':'invalid'}}
    assert 'strategy must' in host.error('exploration_decide', model_call=True, fields=invalid)
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        raw = db.execute('SELECT details FROM workflow_intents WHERE intent_id=?', (decision['intent_id'],)).fetchone()[0]
        details = json.loads(raw)
        details['next_work']['strategy'] = 'invalid'
        db.execute('UPDATE workflow_intents SET details=? WHERE intent_id=?', (json.dumps(details), decision['intent_id']))
    failed = host.call('exploration_materialize', intent_id=decision['intent_id'])
    assert failed['state'] == 'failed' and 'strategy must' in failed['reason']
    assert host.call('exploration_pending') == []
    ordinary = host.call('propose', model_call=True, question='Ordinary work', why_now='now', plan='measure', root_reason='root')
    task = host.call('workflow', action='task', fields={'node_id':ordinary['node_id']})
    assert host.call('node_launch_claim', task_id=task['task_id'], concurrency=1)['claimed']
