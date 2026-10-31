#!/usr/bin/env python3
"""Rebuild synthetic R1 fixtures and compare policies in one read transaction."""
import argparse
from collections import Counter
import json
import sqlite3
from statistics import median, quantiles
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from test_context_policy import completed, sections, rows
from auto_research.context_policy import encoded, context_metrics


def replay():
    results = []
    for scenario in ('ordinary', 'clerical_storm', 'long_conditions', 'withdrawal', 'field_failures', 'late_impact', 'unknown_scope'):
        with TemporaryDirectory(prefix='r1-shadow-') as folder:
            host, store, nid, sid, claim, publication = completed.__wrapped__(Path(folder))
            expected = [('publication', 'pub/P-001', None)]
            if scenario == 'clerical_storm':
                for i in range(100):
                    item = store.record_knowledge({'kind':'hypothesis','statement':f'mentions K-999@1 ({i})'}, f'clerical-{i}')
                    with store._connection() as db:
                        store.queue_review_in_tx(db, 'fixture', item['ref'], nid)
            if scenario == 'long_conditions':
                store.revise_knowledge({'ref':claim['ref'],'expected_revision':1,
                    'changes':{'conditions':{'case':'条件'*1000}},'reason':'long',
                    'affected_scope_mode':'none','change_kind':'reword'}, 'long')
            if scenario == 'withdrawal':
                withdrawn = store.revise_knowledge({'ref':claim['ref'],'expected_revision':1,
                    'changes':{'status':'retracted'},'reason':'withdrawn','change_kind':'retract',
                    'affected_scope_mode':'versions','affected_scope':[claim['ref']]}, 'withdraw')
                citer = store.record_knowledge({'kind':'claim','statement':'cites withdrawn version',
                    'evidence_refs':[withdrawn['ref']]}, 'citer')
                expected += [('retracted_basis',citer['ref'],None), ('publication',f"pub/{publication['publication_id']}",None)]
            if scenario == 'field_failures':
                with sqlite3.connect(store.db_path) as db:
                    for i, status in enumerate(('inconsistent','not_checkable')):
                        db.execute('INSERT INTO field_checks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (claim['ref'],i,'{}','pub/P-001#data',None,None,None,None,'fixture',status,
                             'value_mismatch' if i == 0 else 'missing_value','2026-10-31',0))
                        expected.append(('field_check',claim['ref'],i))
            if scenario in ('late_impact', 'unknown_scope'):
                followers = [store.record_knowledge({'kind':'claim','statement':f'Follower {i}',
                    'evidence_refs':[claim['ref']]},f'follower-{i}') for i in range(12)]
                revised = store.revise_knowledge({'ref':claim['ref'],'expected_revision':1,
                    'changes':{'statement':'Corrected result'},'reason':'correction',
                    'change_kind':'correct','affected_scope_mode':'versions' if scenario == 'late_impact' else 'unknown',
                    **({'affected_scope':[claim['ref']]} if scenario == 'late_impact' else {})},'correction')
                if scenario == 'late_impact':
                    for row in followers[:-1]:
                        store.dispose_impact({'change_id':revised['change']['change_id'],
                            'affected_version':row['ref'],'disposition_kind':'retained_with_evidence',
                            'reason':'fixture review','evidence_refs':[claim['ref']]},'dispose-'+row['knowledge_id'])
                expected.append(('impact',followers[-1]['ref'],None))
            before = rows(store)
            shadow = host.call('context_shadow', profile='opencode-auto')
            old, new = shadow['memory-v2'], shadow['memory-r1-v1']
            old_stats, new_stats = context_metrics(old), context_metrics(new)
            new_blocks = sections(new)
            old_routine = old_stats['routine_characters']
            visible = {(v['kind'],v['target'],v['risk'].get('check_index')) for v in new_blocks['critical_now']['items'] + new_blocks['critical_now'].get('risk_index', [])}
            query = host.call('query', collection='critical_now', limit=200)
            reachable = {(v['kind'],v['target'],v['risk'].get('check_index')) for v in query['items']}
            expanded = [store.reference_query(v['expand']['ref'], full=True) for v in query['items']]
            field_status = {key:dict(Counter('truncated' if key in v['truncated_fields'] else
                'not_recorded' if v[key] == {'status':'未记录'} else 'complete'
                for v in new_blocks['findings']['items'])) for key in ('scope','conditions')}
            role_budgets = []
            for role, session in (('main','ses_main'), ('node_core',sid)):
                for budget in (500,12000):
                    view = store.context_view('opencode',session,max_chars=budget,
                        profile='opencode-auto',policy='memory-r1-v1')
                    role_budgets.append({'role':role,'max_chars':budget,
                        'within_budget':len(view['text']) <= budget,
                        'metrics':context_metrics(view),'coverage':view['coverage'],
                        'truncation':view['truncation']})
            repeat = host.call('context_shadow',profile='opencode-auto')
            results.append({'fixture':scenario,'expected_critical':expected,
                'critical_index_recall': sum(v in visible for v in expected)/len(expected),
                'critical_query_recall':sum(v in reachable for v in expected)/len(expected),
                'ordinary_visible':any(v['ref']==claim['ref'] for v in new_blocks['findings']['items']),
                'critical_source_count':new['coverage']['critical_now']['total'],
                'critical_shown_count':new['coverage']['critical_now']['shown'],
                'critical_specific_index_count':new['coverage']['critical_now']['index_shown'],
                'critical_details_complete':new['coverage']['critical_now']['details_complete'],
                'findings_unique':len(new_blocks['findings']['items']) == len({v['ref'] for v in new_blocks['findings']['items']}),
                'displayed_refs_resolve':all(store.reference_query(v['ref'])['value']['ref']==v['ref'] for v in new_blocks['findings']['items']),
                'critical_expansions_read':len(expanded),
                'displayed_field_status':field_status,
                'role_budgets':role_budgets,
                'unchanged_repeat':{'same_text':{policy:repeat[policy]['text']==view['text'] for policy,view in shadow.items()},
                                    'old_routine_twice':old_routine + context_metrics(repeat['memory-v2'])['routine_characters'],
                                    'new_routine_twice':new_stats['routine_characters'] + context_metrics(repeat['memory-r1-v1'])['routine_characters']},
                'condition_truncations':new['truncation']['fields'],
                'read_only':rows(store) == before,
                'completed_node_count':host.call('query',collection='completed_findings')['total'],
                'old_routine_chars':old_routine,
                'new_routine_chars':new_stats['routine_characters'],
                'findings_chars':new['block_chars']['findings'],
                'old':old,'new':new})
    paired = [r for r in results if r['old_routine_chars'] > 0]
    old_values = [r['old_routine_chars'] for r in paired]
    new_values = [r['new_routine_chars'] for r in paired]
    summary = {'paired_samples':len(paired),'old_median':median(old_values),'new_median':median(new_values),
               'median_reduction':1-median(new_values)/median(old_values),
               'old_p90':quantiles(old_values,n=10,method='inclusive')[8],
               'new_p90':quantiles(new_values,n=10,method='inclusive')[8],
               'old_max':max(old_values),'new_max':max(new_values)}
    return {'kind':'synthetic same-transaction shadow', 'real_model_experiment':False,
            'metric':'rendered pending_review plus classified clerical items; R1 backlog plus clerical_index; characters, not tokens',
            'summary':summary,'results':results}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    text=json.dumps(replay(),ensure_ascii=False,indent=2)+'\n'
    if args.output:
        args.output.write_text(text)
    else:
        print(text,end='')
