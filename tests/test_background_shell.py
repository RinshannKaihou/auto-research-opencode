"""Background shell execution must be part of autonomous control."""
import sqlite3

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
    assert 'different terminal notice' in host.error('runtime_shell_notice', session=sid, shell_id='sh_outcome',
        message_id='msg_outcome', outcome='completed', exit_code=1)
