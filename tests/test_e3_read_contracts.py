"""Exercise the public service paging surface, including malformed model input."""
import hashlib

import pytest

from test_e3_budget import project, reserve


@pytest.mark.parametrize('cursor', ['bad', [], [1], False, 3])
@pytest.mark.parametrize('collection', ['exploration_budget', 'evidence', 'exploration_routes'])
def test_non_object_cursors_fail_explicitly(tmp_path, cursor, collection):
    host, _ = project(tmp_path)
    result = host.raw('query', collection=collection, cursor=cursor)
    assert not result['ok'] and result['error']['code'] == 'ValidationError'


def test_budget_pages_cover_all_rows_and_bind_cursor_to_current_view(tmp_path):
    host, store = project(tmp_path)
    reserve(store, 'a')
    reserve(store, 'b', scope='ES-002')
    first = host.call('query', collection='exploration_budget', limit=1)
    assert first['total'] == 2 and first['summary']['allocated'] == 2
    second = host.call('query', collection='exploration_budget', cursor=first['cursor'], limit=1)
    assert second['cursor'] is None
    assert {first['items'][0]['intent_id'], second['items'][0]['intent_id']} == {'a', 'b'}
    for offset in [-1, 3, 0.5, '1', True]:
        bad = host.raw('query', collection='exploration_budget', cursor={**first['cursor'], 'offset': offset})
        assert not bad['ok'] and bad['error']['code'] == 'ValidationError'
    reserve(store, 'c')
    assert 'read_view_changed' in host.error('query', collection='exploration_budget', cursor=first['cursor'])


def test_bad_evidence_page_elements_do_not_discard_a_valid_page(tmp_path):
    host, _ = project(tmp_path)
    page = host.call('query', collection='evidence', refs=[])
    valid = {'snapshot_id': page['snapshot_id'], 'offset': page['offset'], 'end': page['end'],
             'manifest_digest': page['manifest_digest'], 'chunk_digest': hashlib.sha256(page['chunk'].encode()).hexdigest()}
    result = host.call('evidence_observed', pages=['bad', valid, None], plugin_instance_id='plugin')
    assert len(result['receipt_ids']) == 1
    assert result['rejected'] == [{'index': 0, 'reason': 'page_mismatch'}, {'index': 2, 'reason': 'page_mismatch'}]
