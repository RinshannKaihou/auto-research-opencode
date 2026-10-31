import json
from pathlib import Path
from unittest.mock import patch

import pytest

from test_p0_contracts import node
from test_r2_runtime import fixture,knowledge,report,link,obligations


def blocks(view):
    return {name:json.loads(body) for name,body in (part.split('\n',1) for part in view['text'].split('## ')[1:])}


def test_observation_metadata_does_not_block_committed_detach_retry(tmp_path):
    host,store=fixture(tmp_path)
    host.call('finish',state='stopped',details={'reason':'detach'})
    # Interrupt between the project commit and registry update.
    original=store.detach(host.host_id,'ses_main','interrupted-detach')
    retry=host.raw('detach',operation_id='interrupted-detach')
    assert retry['ok'] and retry['replayed'] is True
    assert retry['value']==original
    assert 'invocation_context' not in retry
    assert 'not associated' in host.error('status').lower()


def test_missing_association_does_not_authorize_new_writes(tmp_path):
    host,store=fixture(tmp_path)
    host.call('finish',state='stopped',details={})
    store.detach(host.host_id,'ses_main','detach-only')
    assert 'not associated' in host.error('note',body='new unauthorized write',kind='progress').lower()


def test_runtime_source_metadata_and_replays_never_scan_backend_files(tmp_path):
    host,store=fixture(tmp_path)
    host.call('workflow',action='run',fields={'state':'running'})
    _,sid,_=node(host)
    owner=host.call('runtime_source_owner',session=sid)
    snapshot={'owner':owner,'algorithm':'source-worker-v1','digest':'0'*64}
    with patch.object(Path,'open',side_effect=AssertionError('Backend must not read source files')):
        first=host.call('runtime_reserve',session=sid,source_snapshot=snapshot,operation_id='reserve')
        assert host.call('runtime_reserve',session=sid,source_snapshot='ignored on replay',operation_id='reserve')==first
        result=host.call('runtime_turn_finished',session=sid,turn='turn',source_snapshot={**snapshot,'digest':'1'*64},operation_id='finish-turn')
        assert result['source_workspace_changed'] is True
        assert host.call('runtime_turn_finished',session=sid,turn='turn',source_snapshot='ignored on replay',operation_id='finish-turn')==result
    assert 'host controller' in host.error('runtime_source_owner',session=sid,model_call=True)


@pytest.mark.parametrize('mutation',['owner','algorithm','unknown'])
def test_host_source_snapshots_are_bound_to_owner_and_algorithm(tmp_path,mutation):
    host,_=fixture(tmp_path)
    host.call('workflow',action='run',fields={'state':'running'})
    _,sid,_=node(host)
    owner=host.call('runtime_source_owner',session=sid)
    baseline={'owner':owner,'algorithm':'source-worker-v1','digest':'0'*64}
    host.call('runtime_reserve',session=sid,source_snapshot=baseline)
    changed={**baseline,'digest':'1'*64}
    if mutation=='owner':changed['owner']={**owner,'attempt_id':'other-attempt'}
    elif mutation=='algorithm':changed['algorithm']='another-probe'
    else:changed['digest']=None
    result=host.call('runtime_turn_finished',session=sid,turn='turn',source_snapshot=changed)
    assert result['source_workspace_changed'] is None
    assert not result['meaningful']


def test_new_attempt_cannot_compare_its_source_to_an_old_attempt_baseline(tmp_path):
    host,_=fixture(tmp_path)
    host.call('workflow',action='run',fields={'state':'running'})
    nid,sid,_=node(host)
    old=host.call('runtime_source_owner',session=sid)
    host.call('runtime_reserve',session=sid,source_snapshot={'owner':old,'algorithm':'source-worker-v1','digest':'0'*64})
    host.call('finish',session=sid,state='finished',details={})
    host.call('focus',session=sid,node_id=nid,role='core',mode='manual')
    current=host.call('runtime_source_owner',session=sid)
    assert current['attempt_id']!=old['attempt_id']
    result=host.call('runtime_turn_finished',session=sid,turn='late-end',source_snapshot={'owner':current,'algorithm':'source-worker-v1','digest':'1'*64})
    assert result['source_workspace_changed'] is None


@pytest.mark.parametrize('policy',['memory-v2','memory-r1-v1'])
def test_large_checkpoint_cannot_crowd_evidence_out_of_context(tmp_path,policy):
    host,_=fixture(tmp_path)
    for i in range(12):knowledge(host,f'claim {i}: '+'bounded finding '*20)
    host.call('memory_write',action='checkpoint',fields={'state':{'notes':'x'*7000},'expected_revision':0})
    view=host.call('memory_context',profile='opencode-auto',policy=policy)
    sections=blocks(view)
    assert 'checkpoint' not in sections and 'checkpoint_snapshot' not in view
    assert len(view['text'])<=12000
    if policy=='memory-r1-v1':assert len(sections['findings']['items'])>2
    else:assert sections['knowledge']['project']['items']


def test_legacy_prioritizes_new_challenges_without_losing_old_generations(tmp_path):
    host,store=fixture(tmp_path)
    refs=[knowledge(host,f'old {i}')['ref'] for i in range(5)]
    host.call('review_scope_set',refs=refs,reason='old synthesis')
    for row in obligations(store):link(host,row,report(host),result='challenges')
    host.call('workflow',action='run',fields={'state':'running','new_generation':True})
    current=knowledge(host,'new claim')
    host.call('review_scope_set',refs=[current['ref']],reason='current synthesis')
    row=next(row for row in obligations(store) if row['target_ref']==current['ref'])
    saved=link(host,row,report(host),result='challenges')
    event=next(e['event_id'] for e in saved['events'] if e['action']=='completed')
    view=blocks(host.call('memory_context',profile='opencode-auto',policy='memory-v2'))
    assert view['review_challenges']['total']==6
    assert view['review_challenges']['items'][0]['review_event']==event
    assert len([r for r in store.context_page('critical_now')['items'] if r['kind']=='review_challenge'])==6
