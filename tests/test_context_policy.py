"""R1 regressions: ordinary discoveries, critical risks, complete reads and budgets."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from auto_research.native_store import NativeStore
from auto_research import field_checks
from test_opencode_host import Host
from test_p0_contracts import node


@pytest.fixture
def completed(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('workflow', action='run', fields={'state': 'running'})
    nid, sid, task = node(host)
    source = host.call('publish', session=sid, model_call=True, status='complete', summary='data', gaps=[],
                       items=[{'item_id': 'data', 'kind': 'text', 'content': {'measured': 7}}], knowledge_refs=[])
    claim = host.call('memory_write', session=sid, model_call=True, action='record', fields={
        'kind': 'claim', 'statement': 'ORDINARY_FINDING: measured 7',
        'scope': {'fixture': 'synthetic'}, 'conditions': {'case': 'one'},
        'evidence_refs': [f"pub/{source['publication_id']}#data"]})
    publication = host.call('publish', session=sid, model_call=True, status='complete', summary='result', gaps=[],
                           items=[{'item_id': 'report', 'kind': 'text', 'content': {'text':'Node result under fixture conditions'}}], knowledge_refs=[claim['ref']])
    host.call('finish', session=sid, state='finished', details={'reason': 'complete'})
    return host, NativeStore(host.root), nid, sid, claim, publication


def sections(view):
    return {name: json.loads(payload) for name, payload in
            (part.split('\n', 1) for part in view['text'].split('## ')[1:])}


def rows(store):
    with store._read() as db:
        return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table}')]
                for table in ('events','knowledge_revisions','relations','review_todos','field_checks')}


def test_legacy_omission_and_new_ordinary_discovery(completed):
    host, store, nid, sid, claim, pub = completed
    before = rows(store)
    legacy = host.call('memory_context', profile='opencode-auto')
    baseline = json.loads((Path(__file__).parent / 'fixtures/context_policy/legacy.json').read_text())
    assert ('ORDINARY_FINDING' in legacy['text']) == baseline['ordinary_finding_visible']
    assert sorted(sections(legacy)['knowledge']) == baseline['knowledge_groups']
    view = host.call('memory_context', profile='opencode-auto', policy='memory-r1-v1')
    finding = next(v for v in sections(view)['findings']['items'] if v['ref'] == claim['ref'])
    for key in ('ref','node_id','kind','status','scope','conditions'):
        assert finding[key] == claim[key]
    assert finding['expand'] == {'ref': claim['ref']}
    assert host.call('query', collection='completed_findings')['items'][0]['finding_count'] == 1
    assert rows(store) == before
    shadow = host.call('context_shadow', profile='opencode-auto')
    assert shadow['memory-v2']['text'] == legacy['text']
    assert shadow['memory-v2']['source_sequence'] == shadow['memory-r1-v1']['source_sequence']


def test_retracted_basis_and_attention_beyond_clerical_page(completed):
    host, store, nid, sid, claim, pub = completed
    for i in range(11):
        store.record_knowledge({'kind': 'hypothesis', 'statement': f'mentions K-999@1 ({i})'}, f'clerical-{i}')
    citer = store.record_knowledge({'kind': 'claim','statement':'cites withdrawn result',
                                    'evidence_refs':[claim['ref']]}, 'citer')
    withdrawn = host.call('memory_write', session=sid, model_call=True, action='revise', fields={
        'ref':claim['ref'], 'expected_revision':1, 'changes':{'status':'retracted'},
        'reason':'withdrawn', 'change_kind':'retract', 'affected_scope_mode':'versions',
        'affected_scope':[claim['ref']]})
    citer = store.record_knowledge({'kind':'claim','statement':'directly cites retracted revision',
        'evidence_refs':[withdrawn['ref']]}, 'direct-retracted-citer')
    page = host.call('query', collection='critical_now', limit=200)
    assert any(v['kind'] == 'retracted_basis' and v['target'] == citer['ref'] for v in page['items'])
    assert any(v['kind'] == 'publication' and v['target'] == f"pub/{pub['publication_id']}"
               and v['risk']['status'] == 'attention' for v in page['items'])
    view = sections(host.call('memory_context', policy='memory-r1-v1'))
    assert view['critical_now']['total'] == page['total']
    assert len(view['findings']['items']) > 0
    assert view['clerical_index']['by_class']['prose_mention_without_relation'] >= 11


@pytest.mark.parametrize('budget', [500, 1200, 12000])
def test_long_conditions_keep_identifiers_and_budget(completed, budget):
    host, store, nid, sid, claim, pub = completed
    revised = store.revise_knowledge({'ref':claim['ref'], 'expected_revision':1,
        'changes':{'conditions':{'case':'条件'*1000}}, 'reason':'long conditions',
        'affected_scope_mode':'none', 'change_kind':'reword'}, 'long')
    result = host.call('memory_context', policy='memory-r1-v1', max_chars=budget)
    assert len(result['text']) <= budget
    for value in sections(result)['findings'].get('items', []):
        assert store.reference_query(value['ref'])['value']['ref'] == value['ref']
        if value['ref'] == revised['ref']:
            assert 'conditions' in value['truncated_fields']
    if budget == 12000:
        assert revised['ref'] in result['text'] and claim['ref'] in result['text']


def test_cursor_survives_usage_but_not_changed_findings(completed):
    host, store, nid, sid, claim, pub = completed
    for i in range(3):
        store.record_knowledge({'kind':'hypothesis','statement':f'background {i}'}, f'item-{i}')
    first = host.call('query', collection='findings', limit=1)
    store.begin_usage('llm','usage-start', host_id='opencode',session_id='ses_main')
    store.finish_usage('llm',9,'actual',{},'usage-end')
    second = host.call('query', collection='findings', limit=1, cursor=first['cursor'])
    assert second['items'][0]['ref'] != first['items'][0]['ref']
    store.record_knowledge({'kind':'hypothesis','statement':'new background'}, 'new')
    assert 'read_view_changed' in host.error('query', collection='findings', cursor=first['cursor'])


def test_field_check_reads_stored_failures(completed, monkeypatch):
    host, store, nid, sid, claim, pub = completed
    # Evaluation facts are immutable; this fixture supplies both stored failure classes.
    with sqlite3.connect(store.db_path) as db:
        for i, status in enumerate(('inconsistent','not_checkable')):
            db.execute('INSERT INTO field_checks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (claim['ref'],i,'{}','pub/P-001#data',None,None,None,None,'fixture',status,
                 'value_mismatch' if i == 0 else 'missing_value','2026-10-31',0))
    monkeypatch.setattr(field_checks,'evaluate',lambda *a,**kw:pytest.fail('read evaluated checks'))
    before = rows(store)
    page = host.call('query', collection='critical_now')
    found = [v for v in page['items'] if v['kind'] == 'field_check']
    assert {v['risk']['result'] for v in found} == {'inconsistent','not_checkable'}
    assert rows(store) == before


def test_item_scope_and_explicit_empty_references(completed):
    host, store, nid, sid, claim, pub = completed
    publication = host.call('publish', model_call=True, status='complete', summary='items', gaps=[],
        items=[{'item_id':'one','kind':'text','content':{},'knowledge_refs':[claim['ref']]},
               {'item_id':'empty','kind':'text','content':{},'knowledge_refs':[]}], knowledge_refs=[])
    target = f"pub/{publication['publication_id']}#empty"
    values = host.call('query',collection='critical_now',final_ref=target)['items']
    item = next(v for v in values if v['target'] == target)
    assert item['risk']['status'] == 'coverage unknown'
    assert item['risk']['versions'] == {}


def test_shadow_holds_one_sqlite_snapshot(completed, monkeypatch):
    host, store, *_ = completed
    original = NativeStore._context_view
    wrote = False

    def interleaved(self, db, host_id, session_id, maximum, profile, policy, refs, final):
        nonlocal wrote
        if policy == 'memory-r1-v1' and not wrote:
            store.record_knowledge({'kind':'hypothesis','statement':'AFTER_SNAPSHOT'}, 'after-snapshot')
            wrote = True
        return original(self, db, host_id, session_id, maximum, profile, policy, refs, final)

    monkeypatch.setattr(NativeStore, '_context_view', interleaved)
    shadow = host.call('context_shadow')
    assert shadow['memory-v2']['source_sequence'] == shadow['memory-r1-v1']['source_sequence']
    assert all('AFTER_SNAPSHOT' not in value['text'] for value in shadow.values())
    assert 'AFTER_SNAPSHOT' in host.call('memory_context', policy='memory-r1-v1')['text']


def test_query_and_board_page_all_completed_nodes(completed):
    host, store, *_ = completed
    for index in range(2, 102):
        nid, sid, task = node(host, index)
        host.call('finish', session=sid, state='finished', details={})
    found = []
    cursor = None
    while True:
        result = host.call('project_read', root=str(host.root), view='page',
                           collection='completed_findings', limit=50, **({'cursor':cursor} if cursor else {}))
        found += result['items']
        cursor = result['cursor']
        if cursor is None:
            break
    assert len(found) == len({v['node_id'] for v in found}) == 101
    assert sum(v['status'] == '未登记发现' for v in found) == 100


def test_policy_configuration_is_host_main_only_and_persistent(completed):
    host, store, nid, sid, *_ = completed
    assert 'host controller' in host.error('context_policy', model_call=True, policy='memory-r1-v1')
    assert 'main session' in host.error('context_policy', session=sid, policy='memory-r1-v1')
    with store._connection() as db:
        db.execute("UPDATE project SET config=?", (json.dumps({'other':'preserve'}),))
    before = rows(store)
    result = host.call('context_policy', policy='memory-r1-v1', operation_id='set-r1')
    assert result['policy_version'] == 'memory-r1-v1'
    assert host.call('memory_context')['policy_version'] == 'memory-r1-v1'
    assert host.call('context_policy', policy='memory-r1-v1', operation_id='set-r1') == result
    with store._read() as db:
        assert json.loads(db.execute('SELECT config FROM project').fetchone()[0])['other'] == 'preserve'
    assert rows(store)['review_todos'] == before['review_todos']
    host.call('context_policy', policy='memory-v2')
    assert host.call('memory_context')['policy_version'] == 'memory-v2'


def test_todo_filters_precede_paging(completed):
    host, store, nid, sid, claim, *_ = completed
    wanted = store.queue_review_in_tx
    for i in range(4):
        with store._connection() as db:
            wanted(db, 'selected' if i == 3 else 'unrelated', f'fixture-{i}', nid)
    query = host.call('query',collection='review_todos',node_id=nid,trigger_kind='selected',state='pending',limit=1)
    assert len(query['items']) == 1 and query['items'][0]['trigger_ref'] == 'fixture-3'
    page = host.call('project_read',root=str(host.root),view='page',collection='review_todos',
                     node_id=nid,trigger_kind='selected',state='pending',limit=1)
    assert page['total'] == 1 and page['items'][0]['trigger_ref'] == 'fixture-3'


def test_schema9_derived_views_remain_readonly(completed):
    host, store, nid, sid, claim, *_ = completed
    with sqlite3.connect(store.db_path) as db:
        db.execute('DROP TABLE project_conclusions')
        db.execute('PRAGMA user_version=9')
    readonly = NativeStore(host.root, readonly=True)
    before = rows(readonly)
    context = readonly.context_view('opencode','ses_main',policy='memory-r1-v1')
    assert claim['ref'] in context['text']
    assert readonly.context_page('critical_now')['total'] > 0
    assert readonly.context_page('completed_findings')['total'] == 1
    assert rows(readonly) == before
    with readonly._read() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 9
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='project_conclusions'").fetchone()


def test_direct_retracted_input_is_not_lost_without_citer_row(completed):
    host, store, nid, sid, claim, *_ = completed
    withdrawn = store.revise_knowledge({'ref':claim['ref'],'expected_revision':1,
        'changes':{'status':'retracted'},'reason':'withdraw','change_kind':'retract',
        'affected_scope_mode':'versions','affected_scope':[claim['ref']]}, 'withdraw-input')
    page = host.call('query',collection='critical_now',decision_refs=[withdrawn['ref']])
    risk = next(v for v in page['items'] if v['kind'] == 'input_reference' and v['target'] == withdrawn['ref'])
    assert risk['relevance'] == 'related'
    assert any(v['reason'] == 'retracted_ref' for v in risk['risk']['reasons'])


def test_preview_compares_without_model_or_ledger_writes(completed):
    host, store, nid, sid, claim, publication = completed
    # A completed project can be inspected without reopening a generation.
    from test_p0_contracts import contract
    host.call('conclude', fields=contract(publication['publication_id']))
    before = rows(store)
    with store._read() as db:
        workflow_before = [tuple(r) for r in db.execute('SELECT * FROM workflow_project')]
        requests_before = db.execute('SELECT COUNT(*) FROM context_requests').fetchone()[0]
    preview = host.call('project_read',root=str(host.root),view='context_preview')
    assert preview['available'] and preview['session']['role'] == 'main'
    assert preview['last_recorded'] is None
    assert preview['policy_version'] == 'memory-v2'
    assert preview['views']['memory-v2']['source_sequence'] == preview['views']['memory-r1-v1']['source_sequence']
    assert claim['ref'] in preview['metrics']['memory-r1-v1']['visible_refs']
    assert rows(store) == before
    with store._read() as db:
        assert workflow_before == [tuple(r) for r in db.execute('SELECT * FROM workflow_project')]
        assert db.execute('SELECT COUNT(*) FROM context_requests').fetchone()[0] == requests_before
    actual = host.call('memory_context',policy='memory-r1-v1')
    host.call('context_record',fields={'body':actual['text'],'policy_version':actual['policy_version'],
                                    'source_sequence':actual['source_sequence']})
    before_policy_read = rows(store)
    policy = host.call('context_policy')
    assert policy['policy_version'] == 'memory-v2'
    assert policy['last_recorded']['policy_version'] == 'memory-r1-v1'
    assert rows(store) == before_policy_read
    host.call('context_policy',policy='memory-r1-v1')
    preview = host.call('project_read',root=str(host.root),view='context_preview')
    assert preview['last_recorded']['text'] == actual['text']
    assert preview['last_recorded']['policy_version'] == 'memory-r1-v1'
    assert 'host' in host.error('project_read',root=str(host.root),view='context_preview',model_call=True).lower()
    assert 'not associated' in host.error('project_read',root=str(host.root),view='context_preview',preview_session_id='unknown')


def test_clerical_expansion_excludes_attention(completed):
    host, store, nid, sid, claim, pub = completed
    for index in range(11):
        store.record_knowledge({'kind':'hypothesis','statement':f'K-999@1 mention {index}'},f'clerical-{index}')
    store.revise_knowledge({'ref':claim['ref'],'expected_revision':1,'changes':{'status':'retracted'},
        'reason':'withdraw','change_kind':'retract','affected_scope_mode':'versions','affected_scope':[claim['ref']]},'withdraw')
    page = host.call('query',collection='hints',kind='clerical',limit=200)
    assert page['total'] == 11
    assert all(row['class'] != 'complete_publication_cites_risk' for row in page['items'])
    board = host.call('project_read',root=str(host.root),view='page',collection='hints',kind='clerical',limit=100)
    assert board['items'] == page['items']


def test_all_small_budgets_preserve_expansion_and_maximum(completed):
    host, store, nid, sid, claim, *_ = completed
    for budget in range(500, 12001, 137):
        view = store.context_view('opencode','ses_main',max_chars=budget,policy='memory-r1-v1')
        assert len(view['text']) <= budget
        blocks = sections(view)
        for name in ('critical_now','findings'):
            assert 'expand' in blocks[name]
            assert blocks[name]['details_complete'] or blocks[name]['expand_required']


def test_disputes_and_backlog_do_not_replace_completed_findings(completed):
    host, store, nid, sid, claim, *_ = completed
    for index in range(20):
        store.record_knowledge({'kind':'hypothesis','status':'disputed','statement':f'Dispute {index}'}, f'dispute-{index}')
        with store._connection() as db:
            store.queue_review_in_tx(db,'fixture-trigger-'+str(index),f'fixture-{index}',None)
    result = host.call('memory_context',policy='memory-r1-v1')
    blocks = sections(result)
    assert any(v['ref']==claim['ref'] for v in blocks['findings']['items'])
    assert any('disputed' in v['selection_reasons'] for v in blocks['findings']['items'])
    assert result['block_chars']['review_backlog'] <= 600
    assert result['block_chars']['review_backlog'] + result['block_chars']['clerical_index'] <= result['block_chars']['findings']


def test_chunked_reference_rejects_mixed_review_states(completed):
    host, store, *_ = completed
    todo = store.history_page('review_todos',limit=1)['items'][0]
    first = host.call('query',ref=todo['todo_id'],offset=0,limit=64)
    store.review_todo_state(todo['todo_id'],'completed','complete-review')
    assert 'read_view_changed' in host.error('query',ref=todo['todo_id'],offset=64,limit=64,
                                            expected_digest=first['content_sha256'])
    new = host.call('query',ref=todo['todo_id'],offset=0,limit=64)
    assert new['content_sha256'] != first['content_sha256']


def test_explicit_node_input_exposes_its_discoveries_and_origin(completed):
    host, store, nid, sid, claim, *_ = completed
    page = host.call('query',collection='findings',decision_refs=[nid])
    value = next(v for v in page['items'] if v['ref']==claim['ref'])
    assert 'declared_node_input' in value['scope_origins']
    assert 'explicit_input' in value['selection_reasons']
    assert 'decision_refs must' in host.error('query',collection='findings',decision_refs='not-a-list')
    assert 'final_ref must' in host.error('query',collection='critical_now',final_ref='')


def test_fixed_versions_keep_content_and_show_current_pointer(completed):
    host, store, nid, sid, claim, *_ = completed
    original = store.reference_query(claim['ref'])['value']
    revision = store.revise_knowledge({'ref':claim['ref'],'expected_revision':1,
        'changes':{'statement':'Updated wording'},'reason':'editorial','change_kind':'reword',
        'affected_scope_mode':'none'},'wording-update')
    fixed = host.call('query',ref=claim['ref'])
    assert fixed['value'] == original
    assert fixed['version_context']['current_ref'] == revision['ref']
    assert claim['ref'] in fixed['version_context']['published_refs']
    result = host.call('memory_context',policy='memory-r1-v1')
    displayed = sections(result)['findings']['items']
    assert {claim['ref'],revision['ref']} <= {v['ref'] for v in displayed}
    assert next(v for v in displayed if v['ref']==claim['ref'])['current_ref'] == revision['ref']


@pytest.mark.parametrize('condition',[False,0,'only on the held-out fixture'])
def test_recorded_scalar_conditions_are_not_relabelled_missing(completed,condition):
    host,store,nid,sid,claim,*_ = completed
    revision=store.revise_knowledge({'ref':claim['ref'],'expected_revision':1,
        'changes':{'conditions':condition},'reason':'explicit condition','change_kind':'reword',
        'affected_scope_mode':'none'},'condition')
    view=host.call('memory_context',policy='memory-r1-v1')
    item=next(v for v in sections(view)['findings']['items'] if v['ref']==revision['ref'])
    assert item['conditions']==condition


@pytest.mark.parametrize('method', ['query', 'history_page', 'workbench_page', 'project_read'])
def test_filtered_todo_cursor_restores_scope_across_all_read_routes(completed, method):
    host, store, nid, *_ = completed
    for i in range(12):
        with store._connection() as db:
            store.queue_review_in_tx(db, 'wanted' if i % 2 == 0 else 'other', f'page-{i}', nid)
    request = {'collection': 'review_todos', 'limit': 2}
    if method == 'project_read':
        request.update(root=str(host.root), view='page')
    first = host.call(method, **request, node_id=nid, state='pending', trigger_kind='wanted')
    assert first['total'] == 6
    cursor = first['cursor']
    found = list(first['items'])
    while cursor:
        following = host.call(method, **request, cursor=cursor)
        found.extend(following['items'])
        cursor = following['cursor']
    assert [v['trigger_ref'] for v in found] == [f'page-{i}' for i in range(0, 12, 2)]
    assert 'read_view_changed' in host.error(method, **request, cursor=first['cursor'], state='completed')
    assert 'read_view_changed' in host.error(method, **request,
        cursor={**first['cursor'], 'project_id': 'another-project'})
    legacy = host.call(method, **{**request, **first['cursor']})
    assert legacy['items'][0]['trigger_ref'] == 'page-4'


@pytest.mark.parametrize('method', ['query', 'history_page', 'workbench_page', 'project_read'])
def test_clerical_cursor_restores_class_and_position(completed, method):
    host, store, nid, sid, claim, *_ = completed
    for i in range(11):
        store.record_knowledge({'kind': 'hypothesis', 'statement': f'mentions K-999@1 ({i})'}, f'page-hint-{i}')
    store.revise_knowledge({'ref': claim['ref'], 'expected_revision': 1, 'changes': {'status': 'retracted'},
        'reason': 'withdrawn', 'change_kind': 'retract', 'affected_scope_mode': 'versions',
        'affected_scope': [claim['ref']]}, 'withdraw-for-page')
    request = {'collection': 'hints', 'limit': 3}
    if method == 'project_read':
        request.update(root=str(host.root), view='page')
    first = host.call(method, **request, kind='clerical')
    assert first['total'] == 11
    found = list(first['items'])
    cursor = first['cursor']
    while cursor:
        following = host.call(method, **request, cursor=cursor)
        found.extend(following['items'])
        cursor = following['cursor']
    assert len(found) == len({(v['class'], v['target']) for v in found}) == 11
    assert all(v['class'] != 'complete_publication_cites_risk' for v in found)
    assert 'read_view_changed' in host.error(method, **request, cursor=first['cursor'], kind='complete_publication_cites_risk')


def test_decision_scope_rejects_unknown_refs_but_accepts_registered_snapshots(completed):
    host, store, *_ = completed
    for ref in ('X-999', 'not-a-reference', 'S-999', 'pub/P-999'):
        assert host.raw('query', collection='findings', decision_refs=[ref])['ok'] is False
    host.call('focus', node_id=None, role='planner', mode='manual')
    (host.root / 'input.txt').write_text('registered input')
    snapshot = host.call('snapshot', paths=['input.txt'])
    for ref in (snapshot['snapshot_id'], snapshot['snapshot_id'] + '#input.txt'):
        before = rows(store)
        assert host.raw('query', collection='findings', decision_refs=[ref])['ok'] is True
        assert rows(store) == before


def test_active_attempt_is_rendered_without_spurious_truncation(completed):
    host, store, *_ = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    attempt = host.call('status')['attempt']
    view = host.call('memory_context', policy='memory-r1-v1', profile='opencode-auto')
    assert sections(view)['attempt']['attempt_id'] == attempt['attempt_id']
    assert 'attempt' not in view['omitted']
    assert all(view['coverage'][key]['details_complete'] for key in ('critical_now', 'findings'))
    assert view['truncated'] is False


def test_small_budget_expansion_keeps_scope_or_rejects_unrepresentable_budget(completed):
    host, store, nid, sid, claim, pub = completed
    ref = f"pub/{pub['publication_id']}#report"
    view = host.call('memory_context', policy='memory-r1-v1', max_chars=500, decision_refs=[ref])
    assert len(view['text']) <= 500
    expansion = sections(view)['critical_now']['expand']
    assert expansion['decision_refs'] == [ref]
    assert host.call('query', **expansion)['total'] == view['coverage']['critical_now']['total']
    refs = [store.record_knowledge({'kind': 'hypothesis', 'statement': f'input {i}'}, f'scope-{i}')['ref'] for i in range(30)]
    before = rows(store)
    assert 'Context budget too small for decision scope' in host.error(
        'memory_context', policy='memory-r1-v1', max_chars=500, decision_refs=refs)
    assert rows(store) == before


def test_priority_finding_is_not_replaced_by_cheaper_background(completed):
    host, store, nid, sid, claim, *_ = completed
    revised = store.revise_knowledge({'ref': claim['ref'], 'expected_revision': 1,
        'changes': {'statement': 'ORDINARY ' + 'X' * 1000, 'scope': {'long': 'S' * 1000}, 'conditions': {'long': 'C' * 1000}},
        'reason': 'long finding', 'change_kind': 'reword', 'affected_scope_mode': 'none'}, 'long-priority')
    tiny = store.record_knowledge({'kind': 'hypothesis', 'status': 'disputed', 'statement': 'tiny'}, 'tiny-background')
    view = sections(host.call('memory_context', policy='memory-r1-v1', max_chars=2150, profile='opencode-auto'))['findings']
    shown = {v['ref'] for v in view.get('items', [])}
    assert revised['ref'] in shown or revised['ref'] in view.get('deferred_refs', [])
    if revised['ref'] not in shown:
        assert tiny['ref'] not in shown
        assert view['expand_required']


@pytest.mark.parametrize('retained_by', ['publication', 'support', 'explicit'])
def test_obsolete_field_failures_are_historical_unless_still_referenced(completed, retained_by):
    host, store, nid, sid, claim, *_ = completed
    old = store.record_knowledge({'kind': 'claim', 'statement': 'old value', 'evidence_refs': ['pub/P-001#data']}, 'old-value')
    current = store.revise_knowledge({'ref': old['ref'], 'expected_revision': 1, 'changes': {'statement': 'corrected value'},
        'reason': 'fix', 'change_kind': 'reword', 'affected_scope_mode': 'none'}, 'correct-value')
    with sqlite3.connect(store.db_path) as db:
        for ref, result in ((old['ref'], 'inconsistent'), (current['ref'], 'consistent')):
            db.execute('INSERT INTO field_checks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (ref, 0, '{}', 'pub/P-001#data', None, None, None, None, 'fixture', result,
                 'value_mismatch' if result == 'inconsistent' else None, '2026-10-31', 0))
    query = {'collection': 'critical_now', 'kind': 'field_check'}
    assert host.call('query', **query)['items'] == []
    assert store.reference_query(old['ref'])['value']['field_checks'][0]['result'] == 'inconsistent'
    if retained_by == 'publication':
        host.call('focus', node_id=None, role='planner', mode='manual')
        host.call('publish', status='complete', summary='pins old value', gaps=[], knowledge_refs=[old['ref']],
            items=[{'item_id': 'report', 'kind': 'text', 'content': {'text': 'Old basis retained'}}])
    elif retained_by == 'support':
        store.record_knowledge({'kind': 'claim', 'statement': 'uses old value', 'evidence_refs': [old['ref']]}, 'old-support')
    else:
        query['decision_refs'] = [old['ref']]
    before = rows(store)
    assert old['ref'] in {v['target'] for v in host.call('query', **query)['items']}
    assert rows(store) == before


def test_same_policy_does_not_emit_events_or_break_derived_cursor(completed):
    host, store, *_ = completed
    for i in range(2):
        store.record_knowledge({'kind': 'hypothesis', 'statement': f'page {i}'}, f'policy-page-{i}')
    first = host.call('query', collection='findings', limit=1)
    host.call('context_policy', policy='memory-v2')
    following = host.call('query', collection='findings', cursor=first['cursor'], limit=1)
    assert following['source_sequence'] == first['source_sequence']
    assert following['items'][0]['ref'] != first['items'][0]['ref']


def test_reference_digest_is_stable_across_support_edge_scan_orders(completed, monkeypatch):
    host, store, *_ = completed
    bases = [store.record_knowledge({'kind': 'claim', 'statement': f'basis {i}',
        'evidence_refs': ['pub/P-001#data']}, f'basis-{i}') for i in range(2)]
    user = store.record_knowledge({'kind': 'claim', 'statement': 'uses both bases',
        'evidence_refs': [v['ref'] for v in bases]}, 'both-bases')
    for i, base in enumerate(bases):
        store.revise_knowledge({'ref': base['ref'], 'expected_revision': 1, 'changes': {'statement': 'new wording'},
            'reason': 'reword', 'change_kind': 'reword', 'affected_scope_mode': 'none'}, f'reword-basis-{i}')
    first = store.reference_chunk(user['ref'], limit=64)
    original = store._connection

    @contextmanager
    def reverse_unordered_scans():
        with original() as db:
            db.execute('PRAGMA reverse_unordered_selects=ON')
            yield db

    monkeypatch.setattr(store, '_connection', reverse_unordered_scans)
    following = store.reference_chunk(user['ref'], offset=64, limit=64, expected_digest=first['content_sha256'])
    assert following['content_sha256'] == first['content_sha256']
