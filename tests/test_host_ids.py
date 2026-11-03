"""Host-neutral ID rules: each host accepts only its own identifier formats."""
from uuid import uuid4

import pytest

from auto_research.errors import ValidationError
from auto_research.host_ids import (CODEX, OPENCODE, new_message_id, valid_message_id,
                                    valid_session_id, valid_shell_id, valid_turn_id)
from auto_research.native_store import NativeStore
from test_opencode_host import Host

THREAD = '01a19f80-803f-7110-b12d-049e3173b880'
TURN = '01a19f80-80c3-7a91-bee3-fdd3c9425cbb'
CHILD = '11b2f901-91a4-4c22-d55f-26b05394ab92'


def test_each_host_accepts_its_own_ids_and_rejects_the_other_hosts_ids():
    assert valid_session_id(OPENCODE, 'ses_main') and not valid_session_id(OPENCODE, THREAD)
    assert valid_session_id(CODEX, THREAD) and not valid_session_id(CODEX, 'ses_main')
    assert valid_message_id(OPENCODE, 'msg_research_0123abcd') and not valid_message_id(OPENCODE, 'ari-cm-0123abcd')
    assert valid_message_id(CODEX, 'ari-cm-0123abcd') and not valid_message_id(CODEX, 'msg_research_0123abcd')
    assert valid_shell_id(OPENCODE, 'sh_1') and not valid_shell_id(CODEX, 'sh_1')
    assert valid_turn_id(OPENCODE, 'turn-1') and valid_turn_id(CODEX, TURN)
    assert not valid_turn_id(CODEX, 'turn-1')


@pytest.mark.parametrize('value', ['', None, 17, 'SES_MAIN', 'MSG_X', 'SH_X',
                                   '01A19F80-803F-7110-B12D-049E3173B880',
                                   '{01a19f80-803f-7110-b12d-049e3173b880}',
                                   '01a19f80803f7110b12d049e3173b880',
                                   'ari-cm-', 'x' * 300])
def test_malformed_values_are_rejected(value):
    assert not valid_session_id(CODEX, value)
    assert not valid_message_id(CODEX, value)
    assert not valid_message_id(OPENCODE, value)
    assert not valid_shell_id(OPENCODE, value)
    assert not valid_turn_id(CODEX, value)


def test_overlong_prefixed_ids_are_rejected():
    assert not valid_session_id(OPENCODE, 'ses_' + 'x' * 253)
    assert not valid_message_id(OPENCODE, 'msg_' + 'x' * 253)
    assert not valid_shell_id(OPENCODE, 'sh_' + 'x' * 254)
    assert not valid_message_id(CODEX, 'ari-cm-' + 'f' * 300)
    assert not valid_turn_id(OPENCODE, 'x' * 300)


def test_new_message_id_is_valid_only_for_its_host():
    codex = new_message_id(CODEX, uuid4().hex)
    opencode = new_message_id(OPENCODE, uuid4().hex)
    assert valid_message_id(CODEX, codex) and not valid_message_id(OPENCODE, codex)
    assert valid_message_id(OPENCODE, opencode) and not valid_message_id(CODEX, opencode)
    assert new_message_id(CODEX, 'deadbeef') == 'ari-cm-deadbeef'
    assert new_message_id(OPENCODE, 'deadbeef') == 'msg_research_deadbeef'


def running(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('focus', node_id=None, role='planner', mode='auto')
    host.call('workflow', action='run', fields={'state': 'running'})
    return host, NativeStore(host.root)


def test_ledger_rejects_cross_host_message_ids(tmp_path):
    _, store = running(tmp_path)
    assert store.runtime_reserve('opencode', 'ses_main', 'op', message_id='msg_host_1')['allowed']
    with pytest.raises(ValidationError, match='valid host message ID'):
        store.runtime_reserve('codex', THREAD, 'op', message_id='msg_host_1')
    with pytest.raises(ValidationError, match='bounded host message ID'):
        store.runtime_turn_not_sent('codex', THREAD, 'msg_host_1', 'op')
    with pytest.raises(ValidationError, match='bounded host message ID'):
        store.runtime_turn_not_sent('opencode', 'ses_main', 'ari-cm-0123abcd', 'op')


def test_shell_ids_are_host_specific(tmp_path):
    _, store = running(tmp_path)
    with pytest.raises(ValidationError, match='not supported for this host'):
        store.runtime_shell_result('codex', THREAD, 'call', 'running', 'sh_1', 'op')
    with pytest.raises(ValidationError, match='not supported for this host'):
        store.runtime_shell_notice('codex', THREAD, 'sh_1', 'ari-cm-0123abcd', 'completed', 0, 'op')
    with pytest.raises(ValidationError, match='Invalid shell terminal notice'):
        store.runtime_shell_notice('opencode', 'ses_main', 'sh_1', 'ari-cm-0123abcd', 'completed', 0, 'op')


def test_service_boundary_enforces_session_shapes_per_host(tmp_path):
    host = Host(tmp_path, host_id='codex')
    host.open(session=THREAD)
    host.call('focus', session=THREAD, node_id=None, role='planner', mode='auto')
    host.call('workflow', session=THREAD, action='run', fields={'state': 'running'})
    assert host.call('runtime_reserve', session=THREAD, message_id='ari-cm-0123abcd')['allowed']
    assert 'not a valid codex session ID' in host.error('status', session='ses_main')
    assert 'valid host message ID' in host.error('runtime_reserve', session=THREAD, message_id='msg_host_1')


def test_specialist_launch_identity_is_host_specific(tmp_path):
    _, store = running(tmp_path)

    def create(launch_ids, request, host):
        return store.specialist_create(
            {'parent_session_id': 'ses_main', 'purpose': 'review', 'label': 'review',
             'prompt': 'check', 'inputs': [],
             'launch': {'tools': ['read'], 'deadline_at': 2000000000000, **launch_ids}},
            request, host=host)

    with pytest.raises(ValidationError, match='Invalid specialist launch identity or deadline'):
        create({'session_id': THREAD, 'message_id': 'ari-cm-0123abcd'}, 'codex-ids-under-opencode', 'opencode')
    with pytest.raises(ValidationError, match='Invalid specialist launch identity or deadline'):
        create({'session_id': 'ses_specialist', 'message_id': 'msg_specialist'}, 'opencode-ids-under-codex', 'codex')
    assert store.specialist_create(
        {'parent_session_id': 'ses_main', 'purpose': 'review', 'label': 'review',
         'prompt': 'check', 'inputs': [],
         'launch': {'tools': ['read'], 'deadline_at': 2000000000000,
                    'session_id': CHILD, 'message_id': 'ari-cm-0123abcd'}},
        'codex-ids-under-codex', host='codex')['task_id']


def test_ledger_generated_codex_message_ids_pass_their_own_validator(tmp_path):
    host = Host(tmp_path, host_id='codex')
    host.open(session=THREAD)
    host.call('focus', session=THREAD, node_id=None, role='planner', mode='auto')
    host.call('workflow', session=THREAD, action='run', fields={'state': 'running'})
    store = NativeStore(host.root)
    node = host.call('propose', session=THREAD, model_call=True, question='Q?',
                     why_now='now', plan='measure', root_reason='first')
    task = store.workflow('codex', THREAD, 'task', {'node_id': node['node_id']}, 'task')

    launch = store.node_launch_claim('codex', THREAD, task['task_id'], 1, 'claim')
    assert valid_message_id(CODEX, launch['message_id']) and launch['message_id'].startswith('ari-cm-')
    child = launch['session_id']
    store.node_launch_phase('codex', THREAD, launch['intent_id'], 'created', 'created')
    store.associate('codex', child, 'child-associate')
    store.workflow('codex', child, 'register',
                   {'role': 'node_core', 'cwd': str(host.root), 'node_id': node['node_id']}, 'child-role')
    store.focus('codex', child, node['node_id'], 'core', 'auto', 'child-focus')
    store.node_launch_phase('codex', THREAD, launch['intent_id'], 'ready', 'ready')
    assert store.runtime_reserve('codex', child, 'reserve', launch_intent_id=launch['intent_id'],
                                 message_id=launch['message_id'])['allowed']

    with store._connection() as db:
        db.execute('INSERT INTO workflow_notifications VALUES(?,?,?,?,?,?,?,?)',
                   ('notice-delivery', task['task_id'], THREAD, 'finished', None, '{}', 'pending', 'now'))
    delivery = store.notification_begin('codex', THREAD, 'notice-delivery', 'notice',
                                        'User must verify external exit')
    assert delivery['send'] and valid_message_id(CODEX, delivery['message_id'])
    store.notification_confirm('codex', THREAD, delivery['intent_id'], delivery['message_id'], 'delivered')

    with store._connection() as db:
        db.execute('INSERT INTO workflow_notifications VALUES(?,?,?,?,?,?,?,?)',
                   ('notice-wake', task['task_id'], THREAD, 'finished', None, '{}', 'delivered', 'now'))
    store.workflow('codex', THREAD, 'session',
                   {'waiting': [task['task_id']], 'pause_reason': 'wait'}, 'wait')
    wake = store.notification_wake_begin('codex', THREAD, 'notice-wake', 'wake')
    assert wake['send'] and valid_message_id(CODEX, wake['message_id'])
    assert wake['message_id'].startswith('ari-cm-wake_')
    assert store.runtime_reserve('codex', THREAD, 'reserve-wake', wake_intent_id=wake['intent_id'],
                                 message_id=wake['message_id'])['allowed']


def test_specialist_bind_child_rejects_cross_host_child_ids_before_registering(tmp_path):
    (tmp_path / 'codex').mkdir()
    codex = Host(tmp_path / 'codex', host_id='codex')
    codex.open(session=THREAD)
    assert 'not a valid codex session ID' in codex.error(
        'specialist_bind_child', session=THREAD, child_session_id='ses_child', task_id='nope')
    with pytest.raises(ValueError, match='not associated'):
        codex.service.registry.root_for('codex', 'ses_child')

    (tmp_path / 'opencode').mkdir()
    opencode = Host(tmp_path / 'opencode')
    opencode.open()
    assert 'not a valid host session ID' in opencode.error(
        'specialist_bind_child', child_session_id=THREAD, task_id='nope')
    with pytest.raises(ValueError, match='not associated'):
        opencode.service.registry.root_for('opencode', THREAD)
