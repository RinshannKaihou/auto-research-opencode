import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from auto_research.errors import ValidationError, ConflictError, BudgetError
from auto_research.native_store import NativeStore
from test_opencode_host import Host
from test_p0_contracts import node


def fixture(tmp_path):
    host=Host(tmp_path); host.open()
    host.call('focus',node_id=None,role='planner',mode='manual')
    return host,NativeStore(host.root)


def knowledge(host, text, **extra):
    note=host.call('note',body=text,kind='condition')
    return host.call('memory_write',action='record',fields={'kind':'claim','statement':text,
        'visibility':'project','evidence_refs':[note['note_id']],**extra})


def obligations(store):
    return store.review_page('review_obligations')['items']


def report(host):
    pub=host.call('publish',status='complete',summary='review',gaps=[],knowledge_refs=[],
        items=[{'item_id':'review','kind':'text','content':{'text':'Independent review: evidence remains inconclusive.'}}])
    return f"pub/{pub['publication_id']}#review"


def link(host,row,ref,**extra):
    fields=dict(contract_version='r2-v1',source_ref=ref,target_ref=row['target_ref'],
        obligation_id=row['obligation_id'],label='reviews',note='Checked exact version',
        result='inconclusive',complete=True)
    return host.call('relate',**{**fields,**extra})


def test_review_completion_is_exact_and_never_resolves_scientific_risk(tmp_path):
    host,store=fixture(tmp_path)
    basis=knowledge(host,'basis')
    claim=knowledge(host,'dependent',dependencies=[basis['ref']])
    host.call('review_scope_set',refs=[claim['ref']],reason='current synthesis')
    host.call('memory_write',action='revise',fields={'ref':basis['ref'],'preset':'correct_previous',
        'reason':'counterexample','changes':{'statement':'corrected'}})
    rows=obligations(store)
    impact=next(row for row in rows if row['target_ref']==claim['ref'] and row['change_id'])
    saved=link(host,impact,report(host))
    assert saved['state']=='completed' and saved['risk_disposition']=='unresolved'
    assert saved['coverage']=='not_assessed'
    assert all(row['state']=='pending' for row in obligations(store) if row['obligation_id']!=impact['obligation_id'])
    assert store.knowledge_risk([claim['ref']])['versions'][claim['ref']]['needs_action']


def test_reopen_survives_scope_exit_and_reentry(tmp_path):
    host,store=fixture(tmp_path)
    claim=knowledge(host,'claim')
    host.call('review_scope_set',refs=[claim['ref']],reason='include')
    row=obligations(store)[0]
    saved=link(host,row,report(host))
    host.call('review_scope_set',refs=[],reason='no longer current')
    assert obligations(store)[0]['state']=='out_of_scope'
    host.call('review_scope_set',refs=[claim['ref']],reason='current again')
    assert obligations(store)[0]['state']=='completed'
    host.call('review_exception',fields={'obligation_id':row['obligation_id'],'action':'reopen','reason':'new concern','evidence_refs':[]})
    host.call('review_scope_set',refs=[],reason='leave')
    host.call('review_scope_set',refs=[claim['ref']],reason='return')
    assert obligations(store)[0]['state']=='pending'
    original=next(e['event_id'] for e in saved['events'] if e['action']=='completed')
    host.call('review_exception',fields={'obligation_id':row['obligation_id'],'action':'accept_previous',
        'report_link':original,'reason':'explicitly reaccepted after checking','evidence_refs':[]})
    assert obligations(store)[0]['state']=='completed'


def test_partial_review_old_todo_and_self_reference_cannot_close_obligation(tmp_path):
    host,store=fixture(tmp_path)
    claim=knowledge(host,'claim')
    host.call('review_scope_set',refs=[claim['ref']],reason='review')
    row=obligations(store)[0]
    ref=report(host)
    result=host.call('relate',contract_version='r2-v1',source_ref=ref,target_ref=row['target_ref'],
        obligation_id=row['obligation_id'],label='reviews',note='not complete',result='challenges',complete=False)
    assert result['state']=='pending'
    todo=store.review_page('review_candidates')['items'][0]['legacy_todo_id']
    store.review_todo_state(todo,'completed','old-task')
    assert obligations(store)[0]['state']=='pending'
    assert 'separate frozen report' in host.error('relate',contract_version='r2-v1',source_ref=row['target_ref'],
        target_ref=row['target_ref'],obligation_id=row['obligation_id'],label='reviews',note='self',result='supports',complete=True)


def test_review_cursor_ignores_unrelated_events_but_binds_filters(tmp_path):
    host,store=fixture(tmp_path)
    refs=[knowledge(host,f'claim {n}')['ref'] for n in range(3)]
    host.call('review_scope_set',refs=refs,reason='review set')
    page=store.review_page('review_obligations',limit=1,state='pending')
    host.call('note',body='unrelated note',kind='progress')
    assert store.review_page('review_obligations',cursor=page['cursor'],limit=1)['items']
    with pytest.raises(ValidationError,match='filters changed'):
        store.review_page('review_obligations',cursor=page['cursor'],state='completed')
    link(host,page['items'][0],report(host))
    with pytest.raises(ValidationError,match='collection changed'):
        store.review_page('review_obligations',cursor=page['cursor'])


def test_turn_limits_are_durable_and_replays_do_not_spend_again(tmp_path):
    host,store=fixture(tmp_path)
    host.call('runtime_budget_set',limits={'max_session_turns':2},reason='test')
    host.call('workflow',action='run',fields={'state':'running'})
    first=host.call('runtime_reserve',operation_id='first')
    assert first['allowed']
    assert host.call('runtime_reserve',operation_id='first')==first
    assert host.call('runtime_reserve')['allowed']
    restarted=NativeStore(host.root)
    assert not restarted.runtime_reserve(host.host_id,'ses_main','third')['allowed']
    host.call('runtime_budget_set',limits={'max_session_turns':3},reason='explicit increase')
    host.call('workflow',action='session',fields={'pause_reason':None})  # explicit resume
    assert host.call('runtime_reserve')['allowed']
    assert host.call('runtime_budget_status')['turns']==3


def test_node_limit_is_atomic_across_two_callers(tmp_path):
    host,store=fixture(tmp_path)
    host.call('runtime_budget_set',limits={'max_total_nodes':1},reason='one slot')
    def propose(n):
        try: return store.propose(f'Q{n}','now','p',f'node-{n}')
        except BudgetError: return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows=list(pool.map(propose,[1,2]))
    assert sum(row is not None for row in rows)==1


def test_wall_deadline_survives_resume_and_cancels_queued_work(tmp_path):
    host,store=fixture(tmp_path)
    host.call('runtime_budget_set',limits={'max_wall_seconds':60},reason='test')
    host.call('workflow',action='run',fields={'state':'running'})
    with store._connection() as db:
        db.execute('UPDATE runtime_budgets SET started_at=?,deadline_at=?',
            ((datetime.now(timezone.utc)-timedelta(seconds=120)).isoformat(),
             (datetime.now(timezone.utc)-timedelta(seconds=60)).isoformat()))
    host.call('workflow',action='run',fields={'state':'paused'})
    host.call('workflow',action='run',fields={'state':'running'})
    assert host.call('runtime_budget_status')['expired']
    assert host.call('runtime_budget_stop')['stopped']
    assert host.call('status')['workflow']['run']['state']=='paused'


def test_notes_checkpoints_and_replays_do_not_reset_durable_idle(tmp_path):
    host,store=fixture(tmp_path)
    host.call('workflow',action='run',fields={'state':'running'})
    for turn in range(3):
        host.call('runtime_reserve')
        host.call('note',body=f'note {turn}',kind='progress')
        host.call('memory_write',action='checkpoint',fields={'state':{'turn':turn},'expected_revision':turn})
        assert host.call('runtime_turn_finished',turn=str(turn))['idle_turns']==turn+1
    assert NativeStore(host.root).runtime_turn_finished(host.host_id,'ses_main','3','next')['idle_turns']==4


def test_repeating_a_disposition_does_not_count_as_new_progress(tmp_path):
    host,store=fixture(tmp_path)
    basis=knowledge(host,'basis'); claim=knowledge(host,'claim',dependencies=[basis['ref']])
    revised=host.call('memory_write',action='revise',fields={'ref':basis['ref'],'preset':'correct_previous',
        'reason':'changed','changes':{'statement':'corrected'}})
    host.call('workflow',action='run',fields={'state':'running'})
    fields={'change_id':revised['change']['change_id'],'affected_version':claim['ref'],'disposition_kind':'unresolved','reason':'awaiting evidence'}
    for n in range(2):
        host.call('runtime_reserve')
        host.call('memory_write',action='dispose',fields=fields)
        value=host.call('runtime_turn_finished',turn=str(n))
        assert value['meaningful'] is (n==0)
        assert value['idle_turns']==n


def test_republishing_the_same_material_does_not_refresh_idle(tmp_path):
    host,store=fixture(tmp_path)
    host.call('workflow',action='run',fields={'state':'running'})
    for n in range(2):
        host.call('runtime_reserve')
        report(host)
        value=host.call('runtime_turn_finished',turn=str(n))
        assert value['meaningful'] is (n==0)
        assert value['idle_turns']==n


def test_native_usage_is_bound_at_start_and_aggregates_do_not_double_count(tmp_path):
    host,store=fixture(tmp_path)
    host.call('workflow',action='run',fields={'state':'running'})
    key='native:step'
    host.call('runtime_native_event',fields={'phase':'started','source_key':key,'model':{'id':'actual','providerID':'p','variant':'v'}})
    tokens={'input':10,'output':2,'reasoning':1,'cache':{'read':3,'write':4}}
    fields={'phase':'completed','source_key':key,'tokens':tokens,'cost':0.2}
    result=host.call('runtime_native_event',fields=fields,operation_id='end')
    assert host.call('runtime_native_event',fields=fields,operation_id='end')==result
    assert host.call('runtime_budget_status')['tokens_observed']==20
    with store._read() as db:
        rows=list(db.execute('SELECT * FROM usage_observations'))
        assert len(rows)==1 and rows[0]['model']=='actual'
        assert json.loads(rows[0]['details'])['variant']=='v'


def test_late_native_result_has_unknown_binding(tmp_path):
    host,store=fixture(tmp_path)
    fields={'phase':'completed','source_key':'late','tokens':None,'cost':None}
    assert host.call('runtime_native_event',fields=fields)['binding']=='unknown'
    assert host.call('runtime_budget_status')['tokens_observed'] is None


def test_native_source_collision_preserves_legacy_observation(tmp_path):
    host,store=fixture(tmp_path)
    with store._connection() as db:
        db.execute("INSERT INTO usage_observations VALUES('legacy','same',NULL,NULL,NULL,'conversation',NULL,NULL,12,'actual','{}','old')")
    assert 'existing non-native' in host.error('runtime_native_event',fields={'phase':'completed','source_key':'same','tokens':None,'cost':None})
    with store._read() as db:
        row=db.execute("SELECT amount,details FROM usage_observations WHERE source_key='same'").fetchone()
        assert tuple(row)==(12,'{}')


def test_report_inside_reviewed_publication_is_not_a_separate_report(tmp_path):
    host,store=fixture(tmp_path)
    ref=report(host); whole=ref.split('#')[0]
    host.call('review_scope_set',refs=[whole],reason='review this whole publication')
    row=next(row for row in obligations(store) if row['target_ref']==whole)
    assert 'outside the reviewed material' in host.error('relate',contract_version='r2-v1',source_ref=ref,
        target_ref=whole,obligation_id=row['obligation_id'],label='reviews',note='self',result='supports',complete=True)


def test_typed_relation_creates_version_and_motivation_does_not_propagate(tmp_path):
    host,store=fixture(tmp_path)
    a,b=knowledge(host,'basis'),knowledge(host,'claim')
    revision={'change_kind':'reword','affected_scope_mode':'none','affected_scope':[]}
    value=host.call('relate',contract_version='r2-v1',source_ref=b['ref'],target_ref=a['ref'],
        label='motivated_by',note='prompted work',revision=revision)
    assert value['ref'].endswith('@2') and value['dependencies']==[]
    assert value['motivated_by']==[a['ref']]
    grounded=host.call('relate',contract_version='r2-v1',source_ref=value['ref'],target_ref=a['ref'],
        label='grounded_in',note='actual support',revision=revision)
    assert grounded['dependencies']==[a['ref']]
    assert store.reference_query(b['ref'])['value']['dependencies']==[]


def test_consume_dedup_preserves_different_purposes(tmp_path):
    host,store=fixture(tmp_path)
    ref=report(host)
    nid=host.call('propose',question='Q',why_now='now',plan='p')['node_id']
    host.call('finish',state='finished',details={})
    host.call('focus',node_id=nid,role='core',mode='manual')
    a=host.call('consume',source_ref=ref,use='baseline',relation_type='context')
    b=host.call('consume',source_ref=ref,use='baseline',relation_type='context')
    c=host.call('consume',source_ref=ref,use='comparison',relation_type='context')
    assert a['consumption_id']==b['consumption_id'] and b['deduplicated']
    assert a['consumption_id']!=c['consumption_id']
    assert store.adoptions(node_id=nid)['total']==2


def test_completed_challenge_remains_visible_and_old_report_cannot_close_reopen(tmp_path):
    host,store=fixture(tmp_path)
    claim=knowledge(host,'claim')
    host.call('review_scope_set',refs=[claim['ref']],reason='current')
    row=obligations(store)[0]; frozen=report(host)
    result=link(host,row,frozen,result='challenges')
    assert result['state']=='completed'
    assert any(r['kind']=='review_challenge' for r in store.context_page('critical_now')['items'])
    host.call('review_exception',fields={'obligation_id':row['obligation_id'],'action':'reopen','reason':'check again'})
    assert 'predates reopen' in host.error('relate',contract_version='r2-v1',source_ref=frozen,target_ref=row['target_ref'],
        obligation_id=row['obligation_id'],label='reviews',note='reuse',result='supports',complete=True)
    assert link(host,row,report(host))['state']=='completed'
    # A later inconclusive review does not erase the earlier scientific challenge.
    assert any(r['kind']=='review_challenge' for r in store.context_page('critical_now')['items'])


def test_manual_node_promotion_is_scoped_to_its_active_attempt(tmp_path):
    host,store=fixture(tmp_path)
    claim=knowledge(host,'project claim')
    host.call('workflow',action='run',fields={'state':'running'})
    nid,sid,_=node(host)
    value=host.call('review_promote',fields={'target_ref':claim['ref'],'scope_owner':'node:'+nid,'reason':'explicit concern'})
    assert value['state']=='pending'
    host.call('note',session=sid,body='another write',kind='progress')
    assert next(r for r in obligations(store) if r['obligation_id']==value['obligation_id'])['state']=='pending'
    host.call('finish',session=sid,state='finished',details={})
    assert next(r for r in obligations(store) if r['obligation_id']==value['obligation_id'])['state']=='out_of_scope'


def test_two_changes_to_one_version_have_independent_review_obligations(tmp_path):
    host,store=fixture(tmp_path)
    basis=knowledge(host,'basis'); claim=knowledge(host,'dependent',dependencies=[basis['ref']])
    host.call('review_scope_set',refs=[claim['ref']],reason='current')
    first=host.call('memory_write',action='revise',fields={'ref':basis['ref'],'preset':'correct_previous','reason':'one','changes':{'statement':'one'}})
    host.call('memory_write',action='revise',fields={'ref':first['ref'],'preset':'custom','reason':'two','changes':{'statement':'two'},
        'change_kind':'correct','affected_scope_mode':'versions','affected_scope':[basis['ref']]})
    rows=[r for r in obligations(store) if r['target_ref']==claim['ref'] and r['change_id']]
    assert len(rows)==2 and rows[0]['change_id']!=rows[1]['change_id']
    link(host,rows[0],report(host))
    current={r['obligation_id']:r for r in obligations(store)}
    assert current[rows[0]['obligation_id']]['state']=='completed'
    assert current[rows[1]['obligation_id']]['state']=='pending'


def test_auxiliary_usage_reconciles_once_and_retains_actual_model(tmp_path):
    host,store=fixture(tmp_path)
    host.call('runtime_native_event',fields={'phase':'started','source_key':'request-aux','kind':'compaction',
        'model':{'providerID':'p','id':'actual','variant':'deep'}})
    fields={'phase':'completed','source_key':'aux-event','request_key':'request-aux','kind':'compaction',
        'tokens':{'input':4,'output':2,'reasoning':1,'cache':{'read':1,'write':0}},'cost':0.1}
    a=host.call('runtime_native_event',operation_id='aux-end',fields=fields)
    fields.pop('request_key')
    assert host.call('runtime_native_event',operation_id='aux-end',fields=fields)==a
    with store._read() as db:
        rows=list(db.execute('SELECT * FROM usage_observations'))
        assert len(rows)==1 and rows[0]['source_key']=='aux-event' and rows[0]['amount']==8
        assert rows[0]['model']=='actual' and json.loads(rows[0]['details'])['variant']=='deep'


def test_duplicate_terminal_event_is_claimed_once_across_restart(tmp_path):
    host,store=fixture(tmp_path)
    first=host.call('runtime_execution_end',turn='native-event',outcome='succeeded',operation_id='event-end')
    second=host.call('runtime_execution_end',turn='native-event',outcome='succeeded',operation_id='event-end')
    assert first['claimed'] and not second['claimed']
    host.call('runtime_turn_finished',turn='turn')
    assert host.call('runtime_turn_finished',turn='turn')['duplicate']
    assert host.call('runtime_budget_status')['turns']==0


def test_budgeted_queue_can_be_redispatched_after_explicit_increase(tmp_path):
    host,store=fixture(tmp_path)
    host.call('runtime_budget_set',limits={'max_wall_seconds':60},reason='test')
    host.call('workflow',action='run',fields={'state':'running'})
    nid=host.call('propose',question='Q',why_now='n',plan='p')['node_id']
    task=host.call('workflow',action='task',fields={'node_id':nid})
    with store._connection() as db:
        db.execute('UPDATE runtime_budgets SET deadline_at=?',((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
    host.call('runtime_budget_stop')
    with store._read() as db:
        assert db.execute('SELECT state FROM exploration_tasks WHERE task_id=?',(task['task_id'],)).fetchone()[0]=='cancelled'
    host.call('runtime_budget_set',limits={'max_wall_seconds':600},reason='increase')
    host.call('workflow',action='run',fields={'state':'running'})
    host.call('workflow',action='session',fields={'pause_reason':None})  # explicit resume
    again=host.call('workflow',action='task',fields={'node_id':nid})
    assert again['task_id']==task['task_id'] and again['state']=='queued'


def test_typed_node_edges_preserve_fixed_proposal_and_reject_cycles(tmp_path):
    host,store=fixture(tmp_path)
    a=host.call('propose',question='A',why_now='n',plan='p')['node_id']
    b=host.call('propose',question='B',why_now='n',plan='p')['node_id']
    fields=dict(contract_version='r2-v1',source_ref=b,target_ref=a,label='branches_from',note='branch')
    edge=host.call('relate',operation_id='typed-edge',**fields)
    assert edge['predecessor_node_id']==a and not edge['scheduling']
    assert store.reference_query(b)['value']['inputs']==[]
    assert 'cycle' in host.error('relate',contract_version='r2-v1',source_ref=a,target_ref=b,label='branches_from',note='cycle')
    assert host.call('relate',operation_id='typed-edge',**fields)==edge
    ordinary=host.call('relate',contract_version='r2-v1',source_ref=a,target_ref=b,label='compares',note='comparison')
    assert ordinary['label']=='compares'
