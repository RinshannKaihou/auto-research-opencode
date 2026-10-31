#!/usr/bin/env python3
"""Scripted R1/R2 protocol comparison, not a model or research-quality experiment."""
import argparse
import json
from pathlib import Path
import statistics
import tempfile
import time

from auto_research.service import NativeService


def run(contract):
    with tempfile.TemporaryDirectory(prefix='r2-protocol-') as directory:
        root=Path(directory)/'project'
        service=NativeService(Path(directory)/'registry.sqlite3')
        sequence=0
        calls=[]
        model=[]
        def call(method,**fields):
            nonlocal sequence
            sequence+=1
            started=time.perf_counter()
            try:
                value=service.handle(dict(method=method,host_id='opencode',session_id='ses_main',
                    operation_id=f'probe-{sequence}',transport_id=f't-{sequence}',**fields))['value']
                calls.append({'method':method,'outcome':'succeeded','ms':(time.perf_counter()-started)*1000})
                return value
            except Exception as error:
                calls.append({'method':method,'outcome':getattr(error,'code',type(error).__name__),'ms':(time.perf_counter()-started)*1000})
                raise
        def tool(action,fields):
            inputs={'action':action,**fields} if contract=='legacy' else fields
            model.append({'tool':'research_memory' if contract=='legacy' else f'research_{action}',
                          'input_fields':len(inputs),'input_chars':len(json.dumps(inputs,ensure_ascii=False)),
                          'keys':list(inputs)})
            return call('memory_write',action=action,fields=fields,model_call=True,
                        **({'contract_version':'r2-v1'} if contract=='r2' else {}))
        call('open',root=str(root),cwd=str(root),goal='Scripted protocol fixture')
        call('focus',node_id=None,role='planner',mode='manual')
        value=call('memory_write',action='record',fields={'kind':'hypothesis','statement':'bounded statement',
            'visibility':'project','execution_refs':['session:ses_main']})
        for preset in ['editorial','correct_previous']:
            fields=dict(ref=value['ref'],changes={'statement':preset+' bounded statement'},reason='scripted fixture')
            if contract=='legacy':
                fields.update(expected_revision=value['revision'],change_kind='reword' if preset=='editorial' else 'correct',
                    affected_scope_mode='none' if preset=='editorial' else 'versions',
                    affected_scope=[] if preset=='editorial' else [value['ref']],execution_refs=['session:ses_main'])
            else:
                fields.update(preset=preset,preserve_sources=True,no_new_evidence=True)
            value=tool('revise',fields)
        view=call('memory_context',profile='opencode-auto')
        if contract=='r2':
            assert view.get('checkpoint_snapshot'), 'complete checkpoint snapshot must be delivered'
            base=call('checkpoint_observed',snapshot=view['checkpoint_snapshot'],origin='context',plugin_instance_id='probe')['base']
            inputs={'state':{'phase':'synthesis'}}
            model.append({'tool':'research_checkpoint','input_fields':1,'input_chars':len(json.dumps(inputs)), 'keys':['state']})
            call('memory_write',action='checkpoint',fields=inputs,contract_version='r2-v1',
                 checkpoint_base=base,plugin_instance_id='probe',model_call=True)
            try:
                call('memory_write',action='checkpoint',fields={'state':{'phase':'stale'}},
                    checkpoint_base=base,plugin_instance_id='probe',model_call=True)
                stale='accepted'
            except Exception as error:
                stale=getattr(error,'code',type(error).__name__)
        else:
            recovery=json.loads(next(part for part in view['text'].split('## ') if part.startswith('checkpoint_recovery\n')).split('\n',1)[1])
            tool('checkpoint',{'state':{'phase':'synthesis'},'expected_revision':recovery['head_revision']})
            call('memory_write',action='checkpoint',fields={'state':{'phase':'stale'}},model_call=True)
            stale='accepted'
        assert stale == ('checkpoint_conflict' if contract=='r2' else 'accepted'), stale
        return {'contract':contract,'model_calls':model,'host_calls':calls,'stale_write':stale,
                'model_input_fields':sum(row['input_fields'] for row in model),
                'host_calls_count':len(calls),'host_call_median_ms':statistics.median(row['ms'] for row in calls),
                'model_usage':'not_run','research_quality':'not_evaluated'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contract',choices=['legacy','r2'],required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.write_text(json.dumps(run(args.contract),ensure_ascii=False,indent=2)+'\n')
