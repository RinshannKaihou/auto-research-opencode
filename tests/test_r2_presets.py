import pytest

from auto_research.errors import ValidationError, ConflictError
from auto_research.native_store import NativeStore


@pytest.fixture
def seeded(tmp_path):
    store = NativeStore(tmp_path)
    store.initialize('revision fixture', 'init')
    store.associate('opencode', 'main', 'associate')
    store.workflow('opencode', 'main', 'register', {'role': 'main', 'cwd': str(tmp_path)}, 'role')
    value = store.record_knowledge({'kind': 'hypothesis', 'statement': 'original',
        'execution_refs': ['session:main', 'session:unknown']}, 'record')
    return store, value


def revise(store, value, operation='revise', **overrides):
    return store.revise_knowledge({'ref': value['ref'], 'changes': {'statement': 'reworded'},
        'reason': 'clearer wording', 'preset': 'editorial', **overrides}, operation)


def test_preservation_is_explicit_and_does_not_upgrade_unlinked_sources(seeded):
    store, old = seeded
    saved = revise(store, old, preserve_sources=True, no_new_evidence=True)
    assert saved['execution_refs'] == old['execution_refs']
    assert saved['provenance'] == {'status': 'unlinked', 'preserved_from': old['ref'], 'execution_verified': False}
    assert saved['applied']['affected_scope_mode'] == 'none'
    assert saved['applied']['change_kind'] == 'reword'
    assert saved['asserted_at']['operation_id'] == 'revise'
    later = revise(store, saved, 'later')
    assert later['execution_refs'] == []
    assert later['provenance']['status'] == 'unknown'
    assert revise(store, old, preserve_sources=True, no_new_evidence=True) == saved
    assert store.reference_query(saved['ref'])['value']['execution_refs'] == old['execution_refs']


def test_correct_previous_targets_only_the_fixed_previous_version(seeded):
    store, old = seeded
    first = revise(store, old)
    second = revise(store, first, 'correct', preset='correct_previous')
    assert second['applied']['affected_scope'] == [first['ref']]
    assert second['applied']['change_kind'] == 'correct'
    assert second['applied']['affected_scope_mode'] == 'versions'
    with pytest.raises(ConflictError):
        revise(store, old, 'stale', preset='correct_previous')


@pytest.mark.parametrize('overrides', [
    {'changes': {'scope': {'dataset': 'new'}}},
    {'changes': {'conditions': {'temperature': 1}}},
    {'change_kind': 'correct'},
    {'affected_scope_mode': 'versions'},
    {'affected_scope': ['knowledge/K-001@1']},
    {'preset': 'correct_previous', 'affected_scope': []},
    {'preserve_sources': True},
    {'no_new_evidence': True},
    {'preserve_sources': 'yes', 'no_new_evidence': True},
    {'preserve_sources': True, 'no_new_evidence': True, 'execution_refs': []},
    {'preset': {}},
])
def test_conflicting_preset_and_source_declarations_are_atomic(seeded, overrides):
    store, old = seeded
    with pytest.raises(ValidationError):
        revise(store, old, **overrides)
    assert store.reference_query(old['ref'])['value']['statement'] == 'original'
    with store._read() as db:
        assert db.execute('SELECT COUNT(*) FROM knowledge_revisions').fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM requests WHERE request_id='revise'").fetchone()[0] == 0


def test_new_evidence_cannot_preserve_sources_but_unknown_new_source_is_allowed(seeded):
    store, old = seeded
    store.focus('opencode', 'main', None, 'planner', 'manual', 'focus')
    note = store.note('opencode', 'main', 'new evidence', 'condition', 'note')
    changes = {'statement': 'new result', 'evidence_refs': [note['note_id']]}
    with pytest.raises(ValidationError, match='source_preservation_conflict'):
        revise(store, old, preset='correct_previous', changes=changes,
               preserve_sources=True, no_new_evidence=True)
    saved = revise(store, old, preset='correct_previous', changes=changes,
                   execution_refs=['session:still-unknown'])
    assert saved['provenance']['status'] == 'unlinked'
    assert saved['execution_refs'][0]['status'] == 'unlinked'


def test_preserving_empty_history_keeps_provenance_unknown(seeded):
    store, old = seeded
    empty = revise(store, old)
    saved = revise(store, empty, 'preserve-empty', preserve_sources=True, no_new_evidence=True)
    assert saved['execution_refs'] == []
    assert saved['provenance']['status'] == 'unknown'


def test_custom_and_old_explicit_semantics_match(seeded):
    store, old = seeded
    custom = revise(store, old, preset='custom', change_kind='narrow',
                    affected_scope_mode='unknown', affected_scope=[], changes={'scope': {'dataset': 'limited'}})
    assert custom['applied']['affected_scope_mode'] == 'unknown'
    assert custom['change']['kind'] == 'narrow'


def test_source_preservation_is_rejected_on_record(seeded):
    store, _ = seeded
    with pytest.raises(ValidationError, match='revise, not record'):
        store.record_knowledge({'kind': 'hypothesis', 'statement': 'new',
                                'preserve_sources': True, 'no_new_evidence': True}, 'new')
