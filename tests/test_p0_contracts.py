"""Ownership, immutable history, and atomic delivery-contract regressions."""
import json
import sqlite3

import pytest

from auto_research.errors import ConflictError, ValidationError
from auto_research.native_store import NativeStore, SCHEMA_VERSION
from auto_research.schema10 import migrate_schema10
from test_opencode_host import Host


def node(host, index=1):
    nid = host.call('propose', model_call=True, question=f'Q{index}', why_now='now',
                    plan='p', root_reason='root')['node_id']
    task = host.call('workflow', action='task', fields={'node_id': nid})
    sid = f'ses_node_{index}'
    host.call('workflow', action='task_session', fields={'task_id': task['task_id'], 'session_id': sid})
    host.call('open', session=sid, root=str(host.root), cwd=str(host.root),
              session_role='node_core', node_id=nid, context={})
    host.call('focus', session=sid, node_id=nid, role='core', mode='auto')
    return nid, sid, task['task_id']


def checkpoint(host, session='ses_main', **fields):
    return host.call('memory_write', session=session, model_call=True, action='checkpoint', fields=fields)


def view(host, sid='ses_main'):
    body = host.call('memory_context', session=sid, profile='opencode-auto')['text']
    result = {}
    for section in body.split('## ')[1:]:
        name, content = section.split('\n', 1)
        result[name] = json.loads(content.strip())
    return result


def published(host, *, status='complete', file=False, items=None):
    if file:
        (host.root / 'report.md').write_text('# Report\nCondition-dependent result.\n')
    return host.call('publish', model_call=True, status=status, summary='final', gaps=[],
                     items=items if items is not None else [
                         {'item_id': 'report', 'kind': 'report', 'source_path': 'report.md'} if file else
                         {'item_id': 'report', 'kind': 'text', 'content': {'text': 'Result with limitations.'}}
                     ], knowledge_refs=[])['publication_id']


def contract(pid, **overrides):
    return {'summary': 'Investigation ended', 'final_ref': f'pub/{pid}#report',
            'outcome': 'unresolved', 'gaps': ['Insufficient evidence'],
            'review': {'status': 'unreviewed', 'refs': [], 'limitations': ['No independent review']},
            **overrides}


def state_rows(host):
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        return {name: db.execute(f'SELECT * FROM {name}').fetchall() for name in
                ('workflow_project', 'workflow_sessions', 'project', 'attempts', 'requests',
                 'events', 'project_conclusions', 'review_todos', 'node_checkpoints')}


def test_checkpoint_owners_interleaving_replay_and_recovery(tmp_path):
    host = Host(tmp_path); host.open()
    host.call('workflow', action='run', fields={'state': 'running'})
    checkpoint(host, state={'owner': 'main'})
    n1, s1, _ = node(host, 1)
    n2, s2, _ = node(host, 2)
    fields = {'state': {'owner': n1}, 'expected_revision': 0}
    one = host.call('memory_write', session=s1, model_call=True, action='checkpoint',
                    fields=fields, operation_id='stable-checkpoint')
    two = checkpoint(host, session=s2, state={'owner': n2})
    assert one['node_id'] == n1 and two['node_id'] == n2
    assert view(host)['checkpoint']['state'] == {'owner': 'main'}
    assert view(host, s1)['checkpoint']['state'] == {'owner': n1}
    assert view(host, s2)['checkpoint']['state'] == {'owner': n2}
    host.call('finish', session=s1, state='finished', details={})
    assert host.call('memory_write', session=s1, model_call=True, action='checkpoint',
                     fields=fields, operation_id='stable-checkpoint') == one
    checkpoint(host, session=s1, state={'owner': 'after attempt'}, expected_revision=1)
    assert view(host, s1)['checkpoint']['node_id'] == n1
    assert 'revision conflict' in host.error('memory_write', session=s1, action='checkpoint',
                                            fields={'state': {}, 'expected_revision': 1})
    for bad in [None, n2]:
        assert 'must match' in host.error('memory_write', session=s1, action='checkpoint',
                                          fields={'node_id': bad, 'state': {}})
    assert 'project checkpoint' in host.error('memory_write', session=s1, action='checkpoint',
                                             fields={'visibility': 'project', 'state': {}})
    assert 'source_identity' in host.error('memory_write', session=s1, action='checkpoint',
                                          fields={'state': {}, 'source_identity': {'host_id': 'opencode', 'session_id': 'ses_main'}})


def test_manual_main_checkpoint_follows_active_node(tmp_path):
    host = Host(tmp_path); host.open()
    nid = host.call('propose', question='Q', why_now='w', plan='p')['node_id']
    host.call('focus', node_id=nid, role='core', mode='manual')
    assert checkpoint(host, state={'manual': True})['node_id'] == nid


def test_historical_globals_quarantined_without_rewriting_and_cas_survives(tmp_path):
    host = Host(tmp_path); host.open()
    host.call('workflow', action='run', fields={'state': 'running'})
    good = checkpoint(host, state={'project': True})
    _, sid, _ = node(host)
    store = NativeStore(host.root)
    bad = store.checkpoint({'node_id': None, 'state': {'node': True},
                            'source_identity': {'host_id': 'opencode', 'session_id': sid}}, 'legacy-node')
    unknown = store.checkpoint({'node_id': None, 'state': {'unknown': True}}, 'legacy-unknown')
    before = state_rows(host)['node_checkpoints']
    memory = view(host)
    assert memory['checkpoint']['checkpoint_id'] == good['checkpoint_id']
    recovery = memory['checkpoint_recovery']
    assert recovery['head_revision'] == unknown['revision']
    assert recovery['excluded_count'] == 2
    assert {x['reason'] for x in recovery['excluded']} == {'node_origin', 'ownership_unknown'}
    assert view(host, sid).get('checkpoint') is None
    assert state_rows(host)['node_checkpoints'] == before
    next_cp = checkpoint(host, state={'project': 'new'}, expected_revision=recovery['head_revision'])
    assert next_cp['revision'] == 4
    assert store.reference_query(bad['checkpoint_id'])['kind'] == 'checkpoint'


@pytest.mark.parametrize('outcome,gaps', [('answered', []), ('partial', ['Remaining question']), ('unresolved', ['Cannot decide'])])
@pytest.mark.parametrize('file,bare', [(False, False), (True, False), (False, True), (True, True)])
def test_atomic_conclusion_and_durable_queries(tmp_path, outcome, gaps, file, bare):
    host = Host(tmp_path); host.open()
    pid = published(host, file=file)
    fields = contract(pid, outcome=outcome, gaps=gaps)
    if bare:
        fields['final_ref'] = f'pub/{pid}'
    result = host.call('conclude', fields=fields, operation_id='end')
    after = state_rows(host)
    assert result['outcome'] == outcome
    assert host.call('conclude', fields=fields, operation_id='end') == result
    assert state_rows(host) == after
    state = NativeStore(host.root).control_state('opencode', 'ses_main')
    assert state['workflow']['run']['state'] == 'complete'
    assert state['workflow']['conclusion'] == result
    assert state['project']['control'] == 'paused' and state['attempt'] is None
    assert view(host)['conclusion']['final_ref'] == fields['final_ref']
    assert 'already concluded' in host.error('conclude', fields=fields)
    # A later publication cannot silently replace the registered final report.
    newer = published(host)
    from auto_research.workbench_read import summary
    board = summary(NativeStore(host.root), 'opencode', 'ses_main')
    assert board['final_publication']['publication_id'] == pid
    assert board['latest_publication']['publication_id'] == newer
    assert board['conclusion']['final_ref'] == fields['final_ref']


@pytest.mark.parametrize('kind', ['missing_item', 'missing_pub', 'bad_path', 'partial', 'empty',
                                  'missing_object', 'corrupt_object', 'missing_contract', 'missing_gaps',
                                  'missing_review_refs', 'missing_limits', 'bad_review_ref'])
def test_invalid_conclusion_has_no_writes(tmp_path, kind):
    host = Host(tmp_path); host.open()
    pid = published(host, file=kind in {'missing_object', 'corrupt_object'},
                    status='partial' if kind == 'partial' else 'complete',
                    items=[] if kind == 'empty' else None)
    fields = contract(pid)
    if kind == 'missing_item': fields['final_ref'] = f'pub/{pid}#does-not-exist'
    if kind == 'missing_pub': fields['final_ref'] = 'pub/P-missing#report'
    if kind == 'bad_path': fields['final_ref'] = f'pub/{pid}#report/../escape'
    if kind == 'empty': fields['final_ref'] = f'pub/{pid}'
    if kind == 'missing_contract': del fields['outcome']
    if kind == 'missing_gaps': fields['gaps'] = []
    if kind == 'missing_review_refs': fields['review']['status'] = 'reviewed'
    if kind == 'missing_limits': fields['review']['limitations'] = []
    if kind == 'bad_review_ref': fields['review'] = {'status': 'reviewed', 'refs': ['pub/P-missing#review'], 'limitations': []}
    if kind in {'missing_object', 'corrupt_object'}:
        with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
            version = db.execute('SELECT object_version FROM publication_items WHERE publication_id=?', (pid,)).fetchone()[0]
        target = host.root / '.research/objects' / version
        target.chmod(0o644)
        if kind == 'missing_object': target.unlink()
        else: target.write_text('tampered')
    before = state_rows(host)
    assert host.error('conclude', fields=fields)
    assert state_rows(host) == before


@pytest.mark.parametrize('review_status', ['partial', 'reviewed'])
def test_review_is_self_declared_with_frozen_material(tmp_path, review_status):
    host = Host(tmp_path); host.open()
    review_pid = published(host, file=True)
    final_pid = published(host)
    fields = contract(final_pid, review={'status': review_status, 'refs': [f'pub/{review_pid}#report'],
                                       'limitations': ['Only one claim was checked'] if review_status == 'partial' else []})
    assert host.call('conclude', fields=fields)['review'] == fields['review']


def test_live_work_and_role_guards_and_workflow_bypass(tmp_path):
    host = Host(tmp_path); host.open()
    host.call('workflow', action='run', fields={'state': 'running'})
    pid = published(host)
    _, sid, tid = node(host)
    assert 'main' in host.error('conclude', session=sid, fields=contract(pid))
    assert 'Live node tasks' in host.error('conclude', fields=contract(pid))
    host.call('finish', session=sid, state='finished', details={})
    # Existing pending todos and open questions do not block delivery.
    assert host.call('conclude', fields=contract(pid))['outcome'] == 'unresolved'
    assert 'Use conclude' in host.error('workflow', action='run', fields={'state': 'complete'})
    assert 'new_generation' in host.error('workflow', action='run', fields={'state': 'running'})
    host.call('workflow', action='run', fields={'state': 'running', 'new_generation': True})
    assert host.call('status')['workflow']['conclusion'] is None


def test_transaction_failure_rolls_back_receipt_attempt_and_run(tmp_path, monkeypatch):
    host = Host(tmp_path); host.open()
    pid = published(host)
    store = NativeStore(host.root)
    before = state_rows(host)
    original = store._event
    def fail(db, kind, data):
        if kind == 'project.concluded': raise RuntimeError('injected transaction failure')
        original(db, kind, data)
    monkeypatch.setattr(store, '_event', fail)
    with pytest.raises(RuntimeError, match='injected'):
        store.conclude('opencode', 'ses_main', contract(pid), 'retryable')
    assert state_rows(host) == before
    monkeypatch.setattr(store, '_event', original)
    assert store.conclude('opencode', 'ses_main', contract(pid), 'retryable')['final_ref'] == f'pub/{pid}#report'


def test_live_specialist_prevents_conclusion(tmp_path):
    host = Host(tmp_path); host.open()
    pid = published(host)
    host.call('specialist_create', fields={'purpose': 'review', 'label': 'review', 'prompt': 'Read the draft', 'inputs': []})
    before = state_rows(host)
    assert 'Live specialist' in host.error('conclude', fields=contract(pid))
    assert state_rows(host) == before


def test_manual_work_prevents_conclusion_even_without_dispatched_tasks(tmp_path):
    host = Host(tmp_path); host.open()
    pid = published(host)
    nid = host.call('propose', question='manual Q', why_now='now', plan='p')['node_id']
    host.call('finish', state='finished', details={})
    host.call('focus', node_id=nid, role='core', mode='manual')
    assert 'work segments' in host.error('conclude', fields=contract(pid))


def test_schema10_migration_rolls_back_on_failure(tmp_path, monkeypatch):
    from auto_research import schema10
    host = Host(tmp_path); host.open()
    database = host.root / '.research/state.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('DROP TABLE project_conclusions')
        db.execute('PRAGMA user_version=9')
    original = schema10.ensure_schema10
    def fail(db):
        original(db)
        raise RuntimeError('migration failed')
    monkeypatch.setattr(schema10, 'ensure_schema10', fail)
    with pytest.raises(RuntimeError, match='migration failed'):
        migrate_schema10(database)
    with sqlite3.connect(database) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 9
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='project_conclusions'").fetchone()


def test_schema9_readonly_and_upgrade_keep_historical_completion(tmp_path):
    host = Host(tmp_path); host.open()
    checkpoint(host, state={'retained': True})
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        db.execute('DROP TABLE project_conclusions')
        db.execute("UPDATE workflow_project SET state='complete'")
        db.execute('PRAGMA user_version=9')
        original = db.execute('SELECT * FROM node_checkpoints').fetchall()
    readonly = NativeStore(host.root, readonly=True)
    state = readonly.control_state('opencode', 'ses_main')
    assert state['schema_version'] == 9 and state['workflow']['conclusion'] is None
    assert 'not_recorded' in readonly.context_view('opencode', 'ses_main')['text']
    store = NativeStore(host.root)
    state = store.control_state('opencode', 'ses_main')
    assert state['schema_version'] == SCHEMA_VERSION == 10
    assert state['workflow']['run']['state'] == 'complete' and state['workflow']['conclusion'] is None
    with store._read() as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM node_checkpoints')] == original
    with sqlite3.connect(host.root / '.research/schema-9-backup.sqlite3') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 9
    migrate_schema10(store.db_path)  # idempotent
