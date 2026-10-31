import pytest

from auto_research import frozen_refs
from auto_research.errors import ValidationError
from test_r2_runtime import fixture, knowledge, report, link, obligations
from test_p0_contracts import node


def challenges(store):
    return [r for r in store.context_page('critical_now')['items'] if r['kind']=='review_challenge']


@pytest.mark.parametrize('transition',['scope_exit','stop','conclude','generation','node_finish'])
@pytest.mark.parametrize('complete',[True,False])
def test_challenge_survives_task_scope_and_run_lifecycle(tmp_path,transition,complete):
    host,store=fixture(tmp_path)
    sid='ses_main'
    if transition=='node_finish':
        host.call('workflow',action='run',fields={'state':'running'})
        _,sid,_=node(host)
        note=host.call('note',session=sid,body='measurement',kind='condition')
        claim=host.call('memory_write',session=sid,action='record',fields={'kind':'claim','statement':'claim','evidence_refs':[note['note_id']]})
    else:
        claim=knowledge(host,'claim')
        host.call('review_scope_set',refs=[claim['ref']],reason='synthesis',model_call=True)
    row=next(r for r in obligations(store) if r['target_ref']==claim['ref'])
    ref=report(host)
    link(host,row,ref,session=sid,result='challenges',complete=complete)
    event=challenges(store)[0]['risk']['review_event']
    if transition=='scope_exit': host.call('review_scope_set',refs=[],reason='leave')
    elif transition=='stop': host.call('workflow',action='run',fields={'state':'stopped'})
    elif transition=='generation': host.call('workflow',action='run',fields={'state':'running','new_generation':True})
    elif transition=='node_finish': host.call('finish',session=sid,state='finished',details={})
    else: host.call('conclude',fields={'summary':'report delivered','final_ref':ref,'outcome':'unresolved','gaps':['challenge'],
        'review':{'status':'partial','refs':[ref],'limitations':['not independent']}})
    assert challenges(store)[0]['risk']['review_event']==event
    assert next(r for r in obligations(store) if r['obligation_id']==row['obligation_id'])['state']=='out_of_scope'
    for policy in ['memory-v2','memory-r1-v1']:
        text=host.call('memory_context',profile='opencode-auto',policy=policy)['text']
        assert str(event) in text and ('review_challenges' in text or 'review_challenge' in text)
    fields={'review_event':event,'affected_version':claim['ref'],'disposition_kind':'retained_with_evidence',
            'reason':'bounded interpretation after counterexample','evidence_refs':[ref]}
    host.call('memory_write',action='dispose',fields=fields,operation_id='handle-challenge',contract_version='r2-v1')
    host.call('memory_write',action='dispose',fields=fields,operation_id='handle-challenge',contract_version='r2-v1')
    assert not challenges(store)
    assert any(e['action']=='challenge_disposed' for e in obligations(store)[0]['events'])


def test_disposition_is_exact_and_cannot_close_another_challenge(tmp_path):
    host,store=fixture(tmp_path)
    a,b=knowledge(host,'one'),knowledge(host,'two')
    host.call('review_scope_set',refs=[a['ref'],b['ref']],reason='both')
    ref=report(host)
    for row in obligations(store):link(host,row,ref,result='challenges')
    cursor=store.review_page('review_obligations',limit=1)['cursor']
    event=challenges(store)[0]
    fields={'review_event':event['risk']['review_event'],'affected_version':b['ref'] if event['target']==a['ref'] else a['ref'],
            'disposition_kind':'retracted','reason':'withdraw','evidence_refs':[ref]}
    assert 'exact review target' in host.error('memory_write',action='dispose',fields=fields)
    fields['affected_version']=event['target'];fields['evidence_refs']=[]
    assert 'requires evidence_refs' in host.error('memory_write',action='dispose',fields=fields)
    fields['evidence_refs']=[ref]
    host.call('memory_write',action='dispose',fields=fields)
    assert len(challenges(store))==1
    with pytest.raises(ValidationError,match='collection changed'):
        store.review_page('review_obligations',cursor=cursor)


def test_node_cannot_close_another_owners_review(tmp_path):
    host,store=fixture(tmp_path)
    claim=knowledge(host,'project claim')
    host.call('review_scope_set',refs=[claim['ref']],reason='project adoption')
    row=obligations(store)[0];ref=report(host)
    host.call('workflow',action='run',fields={'state':'running'})
    _,sid,_=node(host)
    assert 'another work owner' in host.error('relate',session=sid,contract_version='r2-v1',source_ref=ref,
        target_ref=claim['ref'],obligation_id=row['obligation_id'],label='reviews',note='wrong owner',result='supports',complete=True)


def test_missing_historical_object_does_not_block_control_and_remains_visible(tmp_path):
    host,store=fixture(tmp_path)
    (host.root/'input.md').write_text('source')
    pub=host.call('publish',status='complete',summary='input',items=[{'item_id':'input','kind':'report','source_path':'input.md'}])
    ref=f"pub/{pub['publication_id']}#input"
    host.call('review_scope_set',refs=[ref],reason='adopt')
    with store._read() as db: _,path=frozen_refs.open(db,host.root,ref)
    path.unlink()
    host.call('memory_write',action='checkpoint',fields={'state':{'next':'repair input'},'expected_revision':0})
    host.call('workflow',action='run',fields={'state':'paused'})
    assert any(r['kind']=='material_unavailable' for r in store.context_page('critical_now')['items'])
    assert 'object_missing' in host.error('review_scope_set',refs=[ref],reason='new adoption')


@pytest.mark.parametrize('policy',['memory-v2','memory-r1-v1'])
def test_complete_nontrivial_checkpoint_survives_context_budget(tmp_path,policy):
    host,store=fixture(tmp_path)
    state={'next':'synthesis','notes':'x'*2000}
    host.call('memory_write',action='checkpoint',fields={'state':state,'expected_revision':0})
    view=host.call('memory_context',profile='opencode-auto',policy=policy)
    assert view['checkpoint_snapshot']['head_revision']==1
    assert state['notes'] in view['text']
    small=host.call('memory_context',profile='opencode-auto',policy=policy,max_chars=2500)
    assert 'checkpoint_snapshot' not in small


def test_reads_never_inherit_the_previous_mutation_replay_flag(tmp_path):
    host,_=fixture(tmp_path)
    fields={'body':'same','kind':'progress','operation_id':'same'}
    assert host.raw('note',**fields)['replayed'] is False
    assert host.raw('note',**fields)['replayed'] is True
    response=host.raw('query',collection='notes',model_call=True)
    assert response['replayed'] is None
    assert response['invocation_context']['association_id']


@pytest.mark.parametrize('method',['review_scope_set','review_promote','review_exception','runtime_budget_set'])
def test_node_cannot_invoke_main_review_or_budget_control(tmp_path,method):
    host,_=fixture(tmp_path)
    host.call('workflow',action='run',fields={'state':'running'})
    _,sid,_=node(host)
    assert 'main' in host.error(method,session=sid,refs=[],reason='attempt',fields={},limits={})


def test_node_source_work_counts_as_progress_but_notes_and_noops_do_not(tmp_path):
    host,_=fixture(tmp_path)
    host.call('workflow',action='run',fields={'state':'running'})
    _,sid,_=node(host)
    host.call('workflow',action='run',fields={'state':'running'})
    owner=host.call('runtime_source_owner',session=sid)
    digest='0'*64
    for turn in range(4):
        snapshot={'owner':owner,'algorithm':'source-worker-v1','digest':digest}
        host.call('runtime_reserve',session=sid,source_snapshot=snapshot)
        (host.root/'experiment.py').write_text(f'assert {turn} >= 0\n')
        digest=f'{turn+1:064x}'
        result=host.call('runtime_turn_finished',session=sid,turn=str(turn),source_snapshot={**snapshot,'digest':digest})
        assert result['source_workspace_changed'] and result['idle_turns']==0
    snapshot={'owner':owner,'algorithm':'source-worker-v1','digest':digest}
    host.call('runtime_reserve',session=sid,source_snapshot=snapshot)
    (host.root/'lab-notes.md').write_text('bookkeeping only')
    result=host.call('runtime_turn_finished',session=sid,turn='no-code-change',source_snapshot=snapshot)
    assert not result['meaningful'] and result['idle_turns']==1
    assert host.call('runtime_turn_finished',session=sid,turn='no-code-change')['duplicate']


def test_cost_accepts_fractional_threshold_and_other_limits_remain_integer(tmp_path):
    host,_=fixture(tmp_path)
    host.call('runtime_budget_set',limits={'max_cost':0.25},reason='small experiment')
    assert host.call('runtime_budget_status')['limits']['max_cost']==0.25
    assert 'positive integers' in host.error('runtime_budget_set',limits={'max_session_turns':0.25},reason='invalid')


def test_missing_node_workspace_is_unknown_and_does_not_refresh_idle(tmp_path):
    host,_=fixture(tmp_path)
    host.call('workflow',action='run',fields={'state':'running'})
    _,sid,_=node(host)
    host.call('workflow',session=sid,action='session',fields={'cwd':str(host.root/'missing-workspace')})
    host.call('runtime_reserve',session=sid)
    result=host.call('runtime_turn_finished',session=sid,turn='no-workspace')
    assert result['source_workspace_changed'] is None
    assert not result['meaningful'] and result['idle_turns']==1


@pytest.mark.parametrize('offset',[-1,1.5,'1'])
def test_review_and_adoption_cursor_reject_invalid_offsets(tmp_path,offset):
    host,store=fixture(tmp_path)
    refs=[knowledge(host,str(i))['ref'] for i in range(3)]
    host.call('review_scope_set',refs=refs,reason='scope')
    page=store.review_page('review_obligations',limit=1)
    with pytest.raises(ValidationError,match='offset'):
        store.review_page('review_obligations',cursor={**page['cursor'],'offset':offset})
    for i in range(2):host.call('propose',question=str(i),why_now='now',plan='p',inputs=refs)
    page=store.adoptions(limit=1)
    with pytest.raises(ValidationError,match='offset'):
        store.adoptions(cursor={**page['cursor'],'offset':offset})
