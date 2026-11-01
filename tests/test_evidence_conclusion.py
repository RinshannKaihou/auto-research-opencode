"""Evidence decisions preserve unknowns and distinguish review work from coverage."""

import json
import hashlib
import base64

import pytest

from auto_research import frozen_refs
from auto_research.errors import ConflictError
from test_context_policy import completed
from test_opencode_host import Host
from test_p0_contracts import node
from test_r2_runtime import fixture, knowledge, obligations, report, link


def fields(core, outcome='answered'):
    return {'core_claim_refs': core, 'outcome': outcome,
            'gaps': [] if outcome == 'answered' else ['Further evidence is needed'],
            'risk_judgments': [], 'review_requirements': [],
            'exploration_stop': {'reason_kind': 'goal_met', 'reason': 'Current task is complete'}}


def test_empty_core_cannot_create_answered_but_partial_is_allowed(tmp_path):
    host = Host(tmp_path)
    host.open()
    store = host.service.stores[str(host.root)]
    store.conclusion_contract('opencode', 'ses_main', 'evidence-v1', 'adopt')
    first = host.call('query', collection='evidence', refs=[])
    snapshot = host.service.evidence.get(store, first['snapshot_id'])
    with pytest.raises(ConflictError, match='Core evidence'):
        store.prepare_conclusion('opencode', 'ses_main', fields([]), snapshot, 'answered')
    draft = store.prepare_conclusion('opencode', 'ses_main', fields([], 'partial'), snapshot, 'partial')
    assert draft['outcome'] == 'partial' and draft['evidence_closure']['status'] == 'unknown'
    assert draft['review_coverage']['status'] == 'unreviewed'
    assert 'conclusion draft' in host.error('conclude', fields={})


def test_existing_core_can_be_resolved_without_inventing_review(completed):
    host, store, _, _, claim, _ = completed
    store.conclusion_contract('opencode', 'ses_main', 'evidence-v1', 'adopt-evidence')
    page = host.call('query', collection='evidence', refs=[claim['ref']])
    snapshot = host.service.evidence.get(store, page['snapshot_id'])
    draft = store.prepare_conclusion('opencode', 'ses_main', fields([claim['ref']]), snapshot, 'prepare')
    assert draft['outcome'] == 'answered'
    assert draft['evidence_closure']['status'] == 'resolved'
    assert draft['review_coverage']['status'] == 'unreviewed'


def test_conclusion_scope_merges_with_explicit_review_scope_and_records_narrowing(completed):
    host, store, _, _, claim, _ = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    other = knowledge(host, 'Another explicitly reviewed question')
    host.call('review_scope_set', refs=[other['ref']], reason='keep separate project review')
    host.call('conclusion_contract', contract='evidence-v1')
    page = host.call('query', collection='evidence', refs=[claim['ref']])
    first = host.call('conclusion_prepare', model_call=True,
                      fields={**fields([claim['ref']]), 'snapshot_id':page['snapshot_id']})
    assert first['outcome'] == 'answered'
    rows = obligations(store)
    assert any(item['target_ref'] == claim['ref'] and item['state'] != 'out_of_scope' for item in rows)
    assert any(item['target_ref'] == other['ref'] and item['state'] != 'out_of_scope' for item in rows)
    page = host.call('query', collection='evidence', refs=[])
    narrowed = {**fields([], 'partial'), 'snapshot_id':page['snapshot_id']}
    assert 'scope_change.reason' in host.error('conclusion_prepare', model_call=True, fields=narrowed)
    narrowed['scope_change'] = {'reason':'The first result no longer answers the revised question',
                                'excluded_impact':'Its evidence is left outside this partial answer'}
    host.call('conclusion_prepare', model_call=True, fields=narrowed)
    with store._read() as db:
        config = store._project(db)['config']
        assert config['r2_review_scope']['refs'] == [other['ref']]
        assert config['conclusion_review_scope']['refs'] == []
        events = list(db.execute("SELECT data FROM events WHERE kind='conclusion.scope_changed' ORDER BY event_id"))
        assert json.loads(events[-1]['data'])['removed'] == [claim['ref']]


def test_older_snapshot_cannot_prepare_after_claim_changes(completed):
    host, store, _, _, claim, _ = completed
    store.conclusion_contract('opencode', 'ses_main', 'evidence-v1', 'adopt-evidence')
    page = host.call('query', collection='evidence', refs=[claim['ref']])
    snapshot = host.service.evidence.get(store, page['snapshot_id'])
    store.revise_knowledge({'ref': claim['ref'], 'expected_revision': 1, 'changes': {'statement': 'New interpretation'},
                           'reason': 'new finding', 'affected_scope_mode': 'none', 'change_kind': 'reword'}, 'change')
    with pytest.raises(ConflictError, match='conclusion_basis_changed') as error:
        store.prepare_conclusion('opencode', 'ses_main', fields([claim['ref']]), snapshot, 'stale-prepare')
    assert claim['ref'] in error.value.details['changed_refs']
    assert claim['ref'] in str(error.value)


def test_typed_review_uses_observed_model_not_claimed_identity(tmp_path):
    host, store = fixture(tmp_path)
    claim = knowledge(host, 'Original claim')
    host.call('review_scope_set', refs=[claim['ref']], reason='synthesis')
    row = next(item for item in obligations(store) if item['target_ref'] == claim['ref'])
    host.call('runtime_native_event', fields={'phase': 'started', 'source_key': 'step:ses_main:review-message',
              'model': {'providerID': 'provider', 'id': 'actual-model', 'variant': 'small'}})
    publication = host.call('publish',status='complete',summary='review',gaps=[],knowledge_refs=[],
        items=[{'item_id':'review','kind':'text','content':{'text':'Review'}}],
        operation_id='ses_main:review-message:review-call:research_publish')
    source = f"pub/{publication['publication_id']}#review"
    link(host, row, source, review_type='citation_scope', limitations=[])
    with store._read() as db:
        assessment = db.execute('SELECT * FROM review_assessments').fetchone()
        identity = json.loads(assessment['identity'])
        assert assessment['target_ref'] == claim['ref']
        assert identity['model'] == {'provider': 'provider', 'model': 'actual-model', 'variant': 'small'}


def test_copied_inline_source_cannot_be_typed_review_of_its_claim(tmp_path):
    host, store = fixture(tmp_path)
    def publication():
        item = host.call('publish', status='complete', summary='material', gaps=[], knowledge_refs=[],
                         items=[{'item_id':'item','kind':'text','content':{'text':'Measured result'}}])
        return f"pub/{item['publication_id']}#item"
    source, copied_report = publication(), publication()
    claim = host.call('memory_write', action='record', fields={'kind':'claim', 'statement':'Measured result',
        'visibility':'project', 'evidence_refs':[source]})
    host.call('review_scope_set', refs=[claim['ref']], reason='review the claim')
    row = next(item for item in obligations(store) if item['target_ref'] == claim['ref'])
    error = host.error('relate', contract_version='r2-v1', source_ref=copied_report, target_ref=claim['ref'],
        obligation_id=row['obligation_id'], label='reviews', note='same content', result='supports',
        complete=True, review_type='citation_scope')
    assert 'already a source' in error
    assert next(item for item in obligations(store) if item['obligation_id'] == row['obligation_id'])['state'] == 'pending'


def test_review_cannot_hide_inside_a_directory_alias(tmp_path):
    host, store = fixture(tmp_path)
    bundle = host.root / 'bundle'
    bundle.mkdir()
    (bundle / 'review.txt').write_text('The report within the reviewed directory', encoding='utf-8')
    def publish():
        item = host.call('publish', status='complete', summary='bundle', gaps=[], knowledge_refs=[],
            items=[{'item_id':'bundle','kind':'directory','source_path':'bundle'}])
        return f"pub/{item['publication_id']}#bundle"
    target, alias = publish(), publish()
    host.call('review_scope_set', refs=[target], reason='review the directory')
    row = next(item for item in obligations(store) if item['target_ref'] == target)
    error = host.error('relate', contract_version='r2-v1', source_ref=alias + '/review.txt', target_ref=target,
        obligation_id=row['obligation_id'], label='reviews', note='same directory', result='supports',
        complete=True, review_type='citation_scope')
    assert 'same frozen object' in error


def prepare_and_publish(host, claim):
    host.call('conclusion_contract', contract='evidence-v1')
    page = host.call('query', collection='evidence', refs=[claim['ref']])
    draft = host.call('conclusion_prepare', model_call=True, fields={**fields([claim['ref']]), 'snapshot_id': page['snapshot_id']})
    publication = host.call('publish', model_call=True, status='complete', summary='Final report', gaps=[],
        items=[{'item_id': 'report', 'kind': 'text', 'content': {'text': 'Measured under the stated conditions.'}}],
        knowledge_refs=[claim['ref']], draft_id=draft['draft_id'], report_item_id='report')
    return draft, publication


def deliver_draft(host, draft_id, instance='plugin-1'):
    page = host.call('query', collection='evidence', draft_id=draft_id, limit=100)
    pages = []
    while True:
        end = page['end']
        pages.append({'snapshot_id': page['snapshot_id'], 'draft_id': draft_id, 'part': page.get('part', 'manifest'), 'offset': page['offset'], 'end': end,
                      'manifest_digest': page['manifest_digest'], 'chunk_digest': hashlib.sha256(page['chunk'].encode()).hexdigest()})
        if not page['cursor']:
            break
        page = host.call('query', collection='evidence', cursor=page['cursor'], limit=100)
    return host.call('evidence_observed', pages=pages, plugin_instance_id=instance)['receipt_ids']


def deliver_material(host, ref, limit=16384):
    page = host.call('query', collection='evidence', material_ref=ref, limit=limit)
    pages, chunks = [], []
    while True:
        chunks.append(page['chunk'])
        pages.append({'snapshot_id': page['snapshot_id'], 'part': 'material', 'material_ref': ref,
                      'model_message_id': f'model-step-{len(pages)}', 'offset': page['offset'], 'end': page['end'],
                      'manifest_digest': page['manifest_digest'], 'chunk_digest': hashlib.sha256(page['chunk'].encode()).hexdigest()})
        if not page['cursor']:
            break
        page = host.call('query', collection='evidence', cursor=page['cursor'], limit=limit)
    for i in range(0, len(pages), 100):
        result = host.call('evidence_observed', pages=pages[i:i+100], plugin_instance_id='plugin-1')
        assert not result['rejected']
    return ''.join(chunks)


def test_evidence_conclusion_uses_bound_report_and_delivered_draft(completed):
    host, _, _, _, claim, _ = completed
    draft, publication = prepare_and_publish(host, claim)
    final_ref = f"pub/{publication['publication_id']}#report"
    assert publication['appendix_ref'] == f"pub/{publication['publication_id']}#evidence-appendix"
    appendix = host.call('reference_content', ref=publication['appendix_ref'])
    assert '核心主张与支撑' in base64.b64decode(appendix['chunk']).decode('utf-8')
    args = {'draft_id': draft['draft_id'], 'final_ref': final_ref, 'summary': 'Answered within the measured scope'}
    assert 'Complete evidence' in host.error('conclude', fields=args, plugin_instance_id='plugin-1')
    receipts = deliver_draft(host, draft['draft_id'])
    assert 'Complete evidence' in host.error('conclude', fields=args, delivery_receipts=receipts[:-1], plugin_instance_id='plugin-1')
    assert 'accepts only' in host.error('conclude', fields={**args,'outcome':'partial'},
        delivery_receipts=receipts, plugin_instance_id='plugin-1')
    value = host.call('conclude', fields=args, delivery_receipts=receipts, plugin_instance_id='plugin-1', operation_id='final')
    assert value['outcome'] == 'answered'
    assert value['evidence']['evidence_closure']['status'] == 'resolved'
    assert value['evidence']['review_coverage']['status'] == 'unreviewed'
    assert value['evidence']['appendix_ref'] == publication['appendix_ref']
    assert host.call('conclude', fields=args, delivery_receipts=receipts, plugin_instance_id='plugin-1', operation_id='final') == value
    assert host.call('status')['workflow']['conclusion']['evidence'] == value['evidence']


def test_evidence_final_report_cannot_be_a_directory(completed):
    host, _, _, _, claim, _ = completed
    host.call('conclusion_contract', contract='evidence-v1')
    page = host.call('query', collection='evidence', refs=[claim['ref']])
    draft = host.call('conclusion_prepare', model_call=True, fields={**fields([claim['ref']]),'snapshot_id':page['snapshot_id']})
    directory = host.root / 'report-directory'
    directory.mkdir()
    (directory / 'report.md').write_text('Report text', encoding='utf-8')
    error = host.error('publish', model_call=True, status='complete', summary='report', gaps=[], knowledge_refs=[claim['ref']],
        items=[{'item_id':'report','kind':'report','source_path':'report-directory'}],
        draft_id=draft['draft_id'], report_item_id='report')
    assert 'not a directory' in error


def test_changed_evidence_cannot_commit_an_old_draft(completed):
    host, store, _, _, claim, _ = completed
    draft, publication = prepare_and_publish(host, claim)
    receipts = deliver_draft(host, draft['draft_id'])
    store.revise_knowledge({'ref': claim['ref'], 'expected_revision': 1, 'changes': {'status': 'retracted'},
        'reason': 'withdrawn', 'change_kind': 'retract', 'affected_scope_mode': 'versions', 'affected_scope': [claim['ref']]}, 'withdraw-after-draft')
    args = {'draft_id': draft['draft_id'], 'final_ref': f"pub/{publication['publication_id']}#report", 'summary': 'stale'}
    assert 'conclusion_basis_changed' in host.error('conclude', fields=args, delivery_receipts=receipts, plugin_instance_id='plugin-1')
    assert host.call('status')['workflow']['conclusion'] is None


def test_noncore_missing_material_allows_a_qualified_answer(completed):
    host, _, _, _, claim, _ = completed
    host.call('conclusion_contract', contract='evidence-v1')
    page = host.call('query', collection='evidence', refs=[claim['ref'], 'knowledge/K-404@1'])
    snapshot = host.service.evidence.get(host.service.stores[str(host.root)], page['snapshot_id'])
    limit = next(item for item in snapshot['view']['limitations'] if item['reason'] == 'knowledge_missing')
    declaration = {**fields([claim['ref']]), 'snapshot_id': page['snapshot_id'], 'risk_judgments': [{
        'risk_id': limit['risk_id'], 'core_impact': 'no', 'disposition': 'unresolved',
        'reason': 'The missing item only concerns a separate mechanism', 'evidence_refs': [claim['ref']],
        'gap': 'Mechanism remains unverified'}]}
    draft = host.call('conclusion_prepare', model_call=True, fields=declaration)
    assert draft['outcome'] == 'answered'
    assert draft['evidence_closure']['status'] == 'qualified'
    assert draft['review_coverage']['status'] == 'unreviewed'


def test_review_coverage_uses_exact_target_and_observed_identity(completed):
    host, store, _, _, claim, _ = completed
    host.call('review_scope_set', refs=[claim['ref']], reason='review the synthesis')
    row = next(item for item in obligations(store) if item['target_ref'] == claim['ref'])
    host.call('runtime_native_event', fields={'phase':'started','source_key':'step:ses_main:review-message',
        'model':{'providerID':'provider','id':'model','variant':'v'}})
    publication = host.call('publish', model_call=True, status='complete', summary='review', gaps=[], knowledge_refs=[],
        items=[{'item_id':'review','kind':'text','content':{'result':'Review of references'}}],
        operation_id='ses_main:review-message:review-call:research_publish')
    source = f"pub/{publication['publication_id']}#review"
    link(host,row,source,review_type='citation_scope',result='supports',limitations=[])
    host.call('conclusion_contract', contract='evidence-v1')
    page = host.call('query', collection='evidence', refs=[claim['ref']])
    declaration = {**fields([claim['ref']]), 'snapshot_id': page['snapshot_id'],
        'review_requirements': [{'target_ref':claim['ref'],'types':['citation_scope'],'independent':False}]}
    draft = host.call('conclusion_prepare', model_call=True, fields=declaration)
    assert draft['review_coverage']['status'] == 'reviewed'
    assert draft['review_coverage']['coverage'][0]['target_ref'] == claim['ref']


def test_limited_review_remains_visible_and_covers_its_declared_type(completed):
    host, store, _, _, claim, _ = completed
    host.call('review_scope_set', refs=[claim['ref']], reason='review the synthesis')
    row = next(item for item in obligations(store) if item['target_ref'] == claim['ref'])
    host.call('runtime_native_event', fields={'phase':'started','source_key':'step:ses_main:limited-review',
        'model':{'providerID':'provider','id':'model'}})
    publication = host.call('publish', model_call=True, status='complete', summary='limited review', gaps=[], knowledge_refs=[],
        items=[{'item_id':'review','kind':'text','content':{'text':'Checked citation scope only'}}],
        operation_id='ses_main:limited-review:call:research_publish')
    link(host, row, f"pub/{publication['publication_id']}#review", review_type='citation_scope',
         result='supports', limitations=['Did not run the experiment'])
    host.call('conclusion_contract', contract='evidence-v1')
    page = host.call('query', collection='evidence', refs=[claim['ref']])
    declaration = {**fields([claim['ref']]), 'snapshot_id':page['snapshot_id'],
        'review_requirements':[{'target_ref':claim['ref'],'types':['citation_scope'],'independent':False}]}
    draft = host.call('conclusion_prepare', model_call=True, fields=declaration)
    coverage = draft['review_coverage']
    assert coverage['status'] == 'reviewed'
    assert coverage['coverage'][0]['reviews'][0]['limitations'] == ['Did not run the experiment']
    assert 'Did not run the experiment' in coverage['limitations']


def test_previous_generation_review_needs_explicit_acceptance(completed):
    host, store, _, _, claim, _ = completed
    host.call('review_scope_set', refs=[claim['ref']], reason='review exact claim')
    row = next(item for item in obligations(store) if item['target_ref'] == claim['ref'])
    host.call('runtime_native_event', fields={'phase':'started','source_key':'step:ses_main:previous-review',
        'model':{'providerID':'provider','id':'model'}})
    publication = host.call('publish', model_call=True, status='complete', summary='review', gaps=[], knowledge_refs=[],
        items=[{'item_id':'review','kind':'text','content':{'text':'Checked the exact claim'}}],
        operation_id='ses_main:previous-review:call:research_publish')
    linked = link(host, row, f"pub/{publication['publication_id']}#review", review_type='citation_scope',
                  result='supports', limitations=[])
    event_id = next(item['event_id'] for item in linked['events'] if item['action'] == 'completed')
    host.call('workflow', action='run', fields={'state':'running','new_generation':True})
    host.call('review_scope_set', refs=[claim['ref']], reason='revisit exact claim')
    host.call('conclusion_contract', contract='evidence-v1')
    def prepare():
        page = host.call('query', collection='evidence', refs=[claim['ref']])
        return host.call('conclusion_prepare', model_call=True, fields={**fields([claim['ref']]),
            'snapshot_id':page['snapshot_id'], 'review_requirements':[
                {'target_ref':claim['ref'],'types':['citation_scope'],'independent':False}]})
    assert prepare()['review_coverage']['status'] == 'unknown'
    current = max((item for item in obligations(store) if item['target_ref'] == claim['ref']), key=lambda item:item['generation'])
    host.call('review_exception', fields={'obligation_id':current['obligation_id'], 'action':'accept_previous',
        'report_link':event_id, 'reason':'The exact reviewed version and conditions still apply', 'evidence_refs':[]})
    accepted = prepare()['review_coverage']
    assert accepted['status'] == 'reviewed'
    assert accepted['coverage'][0]['review_events'] == [event_id]


def test_reopened_review_does_not_count_until_explicitly_reaccepted(completed):
    host, store, _, _, claim, _ = completed
    host.call('review_scope_set', refs=[claim['ref']], reason='review exact claim')
    row = next(item for item in obligations(store) if item['target_ref'] == claim['ref'])
    host.call('runtime_native_event', fields={'phase':'started','source_key':'step:ses_main:reopened-review',
        'model':{'providerID':'provider','id':'model'}})
    publication = host.call('publish', model_call=True, status='complete', summary='review', gaps=[], knowledge_refs=[],
        items=[{'item_id':'review','kind':'text','content':{'text':'Checked the exact claim'}}],
        operation_id='ses_main:reopened-review:call:research_publish')
    linked = link(host, row, f"pub/{publication['publication_id']}#review", review_type='citation_scope',
                  result='supports', limitations=[])
    event_id = next(item['event_id'] for item in linked['events'] if item['action'] == 'completed')
    host.call('review_exception', fields={'obligation_id':row['obligation_id'], 'action':'reopen',
        'reason':'New concern', 'evidence_refs':[]})
    host.call('conclusion_contract', contract='evidence-v1')
    def coverage():
        page = host.call('query', collection='evidence', refs=[claim['ref']])
        draft = host.call('conclusion_prepare', model_call=True, fields={**fields([claim['ref']]),
            'snapshot_id':page['snapshot_id'], 'review_requirements':[
                {'target_ref':claim['ref'],'types':['citation_scope'],'independent':False}]})
        return draft['review_coverage']['status']
    assert coverage() == 'unknown'
    host.call('review_exception', fields={'obligation_id':row['obligation_id'], 'action':'accept_previous',
        'report_link':event_id, 'reason':'Concern checked against the existing report', 'evidence_refs':[]})
    assert coverage() == 'reviewed'


def test_unrelated_old_challenge_remains_recorded_but_does_not_block_current_core(completed):
    host, store, _, _, claim, _ = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    other = knowledge(host, 'An unrelated earlier question')
    host.call('review_scope_set', refs=[other['ref']], reason='earlier review')
    row = next(item for item in obligations(store) if item['target_ref'] == other['ref'])
    link(host, row, report(host), result='challenges')
    host.call('review_scope_set', refs=[], reason='current answer has another scope')
    host.call('conclusion_contract', contract='evidence-v1')
    page = host.call('query', collection='evidence', refs=[claim['ref']])
    snapshot = host.service.evidence.get(store, page['snapshot_id'])
    assert not any(risk.get('target') == other['ref'] for risk in snapshot['view']['risks'])
    draft = host.call('conclusion_prepare', model_call=True, fields={**fields([claim['ref']]),'snapshot_id':page['snapshot_id']})
    assert draft['evidence_closure']['status'] == 'resolved'
    with store._read() as db:
        from auto_research.review_lifecycle import unresolved_challenges
        assert any(item['target_ref'] == other['ref'] for item in unresolved_challenges(db))


def test_challenge_to_current_core_blocks_an_unqualified_answer(completed):
    host, store, _, _, claim, _ = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    host.call('review_scope_set', refs=[claim['ref']], reason='review the synthesis')
    row = next(item for item in obligations(store) if item['target_ref'] == claim['ref'])
    link(host, row, report(host), result='challenges')
    host.call('conclusion_contract', contract='evidence-v1')
    page = host.call('query', collection='evidence', refs=[claim['ref']])
    assert 'Core evidence' in host.error('conclusion_prepare', model_call=True,
        fields={**fields([claim['ref']]),'snapshot_id':page['snapshot_id']})


def test_grouping_does_not_allow_a_review_challenge_to_be_marked_handled(completed):
    host, store, _, _, claim, _ = completed
    host.call('focus', node_id=None, role='planner', mode='manual')
    host.call('review_scope_set', refs=[claim['ref']], reason='review the synthesis')
    row = next(item for item in obligations(store) if item['target_ref'] == claim['ref'])
    link(host, row, report(host), result='challenges')
    host.call('conclusion_contract', contract='evidence-v1')
    page = host.call('query', collection='evidence', refs=[claim['ref'], 'knowledge/K-404@1'])
    snapshot = host.service.evidence.get(store, page['snapshot_id'])
    challenge = next(item['risk_id'] for item in snapshot['view']['risks'] if item['kind'] == 'review_challenge')
    missing = next(item['risk_id'] for item in snapshot['view']['limitations'] if item['reason'] == 'knowledge_missing')
    group = {'risk_ids':[missing,challenge], 'core_impact':'no', 'disposition':'handled',
             'reason':'Attempt to classify both risks together', 'evidence_refs':[claim['ref']]}
    assert 'challenges need their exact disposition' in host.error('conclusion_prepare', model_call=True,
        fields={**fields([claim['ref']]),'snapshot_id':page['snapshot_id'], 'risk_judgments':[group]})


def test_execution_review_requires_host_observed_executable_work(completed):
    host, store, _, _, claim, _ = completed
    host.call('workflow', action='run', fields={'state':'running'})
    _, sid, _ = node(host, index=9)
    host.call('review_scope_set', refs=[claim['ref']], reason='check the computation')
    row = next(item for item in obligations(store) if item['target_ref'] == claim['ref'] and item['scope_owner'] == 'project')
    host.call('runtime_native_event', session=sid, fields={'phase':'started',
        'source_key':f'step:{sid}:review-message', 'model':{'providerID':'provider','id':'model'}})
    def publish_review(text, operation):
        publication = host.call('publish', session=sid, model_call=True, status='complete', summary='review', gaps=[],
            knowledge_refs=[], items=[{'item_id':'review','kind':'text','content':{'text':text}}],
            operation_id=f'{sid}:review-message:{operation}:research_publish')
        return f"pub/{publication['publication_id']}#review"
    link(host, row, publish_review('Claimed a recomputation', 'first'), review_type='recompute', result='supports')
    host.call('conclusion_contract', contract='evidence-v1')
    def coverage():
        page = host.call('query', collection='evidence', refs=[claim['ref']])
        draft = host.call('conclusion_prepare', model_call=True, fields={**fields([claim['ref']]),
            'snapshot_id':page['snapshot_id'], 'review_requirements':[
                {'target_ref':claim['ref'],'types':['recompute'],'independent':False}]})
        return draft['review_coverage']
    assert coverage()['status'] == 'unknown'
    assert host.call('runtime_tool_observed', session=sid, tool='shell', call_id='execute-1')['recorded']
    link(host, row, publish_review('Recomputed with the recorded execution', 'second'),
         review_type='recompute', result='supports')
    checked = coverage()
    assert checked['status'] == 'reviewed'
    assert checked['coverage'][0]['reviews'][0]['identity']['observed_execution_tools'] == ['shell']


@pytest.mark.parametrize('assigned_related', [True, False])
def test_blind_review_independence_uses_actual_assigned_inputs(tmp_path, assigned_related):
    host = Host(tmp_path)
    host.open()
    host.call('focus', node_id=None, role='planner', mode='manual')
    (host.root / 'source.txt').write_text('Measured value: 7', encoding='utf-8')
    (host.root / 'other.txt').write_text('Unrelated reading', encoding='utf-8')
    publication = host.call('publish', status='complete', summary='inputs', gaps=[], knowledge_refs=[], items=[
        {'item_id':'source','kind':'text','source_path':'source.txt'},
        {'item_id':'other','kind':'text','source_path':'other.txt'}])
    source = f"pub/{publication['publication_id']}#source"
    other = f"pub/{publication['publication_id']}#other"
    host.call('runtime_native_event', fields={'phase':'started','source_key':'step:ses_main:author-message',
        'model':{'providerID':'provider','id':'author-model'}})
    claim = host.call('memory_write', model_call=True, action='record', fields={'kind':'claim',
        'statement':'Measured value is 7','visibility':'project','evidence_refs':[source]},
        operation_id='ses_main:author-message:record:research_record')
    host.call('review_scope_set', refs=[claim['ref']], reason='review this claim')
    store = host.service.stores[str(host.root)]
    obligation = next(item for item in obligations(store) if item['target_ref'] == claim['ref'])
    task = host.call('specialist_create', fields={'purpose':'review','label':'blind review',
        'prompt':'Review the assigned material','inputs':[source if assigned_related else other],'context_mode':'blind'})
    host.call('specialist_bind_child', task_id=task['task_id'], child_session_id='ses_blind', cwd=str(host.root))
    host.call('runtime_native_event', session='ses_blind', fields={'phase':'started',
        'source_key':'step:ses_blind:review-message','model':{'providerID':'provider','id':'review-model'}})
    result_dir = host.root / '.research' / 'specialist-results'
    result_dir.mkdir(exist_ok=True)
    result_text = '{"review":"Checked the assigned material"}'
    (result_dir / f"{task['task_id']}.json").write_text(result_text, encoding='utf-8')
    artifact = host.call('specialist_result_import', task_id=task['task_id'])
    host.call('specialist_finish', fields={'task_id':task['task_id'],'state':'completed',
        'exit_verified':True,'result':{'artifact':artifact}})
    (host.root / 'review.json').write_text(result_text, encoding='utf-8')
    review = host.call('publish', status='complete', summary='blind review', gaps=[], knowledge_refs=[],
        items=[{'item_id':'review','kind':'text','source_path':'review.json'}])
    link(host, obligation, f"pub/{review['publication_id']}#review", review_type='citation_scope',
         specialist_task_id=task['task_id'], result='supports')
    host.call('conclusion_contract', contract='evidence-v1')
    deliver_material(host, source)
    page = host.call('query', collection='evidence', refs=[claim['ref']])
    draft = host.call('conclusion_prepare', model_call=True, fields={**fields([claim['ref']]),
        'snapshot_id':page['snapshot_id'], 'review_requirements':[
            {'target_ref':claim['ref'],'types':['citation_scope'],'independent':True}]})
    coverage = draft['review_coverage']
    assert coverage['status'] == ('reviewed' if assigned_related else 'unknown')
    with store._read() as db:
        identity = json.loads(db.execute('SELECT identity FROM review_assessments').fetchone()[0])
        assert identity['model']['model'] == 'review-model'
        assert identity['author_model']['model'] == 'author-model'
        assert identity['same_model_as_author'] is False
        assert identity['assigned_inputs'] == [source if assigned_related else other]


def test_identical_report_content_cannot_be_its_own_answered_evidence(completed):
    host, _, _, _, claim, _ = completed
    host.call('conclusion_contract', contract='evidence-v1')
    page = host.call('query', collection='evidence', refs=[claim['ref']])
    draft = host.call('conclusion_prepare', model_call=True, fields={**fields([claim['ref']]), 'snapshot_id':page['snapshot_id']})
    publication = host.call('publish', model_call=True, status='complete', summary='copy', gaps=[],
        items=[{'item_id':'report','kind':'text','content':{'measured':7}}], knowledge_refs=[claim['ref']],
        draft_id=draft['draft_id'], report_item_id='report')
    receipts = deliver_draft(host,draft['draft_id'])
    args = {'draft_id':draft['draft_id'],'final_ref':f"pub/{publication['publication_id']}#report",'summary':'copied source'}
    assert 'cannot be treated as evidence' in host.error('conclude',fields=args,delivery_receipts=receipts,plugin_instance_id='plugin-1')


def test_core_file_must_be_delivered_and_verified_again_at_conclusion(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('focus', node_id=None, role='planner', mode='manual')
    (host.root / 'measurement.txt').write_text('Measured value: 7', encoding='utf-8')
    source = host.call('publish', status='complete', summary='measurement', gaps=[], knowledge_refs=[],
        items=[{'item_id':'data','kind':'text','source_path':'measurement.txt'}])
    source_ref = f"pub/{source['publication_id']}#data"
    claim = host.call('memory_write', action='record', fields={'kind':'claim','statement':'Measured value is 7',
        'evidence_refs':[source_ref],'visibility':'project'})
    host.call('conclusion_contract',contract='evidence-v1')
    missing = host.call('query',collection='evidence',refs=[claim['ref']])
    declaration = {**fields([claim['ref']]),'snapshot_id':missing['snapshot_id']}
    assert 'Core evidence' in host.error('conclusion_prepare',model_call=True,fields=declaration)
    deliver_material(host, source_ref)
    page = host.call('query',collection='evidence',refs=[claim['ref']])
    draft = host.call('conclusion_prepare',model_call=True,fields={**declaration,'snapshot_id':page['snapshot_id']})
    publication = host.call('publish',status='complete',summary='final',gaps=[],knowledge_refs=[claim['ref']],
        items=[{'item_id':'report','kind':'text','content':{'text':'Result is 7'}}],
        draft_id=draft['draft_id'],report_item_id='report')
    receipts = deliver_draft(host,draft['draft_id'])
    store = host.service.stores[str(host.root)]
    with store._read() as db:
        _, path = frozen_refs.open(db,host.root,source_ref)
    path.chmod(0o600)
    path.write_text('tampered',encoding='utf-8')
    args={'draft_id':draft['draft_id'],'final_ref':f"pub/{publication['publication_id']}#report",'summary':'answer'}
    assert 'conclusion_basis_changed' in host.error('conclude',fields=args,delivery_receipts=receipts,plugin_instance_id='plugin-1')


def test_receipt_rejects_a_changed_or_redacted_page(tmp_path):
    host = Host(tmp_path)
    host.open()
    page = host.call('query',collection='evidence',refs=[])
    end = page['cursor']['offset'] if page['cursor'] else page['total_chars']
    altered = {'snapshot_id':page['snapshot_id'],'offset':page['offset'],'end':end,
               'manifest_digest':page['manifest_digest'],'chunk_digest':hashlib.sha256(b'changed').hexdigest()}
    result = host.call('evidence_observed',pages=[altered],plugin_instance_id='plugin-1')
    assert result['receipt_ids'] == [] and result['rejected'][0]['reason'] == 'digest_mismatch'
