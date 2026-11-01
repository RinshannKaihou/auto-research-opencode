"""Exercise E1 and E2 in an isolated OpenCode host with a scripted local provider.

The provider is a deterministic fixture, not a model-quality evaluation. Every
research tool still runs through OpenCode and the plugin's public host hooks.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from uuid import uuid4

from probe_opencode_execution import request
from probe_research_run import ResearchProvider


def objects(value):
    """Yield JSON objects from OpenAI-compatible tool result content."""
    if isinstance(value, str):
        try:
            yield from objects(json.loads(value))
        except (ValueError, RecursionError):
            return
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)
    elif isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item)


class E1E2Provider(ResearchProvider):
    database = None
    stage = 0
    actions = []
    failures = []
    material_pages = 0
    finished_nodes = set()
    lock = threading.Lock()

    @classmethod
    def rows(cls, query, args=()):
        with sqlite3.connect(cls.database) as db:
            return db.execute(query, args).fetchall()

    @classmethod
    def current(cls):
        scope = cls.rows('SELECT scope_id,revision FROM exploration_scopes ORDER BY rowid DESC LIMIT 1')[0]
        routes = cls.rows('SELECT route_id,MAX(revision) FROM exploration_routes GROUP BY route_id ORDER BY route_id')
        return f'explore/scope/{scope[0]}@{scope[1]}', [f'explore/route/{a}@{b}' for a, b in routes]

    @classmethod
    def last_page(cls, body, part=None):
        matches = [item for message in body.get('messages', []) if message.get('role') == 'tool'
                   for item in objects(message) if isinstance(item.get('chunk'), str)
                   and isinstance(item.get('snapshot_id'), str) and (part is None or item.get('part') == part)]
        if not matches:
            raise RuntimeError(f'No {part or "evidence"} tool page in the provider request')
        return matches[-1]

    @classmethod
    def choose(cls, body, names):
        if 'research_dispatch' not in names and 'research_finish' not in names:
            return None
        if 'research_finish' in names:
            tasks = cls.rows("SELECT node_id,state FROM exploration_tasks ORDER BY rowid")
            ordinal = len(tasks)
            if tasks[-1][0] in cls.finished_nodes:
                return None
            published = cls.rows('SELECT COUNT(*) FROM publications WHERE node_id=?', (tasks[-1][0],))[0][0]
            if not published:
                item = ({'item_id':'result','kind':'text','content':{'text':'The first probe is inconclusive'}}
                        if ordinal == 1 else {'item_id':'data','kind':'text',
                            'content':{'text':'synthetic measurement 温度😀\r\n' * 1100}})
                return 'research_publish', {'status':'partial' if ordinal == 1 else 'complete',
                    'summary':'controlled result', 'gaps':[], 'knowledge_refs':[], 'items':[item]}
            if ordinal == 2 and not cls.rows("SELECT 1 FROM knowledge_entries WHERE kind='claim'"):
                publication = cls.rows('SELECT publication_id FROM publications WHERE node_id=? ORDER BY rowid DESC', (tasks[-1][0],))[0][0]
                return 'research_record', {'kind':'claim','statement':'The second synthetic probe measured the target',
                    'visibility':'project','evidence_refs':[f'pub/{publication}#data']}
            cls.finished_nodes.add(tasks[-1][0])
            return 'research_finish', {'state':'finished','summary':'controlled result; first probe insufficient' if ordinal == 1 else 'controlled measurement complete'}

        stage = cls.stage
        if stage == 0:
            cls.stage = 1
            return 'research_scope', {'changes':{'objective':'Compare two synthetic strategies','task_kind':'selection'}}
        if stage in (1, 2):
            scope, _ = cls.current()
            cls.stage += 1
            return 'research_route', {'scope_ref':scope, 'changes':{'title':f'Candidate {stage}',
                'rationale':'Compare controlled options','critical_assumption':'The probe distinguishes the options'}}
        if stage == 3:
            cls.stage = 4
            return 'research_query', {'collection':'evidence','refs':[],'limit':16384}
        if stage == 4:
            scope, routes = cls.current()
            cls.stage = 5
            return 'research_decide', {'scope_ref':scope,'route_refs':routes,'snapshot_id':cls.last_page(body)['snapshot_id'],
                'result_ids':[],'action':'probe','reason':'Test the first candidate',
                'next_work':{'route_refs':[routes[0]],'question':'Is candidate one sufficient?',
                    'why_now':'First comparison','plan':'Run a bounded probe','root_reason':'Initial comparison'}}
        if stage in (5, 8):
            tasks = cls.rows('SELECT task_id,state FROM exploration_tasks ORDER BY rowid')
            if len(tasks) < (1 if stage == 5 else 2):
                return None
            cls.stage += 1
            return 'research_wait', {'task_ids':[tasks[-1][0]]}
        if stage in (6, 9):
            tasks = cls.rows('SELECT state FROM exploration_tasks ORDER BY rowid')
            if len(tasks) < (1 if stage == 6 else 2) or tasks[-1][0] != 'finished':
                return None
            cls.stage += 1
            return 'research_query', {'collection':'evidence','refs':[],'limit':16384}
        if stage in (7, 10):
            scope, routes = cls.current()
            results = [row[0] for row in cls.rows('SELECT result_id FROM exploration_results WHERE handled_by_decision IS NULL ORDER BY result_id')]
            page = cls.last_page(body)
            if stage == 7:
                cls.stage = 8
                return 'research_decide', {'scope_ref':scope,'route_refs':routes,'snapshot_id':page['snapshot_id'],
                    'result_ids':results,'action':'redirect','reason':'First result is insufficient',
                    'route_changes':[{'route_ref':routes[0],'disposition':'deferred','reason':'Insufficient result',
                                      'reactivation_condition':'New measurements'}],
                    'next_work':{'route_refs':[routes[1]],'question':'Can the alternative answer?',
                        'why_now':'Redirect after first result','plan':'Measure the alternative','root_reason':'Independent alternative'}}
            cls.stage = 11
            return 'research_decide', {'scope_ref':scope,'route_refs':routes,'snapshot_id':page['snapshot_id'],
                'result_ids':results,'action':'propose_stop','reason':'The second probe is sufficient for this bounded comparison',
                'route_changes':[{'route_ref':routes[1],'disposition':'adopted','reason':'Measured alternative'}]}
        if stage == 11:
            cls.stage = 12
            publication = cls.rows("SELECT publication_id FROM publication_items WHERE item_id='data'")[0][0]
            return 'research_query', {'collection':'evidence','material_ref':f'pub/{publication}#data','limit':16384}
        if stage == 12:
            page = cls.last_page(body, 'material')
            cls.material_pages += 1
            if page.get('cursor'):
                return 'research_query', {'collection':'evidence','cursor':page['cursor'],'limit':16384}
            claim = cls.rows("SELECT r.knowledge_id,MAX(r.revision) FROM knowledge_revisions r JOIN knowledge_entries e USING(knowledge_id) WHERE e.kind='claim' GROUP BY r.knowledge_id")[0]
            cls.stage = 13
            return 'research_query', {'collection':'evidence','refs':[f'knowledge/{claim[0]}@{claim[1]}','knowledge/K-999@1'],'limit':16384}
        if stage == 13:
            page = cls.last_page(body)
            view = json.loads(page['chunk'])
            claim = cls.rows("SELECT r.knowledge_id,MAX(r.revision) FROM knowledge_revisions r JOIN knowledge_entries e USING(knowledge_id) WHERE e.kind='claim' GROUP BY r.knowledge_id")[0]
            ref = f'knowledge/{claim[0]}@{claim[1]}'
            risks = [item for item in view['risks'] if item['kind'] != 'version_notice' and not (
                item['kind'] == 'impact' and item.get('disposition') and item['disposition']['kind'] != 'unresolved')]
            risks += view['limitations']
            judgments = [{'risk_id':item['risk_id'],'core_impact':'no','disposition':'unresolved',
                'reason':'This missing comparison is outside the measured core claim','evidence_refs':[ref],
                'gap':'The separate comparison remains unavailable'} for item in risks]
            cls.stage = 14
            return 'research_prepare_conclusion', {'snapshot_id':page['snapshot_id'],'core_claim_refs':[ref],
                'outcome':'answered','gaps':['Separate comparison remains unavailable'], 'risk_judgments':judgments,
                'review_requirements':[], 'exploration_stop':{'reason_kind':'sufficient_for_current_choice',
                    'reason':'The measured alternative supports the bounded choice'}}
        if stage == 14:
            drafts = cls.rows('SELECT draft_id FROM conclusion_drafts ORDER BY rowid DESC')
            if not drafts:
                messages = [message for message in body.get('messages',[]) if message.get('role') == 'tool']
                raise RuntimeError(f'Prepare did not create a draft: {str(messages[-1])[:1200] if messages else "no tool result"}')
            draft = drafts[0][0]
            claim = cls.rows("SELECT r.knowledge_id,MAX(r.revision) FROM knowledge_revisions r JOIN knowledge_entries e USING(knowledge_id) WHERE e.kind='claim' GROUP BY r.knowledge_id")[0]
            cls.stage = 15
            return 'research_publish', {'status':'complete','summary':'Qualified comparison',
                'gaps':['Separate comparison unavailable'],'knowledge_refs':[f'knowledge/{claim[0]}@{claim[1]}'],
                'draft_id':draft,'report_item_id':'report',
                'items':[{'item_id':'report','kind':'text','content':{'text':'Qualified result for the synthetic comparison'}}]}
        if stage == 15:
            draft = cls.rows('SELECT draft_id FROM conclusion_drafts ORDER BY rowid DESC')[0][0]
            cls.stage = 16
            return 'research_query', {'collection':'evidence','draft_id':draft,'limit':16384}
        if stage == 16:
            page = cls.last_page(body, 'decision')
            if page.get('cursor'):
                return 'research_query', {'collection':'evidence','cursor':page['cursor'],'limit':16384}
            draft = cls.rows('SELECT draft_id FROM conclusion_drafts ORDER BY rowid DESC')[0][0]
            final = cls.rows("SELECT publication_id FROM publication_items WHERE item_id='report' ORDER BY rowid DESC")[0][0]
            cls.stage = 17
            return 'research_conclude', {'draft_id':draft,'final_ref':f'pub/{final}#report','summary':'Qualified synthetic comparison'}
        return None

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
        names = {item.get('function',{}).get('name') for item in body.get('tools',[]) if item.get('type') == 'function'}
        self.before_request(names)
        with self.lock:
            try:
                choice = self.choose(body, names)
                if choice:
                    action, args = choice
                    if len(self.actions) >= 3 and self.actions[-3:] == [action] * 3 and action not in {'research_query'}:
                        tools = [message for message in body.get('messages',[]) if message.get('role') == 'tool']
                        raise RuntimeError(f'Repeated {action}; last tool result: {str(tools[-1])[:500] if tools else "missing"}')
                    self.actions.append(action)
                else:
                    action, args = None, None
            except Exception as error:
                self.failures.append(f'{type(error).__name__}: {error}')
                action, args = None, None
        delta = ({'tool_calls':[{'index':0,'id':'call_'+uuid4().hex,'type':'function',
                  'function':{'name':action,'arguments':json.dumps(args)}}]} if action
                 else {'role':'assistant','content':'waiting for the next controlled event'})
        chunks = [{'id':'probe','object':'chat.completion.chunk','created':1,'model':'probe',
                   'choices':[{'index':0,'delta':delta,'finish_reason':None}]},
                  {'id':'probe','object':'chat.completion.chunk','created':1,'model':'probe',
                   'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls' if action else 'stop'}],
                   'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}]
        data = (''.join('data: '+json.dumps(item)+'\n\n' for item in chunks)+'data: [DONE]\n\n').encode()
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    @classmethod
    def before_request(cls, _names):
        pass


class RecoveryProvider(E1E2Provider):
    scenario = None
    node_gate = threading.Event()
    node_waiting = threading.Event()

    @classmethod
    def before_request(cls, names):
        if 'research_finish' in names:
            cls.node_waiting.set()
            cls.node_gate.wait(timeout=50)

    @classmethod
    def choose(cls, body, names):
        if 'research_finish' in names:
            node = cls.rows('SELECT node_id FROM exploration_tasks ORDER BY rowid LIMIT 1')[0][0]
            if node in cls.finished_nodes:
                return None
            cls.finished_nodes.add(node)
            return 'research_finish', {'state':'finished','summary':'The ordinary task finished'}
        if 'research_dispatch' not in names:
            return None
        stage = cls.stage
        if stage == 0:
            cls.stage = 1
            return 'research_propose', {'question':'Ordinary work occupies the slot','why_now':'fixture',
                'plan':'Finish after the gate','root_reason':'Existing task'}
        if stage == 1:
            cls.stage = 2
            return 'research_dispatch', {'node_id':cls.rows('SELECT node_id FROM nodes ORDER BY rowid LIMIT 1')[0][0]}
        if stage == 2:
            cls.stage = 3
            return 'research_scope', {'changes':{'objective':'Choose an option','task_kind':'selection'}}
        if stage == 3:
            cls.stage = 4
            scope, _ = cls.current()
            return 'research_route', {'scope_ref':scope,'changes':{'title':'Candidate','rationale':'Compare',
                'critical_assumption':'The original workload applies'}}
        if stage == 4:
            cls.stage = 5
            return 'research_query', {'collection':'evidence','refs':[],'limit':16384}
        if stage == 5:
            cls.stage = 6
            scope, routes = cls.current()
            return 'research_decide', {'scope_ref':scope,'route_refs':routes,'snapshot_id':cls.last_page(body)['snapshot_id'],
                'result_ids':[],'action':'probe','reason':'Test the option',
                'next_work':{'route_refs':routes,'question':'Does it work?','why_now':'fixture',
                             'plan':'A bounded probe','root_reason':'First probe'}}
        if stage == 6 and cls.scenario == 'stale':
            cls.stage = 7
            scope, routes = cls.current()
            return 'research_route', {'scope_ref':scope,'route_ref':routes[0],
                'reason':'The assumption changed before the queued task starts',
                'changes':{'critical_assumption':'A different workload applies'}}
        if stage in (6,7):
            tasks = cls.rows('SELECT task_id FROM exploration_tasks ORDER BY rowid')
            if len(tasks) < 2:
                return None
            cls.stage = 8
            return 'research_wait', {'task_ids':[tasks[-1][0]]}
        if stage == 8:
            results = cls.rows("SELECT result_id FROM exploration_results WHERE kind IN ('stale','cancelled') AND handled_by_decision IS NULL")
            if not results:
                return None
            cls.stage = 9
            return 'research_query', {'collection':'evidence','refs':[],'limit':16384}
        if stage == 9:
            scope, routes = cls.current()
            results = [row[0] for row in cls.rows('SELECT result_id FROM exploration_results WHERE handled_by_decision IS NULL ORDER BY result_id')]
            cls.stage = 10
            return 'research_decide', {'scope_ref':scope,'route_refs':routes,'snapshot_id':cls.last_page(body)['snapshot_id'],
                'result_ids':results,'action':'defer','reason':'Record the cancelled probe for later'}
        return None


class MaterialFailureProvider(E1E2Provider):
    """Exercise an unavailable frozen object while its old tool output remains visible."""
    observations = {}
    trace = []
    saved_object = None

    @classmethod
    def choose(cls, body, names):
        if 'research_dispatch' not in names or 'research_finish' in names:
            return None
        stage = cls.stage
        tool_results = [message for message in body.get('messages', []) if message.get('role') == 'tool']
        if tool_results:
            cls.trace.append({'stage':stage, 'last_tool_result':tool_results[-1]})
        if stage == 0:
            cls.stage = 1
            return 'research_publish', {'status':'complete','summary':'synthetic materials','gaps':[],
                'knowledge_refs':[], 'items':[{'item_id':'good','kind':'text','source_path':'good.txt'},
                                               {'item_id':'missing','kind':'text','source_path':'missing.txt'}]}
        publication = cls.rows("SELECT publication_id FROM publication_items WHERE item_id='missing'")[0][0]
        good, missing = f'pub/{publication}#good', f'pub/{publication}#missing'
        if stage in (1, 2):
            cls.stage += 1
            return 'research_query', {'collection':'evidence', 'material_ref':good if stage == 1 else missing}
        if stage == 3:
            object_version = cls.rows("SELECT object_version FROM publication_items WHERE item_id='missing'")[0][0]
            path = cls.database.parent / 'objects' / object_version
            cls.saved_object = (path, path.read_bytes())
            path.unlink()
            cls.observations['old_material_output_visible'] = any(
                item.get('material_ref') == missing for message in tool_results for item in objects(message))
            cls.stage = 4
            return 'research_query', {'collection':'evidence','refs':[good,missing], 'limit':16384}
        if stage == 4:
            view = json.loads(cls.last_page(body)['chunk'])
            by_ref = {item['ref']:item for item in view['materials']}
            cls.observations['valid_page_confirmed'] = by_ref[good]['material_read']['complete']
            cls.observations['missing_page_not_confirmed'] = not (by_ref[missing].get('material_read') or {}).get('complete',False)
            cls.observations['missing_reported'] = any(item.get('ref') == missing for item in view['limitations'])
            path, content = cls.saved_object
            path.write_bytes(content)
            cls.stage = 5
            return 'research_query', {'collection':'evidence','material_ref':missing}
        if stage == 5:
            cls.stage = 6
            return 'research_query', {'collection':'evidence','refs':[good,missing], 'limit':16384}
        if stage == 6:
            view = json.loads(cls.last_page(body)['chunk'])
            by_ref = {item['ref']:item for item in view['materials']}
            cls.observations['fresh_page_confirmed_after_restore'] = by_ref[missing]['material_read']['complete']
            cls.stage = 7
            return 'research_query', {'collection':'nodes'}
        if stage == 7:
            cls.observations['subsequent_tool_succeeded'] = any('items' in item for item in objects(tool_results[-1]))
            cls.stage = 8
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plugin', type=Path, default=Path(__file__).resolve().parents[1] / 'plugin')
    parser.add_argument('--scenario',choices=('full','stale','off','restart','material-failure'),default='full')
    parser.add_argument('--trace-output',type=Path,help='write selected synthetic host tool results')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='ari-e1e2-host-') as directory:
        root = Path(directory)
        workspace = root / 'project'
        workspace.mkdir()
        if args.scenario == 'material-failure':
            (workspace / 'good.txt').write_text('Valid synthetic material.', encoding='utf-8')
            (workspace / 'missing.txt').write_text('Synthetic material that is temporarily unavailable.', encoding='utf-8')
        env = {key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG','LC_ALL') if key in os.environ}
        for name in ('DATA','CONFIG','STATE','CACHE'):
            path = root / name.lower()
            path.mkdir()
            env[f'XDG_{name}_HOME'] = str(path)
        password = uuid4().hex
        env.update(OPENCODE_SERVER_PASSWORD=password, OPENCODE_DISABLE_MODELS_FETCH='1', ARI_PROBE_KEY='test-only')
        provider = (E1E2Provider if args.scenario == 'full' else
                    MaterialFailureProvider if args.scenario == 'material-failure' else RecoveryProvider)
        provider.scenario = args.scenario
        if provider is RecoveryProvider:
            provider.node_gate.clear()
            provider.node_waiting.clear()
        fake = ThreadingHTTPServer(('127.0.0.1',0), provider)
        threading.Thread(target=fake.serve_forever,daemon=True).start()
        config = root / 'config' / 'opencode'
        config.mkdir()
        (config / 'opencode.jsonc').write_text(json.dumps({'plugins':[str(args.plugin.resolve())], 'model':'ari-probe/probe',
            'providers':{'ari-probe':{'name':'isolated test','env':['ARI_PROBE_KEY'],
                'package':'@opencode/ai/providers/openai-compatible',
                'settings':{'baseURL':f'http://127.0.0.1:{fake.server_port}/v1'},
                'models':{'probe':{'name':'probe','limit':{'context':200000,'output':1024}}}}}}))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        provider.database = workspace / '.research' / 'state.sqlite3'
        provider.actions, provider.failures, provider.stage, provider.material_pages = [], [], 0, 0
        provider.finished_nodes = set()
        if provider is MaterialFailureProvider:
            provider.observations, provider.trace, provider.saved_object = {}, [], None
        with (root / 'server.log').open('w+') as log:
            process = subprocess.Popen(['opencode','serve','--hostname','127.0.0.1','--port',str(port)],
                                       cwd=workspace,env=env,stdout=log,stderr=log)
            try:
                deadline = time.monotonic()+20
                while time.monotonic() < deadline:
                    try:
                        if request(base,'/api/session/active',password)[0] == 200:
                            break
                    except OSError:
                        pass
                    time.sleep(.1)
                sid = 'ses_e1e2_'+uuid4().hex
                status, _ = request(base,'/api/session',password,{'id':sid,'title':'isolated E1/E2',
                    'location':{'directory':str(workspace)},'model':{'providerID':'ari-probe','id':'probe'}})
                if status != 200:
                    raise RuntimeError('Could not create coordinator')
                def command(action, values):
                    deadline = time.monotonic()+8
                    while True:
                        status, response = request(base,'/api/rpc/auto-research/command',password,
                            {'input':{'action':action,'sessionID':sid,'directory':str(workspace),'args':values}})
                        if status == 200 or time.monotonic() >= deadline:
                            break
                        time.sleep(.1)
                    output = response.get('output',{})
                    if status != 200 or output.get('error'):
                        raise RuntimeError(f'{action} failed: HTTP {status} {str(response)[:500]}')
                command('init',{'goal':'isolated E1/E2 fixture'})
                command('budget',{'limit':'turns','value':'80','reason':'bounded host fixture'})
                command('exploration',{'mode':'on'})
                command('auto',{})
                if provider is MaterialFailureProvider:
                    deadline = time.monotonic()+45
                    while provider.stage < 8 and time.monotonic() < deadline:
                        if provider.failures:
                            raise RuntimeError(provider.failures[-1])
                        time.sleep(.1)
                    observed = {'scenario':'material-failure','actions':provider.actions,
                                **provider.observations,'used_external_model':False}
                    if provider.stage != 8 or not all(provider.observations.values()) or len(provider.observations) != 6:
                        raise RuntimeError(json.dumps(observed,sort_keys=True))
                    if args.trace_output:
                        trace = json.dumps(provider.trace, ensure_ascii=False, indent=2).replace(str(workspace),'<isolated-project>')
                        args.trace_output.write_text(trace+'\n')
                    print(json.dumps(observed,sort_keys=True))
                    return
                if provider is RecoveryProvider:
                    deadline = time.monotonic()+30
                    while time.monotonic() < deadline:
                        if provider.failures:
                            raise RuntimeError(provider.failures[-1])
                        with sqlite3.connect(provider.database) as db:
                            tasks = db.execute('SELECT state FROM exploration_tasks ORDER BY rowid').fetchall()
                            waiting = db.execute("SELECT pause_reason FROM workflow_sessions WHERE role='main' AND detached=0").fetchone()
                        if len(tasks) == 2 and tasks[0][0] == 'running' and tasks[1][0] == 'queued' and waiting and waiting[0] == 'wait':
                            break
                        time.sleep(.1)
                    else:
                        raise RuntimeError('Coordinator did not wait on a queued exploration behind an ordinary task')
                    if args.scenario == 'restart':
                        old_pid = process.pid
                        process.kill()
                        process.wait(timeout=5)
                        provider.node_gate.set()
                        process = subprocess.Popen(['opencode','serve','--hostname','127.0.0.1','--port',str(port)],
                                                   cwd=workspace,env=env,stdout=log,stderr=log)
                        deadline = time.monotonic()+20
                        while time.monotonic() < deadline:
                            try:
                                if request(base,'/api/session/active',password)[0] == 200:
                                    break
                            except OSError:
                                pass
                            time.sleep(.1)
                        activation, _ = request(base,'/api/session',password,{'id':'ses_activate_'+uuid4().hex,
                            'title':'activate recovered plugin','location':{'directory':str(workspace)},
                            'model':{'providerID':'ari-probe','id':'probe'}})
                        if activation != 200:
                            raise RuntimeError('Could not activate the restarted plugin')
                        command('status',{})
                        deadline = time.monotonic()+20
                        while time.monotonic() < deadline:
                            with sqlite3.connect(provider.database) as db:
                                run_state = db.execute('SELECT state FROM workflow_project WHERE id=1').fetchone()[0]
                                task_states = [row[0] for row in db.execute('SELECT state FROM exploration_tasks ORDER BY rowid')]
                                nodes = db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0]
                                intents = db.execute("SELECT COUNT(*) FROM workflow_intents WHERE kind='exploration_work'").fetchone()[0]
                            if run_state == 'paused' and task_states == ['unverified','queued']:
                                break
                            time.sleep(.1)
                        if run_state != 'paused' or task_states != ['unverified','queued'] or nodes != 2 or intents != 1:
                            raise RuntimeError(f'Cold E1 recovery changed its work: {run_state} {task_states} {nodes} {intents}')
                        command('resume',{})
                        time.sleep(.3)
                        with sqlite3.connect(provider.database) as db:
                            after = [row[0] for row in db.execute('SELECT state FROM exploration_tasks ORDER BY rowid')]
                            nodes_after = db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0]
                            intents_after = db.execute("SELECT COUNT(*) FROM workflow_intents WHERE kind='exploration_work'").fetchone()[0]
                        observed = {'scenario':'restart','host_process_changed':old_pid != process.pid,
                            'state_after_restart':run_state,'tasks_after_restart':task_states,'tasks_after_resume':after,
                            'node_count_before':nodes,'node_count_after':nodes_after,
                            'exploration_intents_before':intents,'exploration_intents_after':intents_after,
                            'used_external_model':False}
                        if after != ['unverified','queued'] or nodes_after != nodes or intents_after != intents:
                            raise RuntimeError(json.dumps(observed,sort_keys=True))
                        print(json.dumps(observed,sort_keys=True))
                        return
                    if args.scenario == 'off':
                        command('exploration',{'mode':'off'})
                    else:
                        provider.node_gate.set()
                    deadline = time.monotonic()+25
                    while time.monotonic() < deadline:
                        if provider.failures:
                            raise RuntimeError(provider.failures[-1])
                        with sqlite3.connect(provider.database) as db:
                            result = db.execute("SELECT kind,handled_by_decision FROM exploration_results "
                                "WHERE kind IN ('stale','cancelled') ORDER BY rowid DESC LIMIT 1").fetchone()
                            notification = db.execute("SELECT state FROM workflow_notifications WHERE kind='exploration_result' ORDER BY rowid DESC LIMIT 1").fetchone()
                            wake = db.execute("SELECT state FROM workflow_intents WHERE kind='notification_wake' ORDER BY rowid DESC LIMIT 1").fetchone()
                        if result and result[1] and notification and notification[0] == 'delivered' and wake and wake[0] == 'delivered':
                            break
                        time.sleep(.1)
                    provider.node_gate.set()
                    with sqlite3.connect(provider.database) as db:
                        task_states = [row[0] for row in db.execute('SELECT state FROM exploration_tasks ORDER BY rowid')]
                        node_count = db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0]
                    observed = {'scenario':args.scenario,'actions':provider.actions,'task_states':task_states,
                        'node_count':node_count,'result_kind':result[0] if result else None,
                        'result_handled':bool(result and result[1]),'notification':notification[0] if notification else None,
                        'wake':wake[0] if wake else None,'used_external_model':False}
                    expected = 'stale' if args.scenario == 'stale' else 'cancelled'
                    if not result or result[0] != expected or not result[1] or not notification or notification[0] != 'delivered' or not wake or wake[0] != 'delivered' or task_states[-1] != 'cancelled' or node_count != 2:
                        raise RuntimeError(json.dumps(observed,sort_keys=True))
                    print(json.dumps(observed,sort_keys=True))
                    return
                deadline = time.monotonic()+75
                state = None
                while time.monotonic() < deadline:
                    if provider.failures:
                        raise RuntimeError(provider.failures[-1])
                    with sqlite3.connect(provider.database) as db:
                        state = db.execute('SELECT state FROM workflow_project WHERE id=1').fetchone()[0]
                    if state in {'complete','paused','stopped'}:
                        break
                    time.sleep(.1)
                with sqlite3.connect(provider.database) as db:
                    decisions = [json.loads(row[0])['action'] for row in db.execute('SELECT data FROM exploration_decisions ORDER BY rowid')]
                    task_states = [row[0] for row in db.execute('SELECT state FROM exploration_tasks ORDER BY rowid')]
                    receipts = db.execute("SELECT COUNT(*) FROM evidence_read_receipts WHERE part='material' AND model_message_id IS NOT NULL").fetchone()[0]
                    conclusion = db.execute('SELECT outcome FROM project_conclusions ORDER BY rowid DESC LIMIT 1').fetchone()
                    draft = db.execute('SELECT data FROM conclusion_drafts ORDER BY rowid DESC LIMIT 1').fetchone()
                closure = json.loads(draft[0])['evaluation']['evidence_closure']['status'] if draft else None
                result = {'run_state':state,'actions':provider.actions,'decisions':decisions,'tasks':task_states,
                          'material_pages':provider.material_pages,'material_receipts':receipts,
                          'outcome':conclusion[0] if conclusion else None,'closure':closure,
                          'used_external_model':False}
                if state != 'complete' or decisions != ['probe','redirect','propose_stop'] or task_states != ['finished','finished'] or closure != 'qualified' or receipts < 2:
                    raise RuntimeError(json.dumps(result,sort_keys=True))
                print(json.dumps(result,sort_keys=True))
            except Exception:
                log.flush()
                log.seek(0)
                print(json.dumps({'stage':provider.stage,'actions':provider.actions[-40:],
                                  'failures':provider.failures,'host_log_tail':log.read()[-8000:]},sort_keys=True))
                raise
            finally:
                if provider is RecoveryProvider:
                    provider.node_gate.set()
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                fake.shutdown()
                fake.server_close()


if __name__ == '__main__':
    main()
