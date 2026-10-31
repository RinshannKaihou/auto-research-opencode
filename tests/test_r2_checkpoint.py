from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from auto_research.errors import CheckpointBaseRequired, CheckpointBaseMismatch, CheckpointConflict
from auto_research.native_store import NativeStore
from test_opencode_host import Host
from test_p0_contracts import node


def setup(tmp_path):
    host = Host(tmp_path)
    host.open()
    return host, NativeStore(host.root)


def observed(store, sid='ses_main', instance='instance', operation='observed'):
    view = store.checkpoint_read('opencode', sid)
    return store.checkpoint_observed('opencode', sid, view['snapshot'], instance, 'query', operation)['base']


def write(store, base, operation, *, sid='ses_main', instance='instance', **fields):
    return store.checkpoint({'state': {'next': operation}, 'base': base, **fields}, operation,
                            execution_identity=('opencode', sid), plugin_instance_id=instance)


def test_no_base_cannot_append_and_legacy_replay_precedes_new_contract(tmp_path):
    host, store = setup(tmp_path)
    with pytest.raises(CheckpointBaseRequired):
        store.checkpoint({'state': {}}, 'no-base', execution_identity=('opencode', 'ses_main'))
    with store._connection() as db:
        payload = dict(node_id=None, state={'old': True}, expected_revision=None, source_identity={})
        original = {'checkpoint_id': 'historical-response'}
        db.execute('INSERT INTO requests VALUES(?,?,?,?,?)',
                   ('legacy', 'memory.checkpoint', json.dumps(payload, sort_keys=True), json.dumps(original), 'old'))
    assert store.checkpoint({'state': {'old': True}}, 'legacy',
                            execution_identity=('opencode', 'ses_main')) == original
    with store._read() as db:
        assert db.execute('SELECT COUNT(*) FROM node_checkpoints').fetchone()[0] == 0


def test_stale_snapshot_acknowledgement_does_not_upgrade_head(tmp_path):
    host, store = setup(tmp_path)
    stale = store.checkpoint_read('opencode', 'ses_main')['snapshot']
    a = observed(store)
    result = write(store, a, 'first')
    assert result['revision'] == 1
    b = store.checkpoint_observed('opencode', 'ses_main', stale, 'instance', 'context', 'late-observed')['base']
    with pytest.raises(CheckpointConflict):
        write(store, b, 'stale-write')
    assert write(store, result['next_base'], 'second')['revision'] == 2
    assert write(store, a, 'first') == result


def test_receipt_owner_session_instance_and_generation_are_bound(tmp_path):
    host, store = setup(tmp_path)
    host.call('workflow', action='run', fields={'state': 'running'})
    base = observed(store)
    with pytest.raises(CheckpointBaseMismatch):
        write(store, base, 'restarted', instance='new-instance')
    _, sid, _ = node(host)
    with pytest.raises(CheckpointBaseMismatch):
        write(store, base, 'cross-session', sid=sid)
    # Move this main session to a different work owner without changing its ID.
    nid = host.call('propose', question='other', why_now='now', plan='p')['node_id']
    host.call('focus', node_id=nid, role='core', mode='manual')
    with pytest.raises(CheckpointBaseMismatch):
        write(store, base, 'cross-owner')
    own = observed(store, operation='node-observed')
    with store._connection() as db:
        db.execute('UPDATE workflow_project SET generation=generation+1')
    with pytest.raises(CheckpointBaseMismatch):
        write(store, own, 'cross-generation')


@pytest.mark.parametrize('value', [False, 0.0, -1, '0'])
def test_explicit_revision_must_be_integer_even_with_valid_receipt(tmp_path, value):
    from auto_research.errors import ValidationError
    host, store = setup(tmp_path)
    base = observed(store)
    with pytest.raises(ValidationError):
        write(store, base, 'invalid-revision', expected_revision=value)


def test_old_adapter_metadata_keeps_payload_identical_and_replays_after_restart(tmp_path):
    host, store = setup(tmp_path)
    base = observed(store)
    fields = {'state': {'legacy': True}}
    result = store.checkpoint(fields, 'old-adapter', execution_identity=('opencode', 'ses_main'),
                              receipt=base, plugin_instance_id='instance')
    with store._read() as db:
        payload = json.loads(db.execute("SELECT payload FROM requests WHERE request_id='old-adapter'").fetchone()[0])
        assert payload['expected_revision'] is None and 'base' not in payload
    assert store.checkpoint(fields, 'old-adapter', execution_identity=('opencode', 'ses_main'),
                            receipt=None, plugin_instance_id='new-instance') == result


def test_quarantined_global_rows_keep_raw_head_separate_from_visible_revision(tmp_path):
    host, store = setup(tmp_path)
    good = write(store, observed(store), 'good')
    store.checkpoint({'node_id': None, 'state': {'ambiguous': True}, 'expected_revision': 1}, 'historical-global')
    view = store.checkpoint_read('opencode', 'ses_main')
    assert view['checkpoint']['checkpoint_id'] == good['checkpoint_id']
    assert view['snapshot']['selected_revision'] == 1 and view['snapshot']['head_revision'] == 2
    result = write(store, observed(store, operation='recovery'), 'recover')
    assert result['revision'] == 3


@pytest.mark.parametrize('same_id', [False, True])
def test_concurrent_writes_append_once(tmp_path, same_id):
    host, store = setup(tmp_path)
    base = observed(store)
    def attempt(index):
        try:
            return write(store, base, 'same' if same_id else f'write-{index}')
        except CheckpointConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [1, 2]))
    assert sum(result is not None for result in results) == (2 if same_id else 1)
    if same_id:
        assert results[0] == results[1]
    with store._read() as db:
        assert db.execute('SELECT COUNT(*) FROM node_checkpoints').fetchone()[0] == 1


def test_query_is_readonly_and_model_cannot_acknowledge_snapshots(tmp_path):
    host, store = setup(tmp_path)
    with store._read() as db:
        before = db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
    view = host.call('query', model_call=True, collection='checkpoint')
    assert 'host controller' in host.error('checkpoint_observed', model_call=True,
        snapshot=view['snapshot'], plugin_instance_id='instance', origin='query')
    with store._read() as db:
        assert db.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == before


@pytest.mark.parametrize('field,value', [('state_digest', 'false'), ('head_revision', 100),
                                      ('selected_revision', 2), ('source_sequence', -1)])
def test_forged_or_inconsistent_snapshot_is_rejected(tmp_path, field, value):
    host, store = setup(tmp_path)
    descriptor = store.checkpoint_read('opencode', 'ses_main')['snapshot']
    descriptor[field] = value
    from auto_research.errors import ValidationError
    with pytest.raises(ValidationError):
        store.checkpoint_observed('opencode', 'ses_main', descriptor, 'instance', 'query', 'bad')
