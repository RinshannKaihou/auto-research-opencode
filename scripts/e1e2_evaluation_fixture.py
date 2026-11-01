#!/usr/bin/env python3
"""Create one isolated E1/E2 evaluation task without copying the scoring key."""
import argparse
import hashlib
import json
from pathlib import Path

from auto_research.service import NativeService


TASKS = Path(__file__).resolve().parents[1] / 'docs/evidence/e1-exploration/evaluation/tasks.json'


def seed_evidence(root, task_id):
    service = NativeService(root / '.research' / 'evaluation-registry.sqlite3')
    counter = 0

    def call(method, **fields):
        nonlocal counter
        counter += 1
        request = {'transport_id':str(counter), 'operation_id':f'fixture:{counter}', 'host_id':'opencode',
                   'session_id':'ses_evaluation_fixture', 'method':method, **fields}
        response = service.handle(request)
        if not response['ok']:
            raise RuntimeError(response['error']['message'])
        return response['value']

    call('open', root=str(root), cwd=str(root), goal='Prepare the assigned evidence decision')
    call('focus', node_id=None, role='planner', mode='manual')
    call('conclusion_contract', contract='evidence-v1')
    if task_id == 'E2-02-self-reference':
        (root / 'report.txt').write_text('Only the final report makes this claim.\n', encoding='utf-8')
        item = {'item_id':'data','kind':'text','source_path':'report.txt'}
    else:
        item = {'item_id':'data','kind':'text','content':{'value':7,'workload':'fixture'}}
    source = call('publish', status='complete', summary='Recorded material', gaps=[], knowledge_refs=[], items=[item])
    source_ref = f"pub/{source['publication_id']}#data"
    claim = call('memory_write', action='record', fields={'kind':'claim','statement':'Measured value is 7',
        'visibility':'project','scope':{'workload':'fixture'},'evidence_refs':[source_ref]})
    refs = [claim['ref']]
    if task_id == 'E2-03-qualified':
        return {'core_claim_ref':claim['ref'], 'source_ref':source_ref, 'additional_ref':'knowledge/K-404@1'}
    call('review_scope_set', refs=refs, reason='Current synthesis')
    view = call('query', collection='evidence', refs=refs,
                **({'include_materials':[source_ref]} if task_id == 'E2-02-self-reference' else {}))
    draft = call('conclusion_prepare', model_call=True, fields={'snapshot_id':view['snapshot_id'],
        'core_claim_refs':refs, 'outcome':'answered', 'gaps':[], 'risk_judgments':[], 'review_requirements':[],
        'exploration_stop':{'reason_kind':'goal_met','reason':'The stated target was measured'}})
    final_item = ({'item_id':'report','kind':'text','source_path':'report.txt'} if task_id == 'E2-02-self-reference'
                  else {'item_id':'report','kind':'text','content':{'text':'The measured result is 7.'}})
    publication = call('publish', status='complete', summary='Final report', gaps=[], knowledge_refs=refs,
        items=[final_item], draft_id=draft['draft_id'], report_item_id='report')
    final_ref = f"pub/{publication['publication_id']}#report"
    page = call('query', collection='evidence', draft_id=draft['draft_id'], limit=500)
    pages = []
    while True:
        end = page['cursor']['offset'] if page['cursor'] else page['total_chars']
        pages.append({'snapshot_id':page['snapshot_id'], 'offset':page['offset'], 'end':end,
            'manifest_digest':page['manifest_digest'], 'chunk_digest':hashlib.sha256(page['chunk'].encode()).hexdigest()})
        if not page['cursor']:
            break
        page = call('query', collection='evidence', cursor=page['cursor'], limit=500)
    call('evidence_observed', pages=pages, plugin_instance_id='fixture-before-current-session')
    if task_id == 'E2-01-stale':
        call('memory_write', action='revise', fields={'ref':claim['ref'], 'changes':{'status':'retracted'},
            'reason':'Original basis was withdrawn', 'change_kind':'retract',
            'affected_scope_mode':'versions', 'affected_scope':refs})
        obligation = next(item for item in call('query', collection='review_obligations')['items']
                          if item['target_ref'] == claim['ref'] and item['scope_owner'] == 'project')
        review = call('publish', status='complete', summary='Counterexample review', gaps=[], knowledge_refs=[],
            items=[{'item_id':'review','kind':'text','content':{'text':'The stated basis does not support this claim.'}}])
        call('relate', contract_version='r2-v1', label='reviews', note='Review of exact version',
            target_ref=claim['ref'], source_ref=f"pub/{review['publication_id']}#review",
            obligation_id=obligation['obligation_id'], result='challenges', complete=True)
    return {'core_claim_ref':claim['ref'], 'source_ref':source_ref, 'draft_id':draft['draft_id'], 'final_ref':final_ref}


def prepare(task_id, destination):
    tasks = {item['id']:item for item in json.loads(TASKS.read_text(encoding='utf-8'))['tasks']}
    task = tasks[task_id]
    root = Path(destination).resolve()
    root.mkdir()
    refs = seed_evidence(root, task_id) if task_id.startswith('E2-') else {}
    lines = [f"# {task['goal']}", '', f"预算：{task['budget']}", '', '## 已提供材料']
    lines.extend(f'- {item}' for item in task['materials'])
    if task.get('options'):
        lines.extend(['', '## 可考虑的选项', *[f'- {item}' for item in task['options']]])
    if refs:
        lines.extend(['', '## 固定记录', *[f'- {key}: {value}' for key,value in refs.items()]])
    if task_id == 'E1-03-direct-delivery':
        lines.extend(['', '运行现有测试：`python3 -B -m unittest -q`。'])
    (root / 'TASK.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    if task_id == 'E1-03-direct-delivery':
        (root / 'delivery.py').write_text('def recieve(name):\n    return f"received {name}"\n', encoding='utf-8')
        (root / 'test_delivery.py').write_text('import unittest\nfrom delivery import receive\n\n'
            'class DeliveryTest(unittest.TestCase):\n    def test_delivery(self):\n'
            '        self.assertEqual(receive("task"), "received task")\n', encoding='utf-8')
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--destination', type=Path, required=True, help='A new directory for this task only')
    args = parser.parse_args()
    prepare(args.task, args.destination)
    print('Prepared one task; scoring material was not copied.')


if __name__ == '__main__':
    main()
