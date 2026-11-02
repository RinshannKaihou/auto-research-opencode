"""Background shell execution must be part of autonomous control."""
import sqlite3
import json
import pytest

from test_opencode_host import Host
from test_p0_contracts import node


def setup_node(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('focus', node_id=None, role='planner', mode='auto')
    host.call('workflow', action='run', fields={'state': 'running'})
    _, sid, task = node(host)
    host.call('workflow', action='task_state', fields={'task_id': task, 'state': 'running'})
    return host, sid, task


def progress(host, sid):
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        return db.execute('SELECT idle_turns,last_finished_turn FROM runtime_progress WHERE session_id=?', (sid,)).fetchone()


def test_background_shell_freezes_idle_until_its_own_terminal_notice(tmp_path):
    host, sid, _ = setup_node(tmp_path)
    host.call('runtime_shell_begin', session=sid, call_id='call_a')
    host.call('runtime_shell_result', session=sid, call_id='call_a', state='running', shell_id='sh_a')
    for n in range(5):
        host.call('runtime_reserve', session=sid)
        result = host.call('runtime_turn_finished', session=sid, turn=f'turn-{n}')
        assert result['waiting_external'] and result['idle_turns'] == 0
    first = host.call('runtime_shell_notice', session=sid, shell_id='sh_a', message_id='msg_done',
                      outcome='completed', exit_code=0, operation_id='notice-a')
    assert first['matched'] and first['first']
    assert host.call('runtime_shell_notice', session=sid, shell_id='sh_a', message_id='msg_done',
                     outcome='completed', exit_code=0, operation_id='notice-a') == first
    duplicate = host.call('runtime_shell_notice', session=sid, shell_id='sh_a', message_id='msg_done',
                          outcome='completed', exit_code=0)
    assert duplicate['matched'] and not duplicate['first']
    host.call('runtime_reserve', session=sid)
    result = host.call('runtime_turn_finished', session=sid, turn='after-completion')
    assert not result['waiting_external'] and result['idle_turns'] == 1


def test_notice_cannot_settle_another_sessions_background_job(tmp_path):
    host, first, _ = setup_node(tmp_path)
    _, second, _ = node(host, 2)
    host.call('runtime_shell_begin', session=first, call_id='call_a')
    host.call('runtime_shell_result', session=first, call_id='call_a', state='running', shell_id='sh_a')
    result = host.call('runtime_shell_notice', session=second, shell_id='sh_a', message_id='msg_wrong',
                       outcome='completed', exit_code=0)
    assert not result['matched']
    assert host.call('runtime_shell_status', session=first)['running'] == 1


def test_resume_atomically_clears_persistent_idle_and_preserves_turn_deduplication(tmp_path):
    host, sid, _ = setup_node(tmp_path)
    for n in range(3):
        host.call('runtime_reserve', session=sid)
        host.call('runtime_turn_finished', session=sid, turn=f'turn-{n}')
    host.call('workflow', session=sid, action='session', fields={'pause_reason': 'no_progress'})
    revision = host.call('status')['workflow']['run']['control_revision']
    result = host.call('runtime_resume_session', session='ses_main', target_session=sid,
                       expected_revision=revision, operation_id='resume-one')
    assert result['resumed'] and progress(host, sid) == (0, 'turn-2')
    assert host.call('runtime_resume_session', session='ses_main', target_session=sid,
                     expected_revision=revision, operation_id='resume-one') == result
    duplicate = host.call('runtime_turn_finished', session=sid, turn='turn-2')
    assert duplicate['duplicate'] and progress(host, sid) == (0, 'turn-2')
    host.call('runtime_reserve', session=sid)
    assert host.call('runtime_turn_finished', session=sid, turn='new')['idle_turns'] == 1


def test_resume_rejects_stale_revision_and_model_calls(tmp_path):
    host, sid, _ = setup_node(tmp_path)
    host.call('workflow', session=sid, action='session', fields={'pause_reason': 'no_progress'})
    revision = host.call('status')['workflow']['run']['control_revision']
    assert 'revision' in host.error('runtime_resume_session', target_session=sid, expected_revision=revision-1).lower()
    assert host.error('runtime_resume_session', target_session=sid, expected_revision=revision, model_call=True)
    assert host.call('status', session=sid)['workflow']['session']['pause_reason'] == 'no_progress'


def test_a_node_cannot_finish_while_a_background_shell_is_running(tmp_path):
    host, sid, _ = setup_node(tmp_path)
    host.call('runtime_shell_begin', session=sid, call_id='call_active')
    host.call('runtime_shell_result', session=sid, call_id='call_active', state='running', shell_id='sh_active')
    assert 'still running or unverified' in host.error('node_finish_request', session=sid, state='finished',
                                                       summary='not yet', turn_token='active-turn')


def test_unverified_shell_keeps_task_blocked_until_operator_verifies_exit(tmp_path):
    host, sid, task = setup_node(tmp_path)
    host.call('runtime_shell_begin', session=sid, call_id='call_unknown')
    host.call('runtime_shell_result', session=sid, call_id='call_unknown', state='running', shell_id='sh_unknown')
    assert host.call('runtime_shell_unverified', session=sid, call_id='call_unknown')['state'] == 'unverified'
    assert host.call('runtime_shell_status', session=sid)['unverified'] == 1
    host.call('workflow', session=sid, action='session', fields={'pause_reason': 'fault'})
    revision = host.call('status')['workflow']['run']['control_revision']
    result = host.call('runtime_resume_session', target_session=sid, expected_revision=revision)
    assert not result['resumed'] and result['reason'] == 'execution_unverified'
    assert host.call('recovery_confirm_exit', task_id=task, reason='operator checked process exit')['state'] == 'failed'
    assert host.call('runtime_shell_status', session=sid)['unverified'] == 0


def test_terminal_notice_must_match_the_original_outcome(tmp_path):
    host, sid, _ = setup_node(tmp_path)
    host.call('runtime_shell_begin', session=sid, call_id='call_outcome')
    host.call('runtime_shell_result', session=sid, call_id='call_outcome', state='running', shell_id='sh_outcome')
    host.call('runtime_shell_notice', session=sid, shell_id='sh_outcome', message_id='msg_outcome',
              outcome='completed', exit_code=0)
    assert host.call('runtime_shell_notice', session=sid, shell_id='sh_outcome',
        message_id='msg_outcome', outcome='completed', exit_code=1)['ignored']


def test_resume_repairs_orphan_starting_task_and_exposes_recovery(tmp_path):
    host, sid, task = setup_node(tmp_path)
    host.call('runtime_shell_begin', session=sid, call_id='orphan')
    host.call('workflow', session=sid, action='session', fields={'pause_reason':'native_stop'})
    revision = host.call('status')['workflow']['run']['control_revision']
    result = host.call('runtime_resume_session', target_session=sid, expected_revision=revision)
    assert result['reason'] == 'execution_unverified' and result['task_id'] == task
    assert host.call('recovery_confirm_exit', task_id=task, reason='checked no process')['exit_verified_by'] == 'user'
    assert host.call('runtime_shell_status', session=sid)['jobs'] == []


def test_unverified_result_marks_task_recoverable_in_same_transaction(tmp_path):
    host, sid, task = setup_node(tmp_path)
    host.call('runtime_shell_begin', session=sid, call_id='lost')
    host.call('runtime_shell_result', session=sid, call_id='lost', state='unverified', shell_id=None)
    assert host.call('recovery_confirm_exit', task_id=task, reason='checked process exit')['state'] == 'failed'


def test_late_notice_after_operator_recovery_is_ignored(tmp_path):
    host, sid, task = setup_node(tmp_path)
    host.call('runtime_shell_begin', session=sid, call_id='late')
    host.call('runtime_shell_result', session=sid, call_id='late', state='running', shell_id='sh_late')
    host.call('runtime_shell_unverified', session=sid, call_id='late')
    host.call('recovery_confirm_exit', task_id=task, reason='checked exit')
    assert host.call('runtime_shell_notice', session=sid, shell_id='sh_late', message_id='msg_late',
                     outcome='completed', exit_code=0)['ignored']


def test_project_resume_clears_idle_without_a_session_pause(tmp_path):
    host, sid, _ = setup_node(tmp_path)
    host.call('runtime_reserve', session=sid)
    host.call('runtime_turn_finished', session=sid, turn='idle')
    host.call('workflow', action='run', fields={'state':'paused'})
    host.call('workflow', action='run', fields={'state':'running'})
    revision = host.call('status')['workflow']['run']['control_revision']
    assert host.call('runtime_resume_session', target_session=sid, expected_revision=revision)['resumed']
    assert progress(host, sid) == (0, 'idle')


def test_each_background_launch_cycle_counts_as_idle(tmp_path):
    host, sid, _ = setup_node(tmp_path)
    for n in range(3):
        host.call('runtime_reserve', session=sid)
        host.call('runtime_shell_begin', session=sid, call_id=f'cycle{n}')
        host.call('runtime_shell_result', session=sid, call_id=f'cycle{n}', state='running', shell_id=f'sh_cycle{n}')
        assert host.call('runtime_turn_finished', session=sid, turn=f'cycle{n}')['idle_turns'] == n+1
        host.call('runtime_shell_notice', session=sid, shell_id=f'sh_cycle{n}', message_id=f'msg_cycle{n}',outcome='completed',exit_code=0)


def test_foreground_results_do_not_accumulate_intents_and_pending_query_uses_index(tmp_path):
    host, sid, _ = setup_node(tmp_path)
    host.call('runtime_shell_begin', session=sid, call_id='foreground')
    assert host.call('runtime_shell_status', session=sid)['starting'] == 1
    assert host.call('runtime_shell_status', session=sid)['unverified'] == 0
    host.call('runtime_shell_result', session=sid, call_id='foreground',state='finished',shell_id=None)
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert db.execute("SELECT COUNT(*) FROM workflow_intents WHERE kind='native_shell'").fetchone()[0] == 0
        plan = db.execute("EXPLAIN QUERY PLAN SELECT * FROM workflow_intents WHERE kind='native_shell' AND session_id=? AND state IN ('starting','running','unverified') AND json_extract(details,'$.host')=? AND json_extract(details,'$.generation')=? ORDER BY rowid",(sid,'host',0)).fetchall()
        assert 'SEARCH workflow_intents USING INDEX native_shell_' in str(plan)


def test_shell_without_an_active_task_is_rejected(tmp_path):
    host, sid, task = setup_node(tmp_path)
    host.call('workflow', action='task_state', fields={'task_id':task,'state':'failed'})
    assert 'active node task' in host.error('runtime_shell_begin', session=sid, call_id='no_task')


def test_unknown_host_outcome_is_failed_with_raw_state_retained(tmp_path):
    host, sid, _ = setup_node(tmp_path)
    host.call('runtime_shell_begin', session=sid, call_id='unknown')
    host.call('runtime_shell_result', session=sid, call_id='unknown',state='running',shell_id='sh_unknown')
    assert host.call('runtime_shell_notice', session=sid, shell_id='sh_unknown',message_id='msg_unknown',
                     outcome='new_host_state',exit_code=None)['state'] == 'failed'
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert db.execute("SELECT json_extract(details,'$.raw_outcome') FROM workflow_intents WHERE kind='native_shell'").fetchone()[0] == 'new_host_state'


def test_resume_pause_sets_stay_in_sync():
    import re
    from pathlib import Path
    from auto_research.runtime_budget import RESUMABLE_SESSION_PAUSES
    source=(Path(__file__).parents[1]/'plugin/src/engine.ts').read_text()
    ts=re.search(r'const RESUMABLE = new Set\(\[(.*?)\]\)',source).group(1)
    assert set(re.findall(r'[\"\']([a-z_]+)[\"\']',ts)) == RESUMABLE_SESSION_PAUSES


def test_a_terminal_shell_cannot_be_changed_to_unverified_by_a_late_scan(tmp_path):
    host, sid, task = setup_node(tmp_path)
    host.call('runtime_shell_begin', session=sid, call_id='scan')
    host.call('runtime_shell_result', session=sid, call_id='scan', state='running', shell_id='sh_scan')
    host.call('runtime_shell_notice', session=sid, shell_id='sh_scan', message_id='msg_scan',outcome='completed',exit_code=0)
    assert host.call('runtime_shell_unverified', session=sid,call_id='scan') == {'state':'completed','changed':False}
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert db.execute('SELECT state FROM exploration_tasks WHERE task_id=?',(task,)).fetchone()[0] == 'running'


def test_resume_refuses_an_unbound_live_start_without_declaring_its_exit_unknown(tmp_path):
    host, sid, task = setup_node(tmp_path)
    host.call('runtime_execution_start', session=sid, execution_token='exec_unbound',ambiguous=True)
    host.call('runtime_shell_begin', session=sid,call_id='unbound',execution_token='exec_unbound')
    revision=host.call('status')['workflow']['run']['control_revision']
    result=host.call('runtime_resume_session',target_session=sid,expected_revision=revision)
    assert result['reason']=='execution_binding_pending'
    assert host.call('runtime_shell_status',session=sid)['starting']==1
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert db.execute('SELECT state FROM exploration_tasks WHERE task_id=?',(task,)).fetchone()[0]=='running'


def transcript_wake(tmp_path, *, reserve=True, sequence=20, ambiguous=False):
    host, sid, task=setup_node(tmp_path)
    host.call('runtime_execution_start',session=sid,execution_token='previous',event_seq=1)
    host.call('runtime_shell_begin',session=sid,call_id='transcript',execution_token='previous')
    host.call('runtime_shell_result',session=sid,call_id='transcript',state='running',shell_id='sh_transcript')
    host.call('runtime_execution_end',session=sid,turn='previous',outcome='succeeded',execution_token='previous',event_seq=10)
    host.call('runtime_shell_notice',session=sid,shell_id='sh_transcript',message_id='msg_transcript',outcome='completed',exit_code=0)
    if reserve:host.call('runtime_reserve',session=sid,message_id='msg_transcript')
    host.call('runtime_execution_start',session=sid,execution_token='wake',event_seq=sequence,ambiguous=ambiguous)
    return host,sid,task


def test_transcript_binding_requires_explicit_witness_and_preserves_native_start_sequence(tmp_path):
    host,sid,_=transcript_wake(tmp_path)
    assert not host.call('runtime_execution_bind',session=sid,execution_token='wake',message_id='msg_transcript')['matched']
    result=host.call('runtime_execution_bind',session=sid,execution_token='wake',message_id='msg_transcript',transcript_witness=True,operation_id='transcript_bind')
    assert result['matched']
    assert host.call('runtime_execution_bind',session=sid,execution_token='wake',message_id='msg_transcript',transcript_witness=True,operation_id='transcript_bind')==result
    view=host.call('runtime_execution_view',session=sid,execution_token='wake')
    assert view['binding_source']=='transcript' and view['event_seq']==20
    assert view['message_id']=='msg_transcript' and view['reservation_id']
    assert host.call('runtime_execution_bind',session=sid,execution_token='wake',message_id='msg_transcript',event_seq=21)['matched']


@pytest.mark.parametrize('case',['no_start_sequence','stale_start','ambiguous','no_authorization','wrong_message','paused_project','paused_session','task_unverified','ended_turn'])
def test_transcript_binding_rejects_incomplete_or_stale_authorization(tmp_path,case):
    host,sid,task=transcript_wake(tmp_path,reserve=case!='no_authorization',
        sequence=None if case=='no_start_sequence' else 10 if case=='stale_start' else 20,ambiguous=case=='ambiguous')
    if case=='paused_project':host.call('workflow',action='run',fields={'state':'paused'})
    if case=='paused_session':host.call('workflow',session=sid,action='session',fields={'pause_reason':'fault'})
    if case=='task_unverified':host.call('workflow',action='task_state',fields={'task_id':task,'state':'unverified'})
    if case=='ended_turn':host.call('runtime_execution_end',session=sid,turn='wake',outcome='succeeded',execution_token='wake',event_seq=21)
    result=host.call('runtime_execution_bind',session=sid,execution_token='wake',message_id='msg_other' if case=='wrong_message' else 'msg_transcript',transcript_witness=True)
    assert not result['matched']
    view=host.call('runtime_execution_view',session=sid,execution_token='wake')
    assert view['state']==('ended' if case=='ended_turn' else 'open')
    assert not view.get('reservation_id') and not view.get('message_id')


def test_transcript_binding_cannot_fake_a_delivery_sequence_or_be_requested_by_a_model(tmp_path):
    host,sid,_=transcript_wake(tmp_path)
    assert 'delivery event sequence' in host.error('runtime_execution_bind',session=sid,execution_token='wake',message_id='msg_transcript',transcript_witness=True,event_seq=99)
    assert host.error('runtime_execution_bind',session=sid,execution_token='wake',message_id='msg_transcript',transcript_witness=True,model_call=True)


@pytest.mark.parametrize('orphan_first',[True,False])
def test_resume_binding_pending_is_side_effect_free_regardless_of_shell_order(tmp_path,orphan_first):
    host,sid,task=setup_node(tmp_path)
    host.call('runtime_execution_start',session=sid,execution_token='live_unbound',event_seq=1,ambiguous=True)
    for call in (['orphan','live'] if orphan_first else ['live','orphan']):
        host.call('runtime_shell_begin',session=sid,call_id=call,**({'execution_token':'live_unbound'} if call=='live' else {}))
    revision=host.call('status')['workflow']['run']['control_revision']
    result=host.call('runtime_resume_session',target_session=sid,expected_revision=revision)
    assert result['reason']=='execution_binding_pending'
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert db.execute('SELECT state FROM exploration_tasks WHERE task_id=?',(task,)).fetchone()[0]=='running'
        assert set(row[0] for row in db.execute("SELECT state FROM workflow_intents WHERE kind='native_shell'"))=={'starting'}
        assert db.execute("SELECT COUNT(*) FROM workflow_notifications WHERE kind='paused'").fetchone()[0]==0


def test_resume_reports_existing_uncertainty_before_an_unbound_live_call(tmp_path):
    host,sid,task=setup_node(tmp_path)
    host.call('runtime_execution_start',session=sid,execution_token='live_unbound',event_seq=1,ambiguous=True)
    host.call('runtime_shell_begin',session=sid,call_id='unknown')
    host.call('runtime_shell_begin',session=sid,call_id='live',execution_token='live_unbound')
    host.call('runtime_shell_result',session=sid,call_id='unknown',state='unverified',shell_id=None)
    revision=host.call('status')['workflow']['run']['control_revision']
    assert host.call('runtime_resume_session',target_session=sid,expected_revision=revision)=={'resumed':False,'reason':'execution_unverified','task_id':task}
