"""Isolated real OpenCode + deterministic loopback-provider E3 engineering probe.

Every action traverses the public plugin tools. Provider-visible tool messages
are retained verbatim in the trace; no external model or quality claim is made.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from uuid import uuid4

from probe_e1e2_run import E1E2Provider, objects
from probe_opencode_execution import request


class E3Provider(E1E2Provider):
    scenario = 'full'
    trace = []
    observed_tools = set()
    late_release = threading.Event()

    @classmethod
    def node(cls, body):
        text = json.dumps(body.get('messages', []), ensure_ascii=False)
        matches = re.findall(r'你是节点 (X-\d+) 的执行会话', text)
        if not matches:
            raise RuntimeError('A node provider request did not contain its own kickoff identity')
        nid = matches[-1]
        rows = cls.rows("SELECT json_extract(details,'$.next_work.key') FROM workflow_intents "
                        "WHERE kind='exploration_work' AND json_extract(details,'$.node_id')=?", (nid,))
        if not rows:
            raise RuntimeError('No exploration intent for the actual node session ' + nid)
        return nid, rows[0][0]

    @classmethod
    def task(cls, key):
        rows = cls.rows("SELECT t.task_id,t.node_id,t.state,i.intent_id FROM workflow_intents i "
            "JOIN exploration_tasks t ON t.task_id=json_extract(i.details,'$.task_id') "
            "WHERE i.kind='exploration_work' AND json_extract(i.details,'$.next_work.key')=?", (key,))
        return rows[0] if rows else None

    @classmethod
    def result_ref(cls, key):
        task = cls.task(key)
        publication = cls.rows('SELECT publication_id FROM publications WHERE node_id=? ORDER BY rowid DESC LIMIT 1', (task[1],))
        return f'pub/{publication[0][0]}#data'

    @classmethod
    def pending(cls, scope=None):
        return [row[0] for row in cls.rows('SELECT result_id FROM exploration_results '
            'WHERE handled_by_decision IS NULL' + (' AND scope_id=?' if scope else '') + ' ORDER BY result_id',
            (scope,) if scope else ())]

    @classmethod
    def scopes(cls):
        return [f'explore/scope/{a}@{b}' for a, b in cls.rows('SELECT scope_id,MAX(revision) '
            'FROM exploration_scopes GROUP BY scope_id ORDER BY scope_id')]

    @classmethod
    def local_routes(cls, scope):
        return [f'explore/route/{a}@{b}' for a, b in cls.rows('SELECT route_id,MAX(revision) '
            'FROM exploration_routes WHERE scope_id=? GROUP BY route_id ORDER BY route_id', (scope.split('/')[-1].split('@')[0],))]

    @classmethod
    def work(cls, key, routes, inputs=None, kind='probe'):
        return {'key': key, 'route_refs': routes, 'question': 'E3:' + key,
                'why_now': 'Controlled E3 engineering fixture', 'plan': 'Publish the deterministic fixture measurement',
                'root_reason': 'Isolated test question ' + key, 'work_kind': kind,
                'budget_amount': 1, 'inputs': inputs or [],
                **({'resources': {'gpu:fixture': 1}} if cls.scenario == 'resource' else {})}

    @classmethod
    def advance(cls, name, args):
        cls.stage += 1
        return name, args

    @classmethod
    def read(cls, refs=None, scope=None):
        cls.latest_read_stage = cls.stage
        return cls.advance('research_query', {'collection': 'evidence', 'refs': refs or [], 'limit': 16384,
            **({'scope_id': scope.split('/')[-1].split('@')[0]} if scope else {})})

    @classmethod
    def wait(cls, key):
        task = cls.task(key)
        return cls.advance('research_wait', {'task_ids': [task[0]]}) if task else None

    @classmethod
    def choose_node(cls, body):
        nid, key = cls.node(body)
        if nid in cls.finished_nodes:
            return None
        if not cls.rows('SELECT 1 FROM publications WHERE node_id=?', (nid,)):
            value = {'shared': 100, 'fast': 80, 'slow': 70, 'combo': 95, 'restored': 70,
                     'multi-a': 80, 'multi-b': 70}[key]
            return 'research_publish', {'status': 'complete', 'summary': 'Controlled fixture ' + key,
                'gaps': [], 'knowledge_refs': [], 'items': [{'item_id': 'data', 'kind': 'text',
                    'content': {'fixture': key, 'latency_ms': value, 'baseline': 'shared frozen measurement',
                                'workload': 'synthetic-fixed', 'environment': 'isolated-host',
                                'repeats': 3, 'uncertainty': 'deterministic fixture; not scientific evidence'}}]}
        if key == 'restored' and not cls.rows("SELECT 1 FROM knowledge_entries WHERE kind='claim'"):
            return 'research_record', {'kind': 'claim', 'statement': 'The isolated fixture restored the single-route measurement after combination degradation',
                'visibility': 'project', 'evidence_refs': [cls.result_ref(key)]}
        cls.finished_nodes.add(nid)
        return 'research_finish', {'state': 'finished', 'summary': 'Controlled fixture complete: ' + key}

    @classmethod
    def stop(cls):
        return {'reason_kind': 'sufficient_for_current_choice', 'reason': 'Stop within the synthetic fixture only',
            'scope_judgments': [{'scope_refs': [scope], 'reason': 'The bounded fixture has completed',
                'remaining_work': 'No external workload was measured', 'residual_uncertainty': 'No real-model or scientific-quality inference',
                'reactivation_condition': 'A separately authorized real workload'} for scope in cls.scopes()]}

    @classmethod
    def final(cls, body, first):
        offset = cls.stage - first
        claims = cls.rows("SELECT r.knowledge_id,MAX(r.revision) FROM knowledge_revisions r JOIN knowledge_entries e USING(knowledge_id) "
                          "WHERE e.kind='claim' GROUP BY r.knowledge_id")
        refs = [f'knowledge/{kid}@{rev}' for kid, rev in claims]
        if offset == 0:
            return cls.read(refs)
        if offset == 1:
            page = cls.last_page(body)
            return cls.advance('research_prepare_conclusion', {'snapshot_id': page['snapshot_id'],
                'core_claim_refs': refs, 'outcome': 'answered' if refs else 'partial',
                'gaps': [] if refs else ['The multi-scope fixture demonstrates engineering behavior only'],
                'risk_judgments': [], 'review_requirements': [], 'delivery_mode': 'paged', 'exploration_stop': cls.stop()})
        drafts = cls.rows('SELECT draft_id FROM conclusion_drafts ORDER BY rowid DESC LIMIT 1')
        if not drafts:
            raise RuntimeError('Public prepare tool did not create a conclusion draft')
        draft = drafts[0][0]
        if offset == 2:
            return cls.advance('research_publish', {'status': 'complete', 'summary': 'Isolated E3 engineering result',
                'gaps': [], 'knowledge_refs': refs, 'draft_id': draft, 'report_item_id': 'report',
                'items': [{'item_id': 'report', 'kind': 'text', 'content': {'text': 'Synthetic fixture only: asynchronous revision, independent combination test, and frozen-input restoration completed.'}}]})
        if offset == 3:
            return cls.advance('research_query', {'collection': 'evidence', 'draft_id': draft, 'limit': 16384})
        if offset == 4:
            page = cls.last_page(body, 'decision')
            if page.get('cursor'):
                return 'research_query', {'collection': 'evidence', 'cursor': page['cursor'], 'limit': 16384}
            final = cls.rows("SELECT publication_id FROM conclusion_publications WHERE draft_id=?", (draft,))[0][0]
            return cls.advance('research_conclude', {'draft_id': draft, 'final_ref': f'pub/{final}#report',
                'summary': 'The E3 engineering fixture completed within its declared synthetic scope'})
        return None

    @classmethod
    def choose_full(cls, body):
        stage = cls.stage
        if stage == 0:
            return cls.advance('research_scope', {'changes': {'objective': 'Controlled asynchronous strategy comparison', 'task_kind': 'selection'}})
        scope, routes = cls.current()
        if stage in (1, 2):
            return cls.advance('research_route', {'scope_ref': scope, 'changes': {'title': 'Route ' + ('A' if stage == 1 else 'B'),
                'rationale': 'Compare controlled candidates', 'critical_assumption': 'The same fixture conditions apply'}})
        if stage in (3, 6, 9, 12, 15, 18, 21):
            return cls.read()
        if stage == 4:
            return cls.advance('research_decide', {'scope_ref': scope, 'route_refs': routes,
                'snapshot_id': cls.last_page(body)['snapshot_id'], 'action': 'probe', 'reason': 'Share one public probe across both routes',
                'next_work': cls.work('shared', routes)})
        if stage in (5, 8, 11, 17, 20):
            if stage == 11:
                cls.late_release.set()
            return cls.wait({5: 'shared', 8: 'fast', 11: 'slow', 17: 'combo', 20: 'restored'}[stage])
        if stage == 7:
            return cls.advance('research_decide', {'scope_ref': scope, 'route_refs': routes,
                'snapshot_id': cls.last_page(body)['snapshot_id'], 'result_ids': cls.pending(), 'action': 'deepen',
                'reason': 'Compare independent routes after one shared probe',
                'work_batch': [cls.work('fast', [routes[0]], [cls.result_ref('shared')]),
                               cls.work('slow', [routes[1]], [cls.result_ref('shared')])]})
        if stage == 10:
            return cls.advance('research_decide', {'scope_ref': scope, 'route_refs': routes,
                'snapshot_id': cls.last_page(body)['snapshot_id'], 'result_ids': cls.pending(), 'action': 'deepen',
                'reason': 'Fast route supports only a provisional choice while the slow measurement is missing',
                'provisional': True, 'expected_results': [cls.task('fast')[3], cls.task('slow')[3]],
                'route_changes': [{'route_ref': routes[0], 'disposition': 'adopted', 'reason': 'Provisional fixture choice'}]})
        if stage == 13:
            previous = cls.rows("SELECT decision_id FROM exploration_decisions WHERE json_extract(data,'$.provisional')=1 ORDER BY rowid DESC LIMIT 1")[0][0]
            refs = [cls.result_ref('fast'), cls.result_ref('slow')]
            conditions = {'baseline_ref': cls.result_ref('shared'), 'workload': 'synthetic-fixed', 'metric': 'latency',
                'unit': 'ms', 'environment': 'isolated-host', 'repeats': 3, 'uncertainty': 'deterministic fixture only'}
            return cls.advance('research_decide', {'scope_ref': scope, 'route_refs': routes,
                'snapshot_id': cls.last_page(body)['snapshot_id'], 'result_ids': cls.pending(), 'action': 'redirect',
                'reason': 'Late valid slow-route evidence revises the provisional selection', 'revises_decision_id': previous,
                'comparisons': [{'result_refs': refs, 'conditions': [conditions, conditions], 'ranking': True,
                    'reason': 'Declared identical fixture conditions; this is an engineering fixture'}],
                'route_changes': [{'route_ref': routes[1], 'disposition': 'adopted', 'reason': 'Late synthetic measurement has lower latency'}]})
        if stage == 14:
            return cls.advance('research_route', {'scope_ref': scope, 'changes': {'title': 'A+B independently tested',
                'rationale': 'Combination must be measured rather than adding benefits', 'critical_assumption': 'Interactions may degrade latency',
                'component_refs': routes[:2], 'relations': [{'kind': 'combines', 'route_ref': ref} for ref in routes[:2]]}})
        if stage == 16:
            return cls.advance('research_decide', {'scope_ref': scope, 'route_refs': routes,
                'snapshot_id': cls.last_page(body)['snapshot_id'], 'action': 'combine', 'reason': 'Measure a new combination experiment',
                'next_work': cls.work('combo', [routes[2]], [cls.result_ref('fast'), cls.result_ref('slow')], 'combine')})
        if stage == 19:
            return cls.advance('research_decide', {'scope_ref': scope, 'route_refs': routes,
                'snapshot_id': cls.last_page(body)['snapshot_id'], 'result_ids': cls.pending(), 'action': 'restore',
                'reason': 'Combination degraded the fixture; restore the measured single route from its frozen output',
                'route_changes': [{'route_ref': routes[2], 'disposition': 'excluded', 'reason': 'Combination latency degraded from 70ms to 95ms',
                    'reactivation_condition': 'A changed workload and new interaction evidence'}],
                'next_work': cls.work('restored', [routes[1]], [cls.result_ref('slow')], 'restore')})
        if stage == 22:
            return cls.advance('research_decide', {'scope_ref': scope, 'route_refs': routes,
                'snapshot_id': cls.last_page(body)['snapshot_id'], 'result_ids': cls.pending(), 'action': 'propose_stop',
                'reason': 'Restoration is measured; stop within the synthetic workload'})
        return cls.final(body, 23)

    @classmethod
    def choose_multi(cls, body):
        stage = cls.stage
        if stage >= 10 and any(not cls.task(key) or cls.task(key)[2] != 'finished' for key in ('multi-a', 'multi-b')):
            # A tool call to research_wait ends the coordinator turn only when
            # this provider stops responding. Yield until the native wake.
            return None
        if stage in (0, 1):
            return cls.advance('research_scope', {'changes': {'objective': 'Independent active scope ' + str(stage + 1), 'task_kind': 'selection'}})
        scopes = cls.scopes()
        if stage in (2, 3):
            return cls.advance('research_route', {'scope_ref': scopes[stage - 2], 'changes': {
                'title': 'Scope candidate', 'rationale': 'Independent bounded work', 'critical_assumption': 'The local fixture applies'}})
        if stage in (4, 6, 10, 12):
            return cls.read(scope=scopes[0 if stage in (4, 10) else 1])
        if stage in (5, 7):
            scope = scopes[0 if stage == 5 else 1]
            routes = cls.local_routes(scope)
            return cls.advance('research_decide', {'scope_ref': scope, 'route_refs': routes,
                'snapshot_id': cls.last_page(body)['snapshot_id'], 'action': 'probe', 'reason': 'Independent active scope allocation',
                'next_work': cls.work('multi-a' if stage == 5 else 'multi-b', routes)})
        if stage in (8, 9):
            return cls.wait('multi-a' if stage == 8 else 'multi-b')
        if stage in (11, 13):
            scope = scopes[0 if stage == 11 else 1]
            routes = cls.local_routes(scope)
            return cls.advance('research_decide', {'scope_ref': scope, 'route_refs': routes,
                'snapshot_id': cls.last_page(body)['snapshot_id'], 'result_ids': cls.pending(scope.split('/')[-1].split('@')[0]),
                'action': 'propose_stop', 'reason': 'Stop this independently measured scope',
                'route_changes': [{'route_ref': ref, 'disposition': 'adopted', 'reason': 'Fixture execution completed'} for ref in routes]})
        if cls.scenario == 'paged' and 14 <= stage < 22:
            # Keep each public RPC below the existing 64KiB transport limit;
            # the aggregate decision still approaches the 256KiB delivery cap.
            return cls.advance('research_record', {'kind': 'claim', 'statement': f'Measurement {stage - 13}: ' + '结果🙂' * 3000,
                'visibility': 'project', 'evidence_refs': [cls.result_ref('multi-b')]})
        return cls.final(body, 22 if cls.scenario == 'paged' else 14)

    def do_POST(self):
        cls = type(self)
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
        names = {item.get('function', {}).get('name') for item in body.get('tools', []) if item.get('type') == 'function'}
        is_node = 'research_finish' in names
        # Do not hold the provider lock while delaying the slow route: the
        # coordinator must remain able to make its provisional choice.
        if is_node and cls.scenario == 'full':
            try:
                if cls.node(body)[1] == 'slow' and not cls.late_release.wait(50):
                    cls.failures.append('Slow route was never released by the provisional coordinator choice')
            except Exception as exc:
                cls.failures.append(str(exc))
        with cls.lock:
            try:
                for message in body.get('messages', []):
                    if message.get('role') != 'tool':
                        continue
                    marker = hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest()
                    if marker not in cls.observed_tools:
                        cls.observed_tools.add(marker)
                        cls.trace.append({'stage': cls.stage, 'node': cls.node(body)[0] if is_node else None,
                                          'provider_visible_tool_message': message})
                        for value in objects(message):
                            if isinstance(value.get('error'), (str, dict)):
                                if not is_node and 'exploration_basis_changed' in str(value['error']):
                                    # Publication/finish notifications can arrive
                                    # between query and CAS. A real coordinator
                                    # must read again, never weaken the check.
                                    cls.trace.append({'stage': cls.stage, 'retry': 'late evidence changed the selection snapshot'})
                                    cls.stage = cls.latest_read_stage
                                    continue
                                if not is_node and any(reason in str(value['error']).lower() for reason in (
                                        'conclusion_basis_changed', 'pending results must be considered')):
                                    cls.trace.append({'stage': cls.stage, 'retry': 'late evidence requires a new stopping decision and draft'})
                                    cls.stage = 10 if cls.scenario in {'multi', 'resource', 'paged'} else 21
                                    continue
                                raise RuntimeError('Public tool rejected the fixture action: ' + str(value['error'])[:800])
                choice = cls.choose_node(body) if is_node else (cls.choose_multi(body) if cls.scenario in {'multi', 'resource', 'paged'} else cls.choose_full(body)) if 'research_dispatch' in names else None
                action, args = choice if choice else (None, None)
                if action:
                    cls.actions.append(action)
                    cls.trace.append({'stage': cls.stage, 'node': cls.node(body)[0] if is_node else None,
                                      'provider_emitted_tool': action, 'arguments': args})
            except Exception as exc:
                cls.failures.append(f'{type(exc).__name__}: {exc}')
                action, args = None, None
        delta = {'tool_calls': [{'index': 0, 'id': 'call_' + uuid4().hex, 'type': 'function',
            'function': {'name': action, 'arguments': json.dumps(args)}}]} if action else {'role': 'assistant', 'content': 'Controlled fixture waiting'}
        chunks = [{'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe',
            'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
            {'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe',
             'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if action else 'stop'}],
             'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}]
        data = (''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plugin', type=Path, default=Path(__file__).resolve().parents[1] / 'plugin')
    parser.add_argument('--scenario', choices=('full', 'multi', 'resource', 'paged'), default='full')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    provider = E3Provider
    provider.scenario, provider.stage = args.scenario, 0
    provider.actions, provider.failures, provider.trace = [], [], []
    provider.observed_tools, provider.finished_nodes = set(), set()
    provider.late_release.clear()
    outcome = {'scenario': args.scenario, 'used_external_model': False, 'classification': 'actual OpenCode host + scripted loopback provider'}
    with tempfile.TemporaryDirectory(prefix='ari-e3-host-') as directory:
        root = Path(directory)
        workspace = root / 'project'
        workspace.mkdir()
        provider.database = workspace / '.research' / 'state.sqlite3'
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL') if key in os.environ}
        for key in ('DATA', 'CONFIG', 'STATE', 'CACHE'):
            path = root / key.lower()
            path.mkdir()
            env['XDG_' + key + '_HOME'] = str(path)
        password = uuid4().hex
        env.update(OPENCODE_SERVER_PASSWORD=password, OPENCODE_DISABLE_MODELS_FETCH='1', ARI_PROBE_KEY='test-only')
        version = subprocess.run(['opencode', '--version'], cwd=workspace, env=env,
                                 capture_output=True, text=True, timeout=10)
        outcome['opencode_version'] = version.stdout.strip() if version.returncode == 0 else 'unknown'
        plugin_root = args.plugin.resolve()
        source_files = sorted(path for path in plugin_root.rglob('*')
                              if path.is_file() and path.suffix in {'.py', '.ts', '.tsx', '.json'}
                              and (path.relative_to(plugin_root).parts[0] in {'src', 'python'}
                                   or path.relative_to(plugin_root).as_posix() in {'index.ts', 'rpc.ts', 'tui.tsx', 'package.json'})
                              and '__pycache__' not in path.relative_to(plugin_root).parts)
        identity = hashlib.sha256()
        for path in source_files:
            identity.update(str(path.relative_to(plugin_root)).encode() + b'\0' + path.read_bytes() + b'\0')
        outcome['plugin_source_digest'] = identity.hexdigest()
        outcome['plugin_source_files'] = len(source_files)
        fake = ThreadingHTTPServer(('127.0.0.1', 0), provider)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        # A wrapper changes only the documented concurrency option, without
        # editing or replacing the plugin loaded from the requested source/package.
        wrapper = root / 'concurrent-plugin'
        wrapper.mkdir()
        (wrapper / 'package.json').write_text(json.dumps({'name': 'ari-e3-fixture', 'type': 'module', 'exports': {'.': './index.js'}}))
        (wrapper / 'index.js').write_text('import research from ' + json.dumps((args.plugin.resolve() / 'index.ts').as_uri()) + ';\n'
            'export default {...research,setup(ctx){return research.setup({...ctx,options:{...ctx.options,concurrency:2}})}};\n')
        config = root / 'config' / 'opencode'
        config.mkdir(exist_ok=True)
        (config / 'opencode.jsonc').write_text(json.dumps({'plugins': [str(wrapper)], 'model': 'ari-probe/probe',
            'providers': {'ari-probe': {'name': 'isolated E3 fixture', 'env': ['ARI_PROBE_KEY'],
                'package': '@opencode/ai/providers/openai-compatible',
                'settings': {'baseURL': f'http://127.0.0.1:{fake.server_port}/v1'},
                'models': {'probe': {'name': 'probe', 'limit': {'context': 200000, 'output': 2048}}}}}}))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        with (root / 'server.log').open('w+') as log:
            process = subprocess.Popen(['opencode', 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
                cwd=workspace, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        if request(base, '/api/session/active', password)[0] == 200:
                            break
                    except OSError:
                        pass
                    time.sleep(.1)
                sid = 'ses_e3_' + uuid4().hex
                status, response = request(base, '/api/session', password, {'id': sid, 'title': 'isolated E3 ' + args.scenario,
                    'location': {'directory': str(workspace)}, 'model': {'providerID': 'ari-probe', 'id': 'probe'}})
                if status != 200:
                    raise RuntimeError('Could not create isolated coordinator: ' + str(response))
                def command(action, values):
                    until = time.monotonic() + 10
                    while True:
                        status, response = request(base, '/api/rpc/auto-research/command', password,
                            {'input': {'action': action, 'sessionID': sid, 'directory': str(workspace), 'args': values}})
                        if status == 200 or time.monotonic() > until:
                            break
                        time.sleep(.1)
                    if status != 200 or response.get('output', {}).get('error'):
                        raise RuntimeError(f'{action}: {status} {response}')
                command('init', {'goal': 'Isolated E3 engineering fixture'})
                command('budget', {'limit': 'turns', 'value': '120', 'reason': 'Bound the synthetic fixture'})
                command('budget', {'limit': 'work', 'value': '8', 'reason': 'One shared allowance across all scopes'})
                if args.scenario == 'resource':
                    command('budget', {'limit': 'resource', 'resource': 'gpu:fixture', 'value': '1',
                                       'reason': 'Serialize declared synthetic resource work'})
                command('exploration', {'mode': 'on'})
                command('auto', {})
                deadline = time.monotonic() + 100
                state = None
                resource_recovery = []
                while time.monotonic() < deadline:
                    if provider.failures:
                        raise RuntimeError(provider.failures[-1])
                    with sqlite3.connect(provider.database) as db:
                        state = db.execute('SELECT state FROM workflow_project WHERE id=1').fetchone()[0]
                        notices = db.execute("SELECT payload FROM workflow_notifications WHERE kind='resource_wait' "
                                             "AND state='delivered' ORDER BY rowid").fetchall() if args.scenario == 'resource' else []
                    if notices and not resource_recovery:
                        payload = json.loads(notices[-1][0])
                        blocker = next((item for item in payload['blockers'] if item['task_state'] == 'finished'), None)
                        if blocker:
                            assert blocker['state'] == 'uncertain'
                            resource_recovery.append({'delivered_notice': payload, 'operator_action': 'recover resource',
                                                      'verified_task': blocker['task_id']})
                            command('recover', {'kind': 'resource', 'task_id': blocker['task_id'],
                                                'reason': 'Synthetic fixture has no external device process; operator test attestation'})
                    if state in {'complete', 'paused', 'stopped'}:
                        break
                    time.sleep(.1)
                with sqlite3.connect(provider.database) as db:
                    db.row_factory = sqlite3.Row
                    decisions = [{**dict(row), 'data': json.loads(row['data'])} for row in db.execute('SELECT * FROM exploration_decisions ORDER BY rowid')]
                    tasks = [dict(row) for row in db.execute('SELECT task_id,node_id,session_id,state FROM exploration_tasks ORDER BY rowid')]
                    allocations = [dict(row) for row in db.execute('SELECT * FROM exploration_allocations ORDER BY rowid')]
                    bindings = [dict(row) for row in db.execute('SELECT * FROM exploration_bindings ORDER BY rowid')]
                    intents = [json.loads(row['details']) for row in db.execute(
                        "SELECT details FROM workflow_intents WHERE kind='exploration_work' ORDER BY rowid")]
                    conclusion = db.execute('SELECT data FROM conclusion_evidence ORDER BY rowid DESC LIMIT 1').fetchone()
                    scopes = [dict(row) for row in db.execute('SELECT scope_id,revision,data FROM exploration_scopes ORDER BY rowid')]
                outcome.update(run_state=state, stage=provider.stage, actions=provider.actions,
                    decisions=decisions, tasks=tasks, allocations=allocations, bindings=bindings, scopes=scopes, intents=intents,
                    evidence=json.loads(conclusion[0]) if conclusion else None,
                    **({'resource_recovery': resource_recovery} if args.scenario == 'resource' else {}))
                assert state == 'complete', f'Fixture did not conclude: state={state}, stage={provider.stage}'
                assert all(item['state'] == 'finished' for item in tasks)
                assert sum(item['amount'] for item in allocations) <= 8
                assert len(allocations) == len(tasks) == (5 if args.scenario == 'full' else 2)
                if args.scenario == 'full':
                    assert sum(item['node_id'] == tasks[0]['node_id'] for item in bindings) == 2
                    assert any(item['data']['provisional'] and item['data']['expected_missing'] for item in decisions)
                    revised = [item['data'] for item in decisions if item['data']['revises_decision_id']]
                    assert revised
                    # The provider did not repeat expected_results after the
                    # provisional decision. The late slow result must still be
                    # carried into the recorded revision by the backend.
                    slow_intent = provider.task('slow')[3]
                    assert any(slow_intent in item['expected_results'] and not item['expected_missing'] for item in revised)
                    combo = next(item for item in intents if item['next_work'].get('key') == 'combo')
                    assert combo['basis_mode'] == 'scope-work-v2'
                    assert set(combo['work_input_refs']) == set(combo['next_work']['inputs'])
                    choices = [item['data']['action'] for item in decisions]
                    assert choices.index('combine') < choices.index('restore')
                    assert choices[-1] == 'propose_stop'
                    assert all(action == 'propose_stop' for action in choices[choices.index('restore') + 1:])
                else:
                    assert len({item['scope_id'] for item in scopes}) == 2
                    assert len(outcome['evidence']['exploration_stop']['considered_scope_refs']) == 2
                if args.scenario == 'resource':
                    assert len(resource_recovery) == 1, 'No delivered resource wait notice and operator recovery observed'
                    assert allocations[0]['resource_state'] == 'released'
                    assert allocations[1]['resource_state'] == 'uncertain'
                    assert all(json.loads(item['resources']) == {'gpu:fixture': 1} for item in allocations)
                else:
                    assert all(item['resource_state'] == 'released' for item in allocations)
                if args.scenario == 'paged':
                    draft = provider.rows('SELECT data FROM conclusion_drafts ORDER BY rowid DESC LIMIT 1')[0][0]
                    manifest = json.loads(draft)['delivery_manifest']
                    assert 230000 < manifest['total_bytes'] <= 256 * 1024
                    pages = []
                    for event in provider.trace:
                        message = event.get('provider_visible_tool_message')
                        if not message:
                            continue
                        pages.extend(value for value in objects(message) if value.get('part') == 'decision')
                    assert len(pages) >= 14
                    assert all('delivery_manifest' not in page for page in pages if page['offset'] > 0)
                    outcome['paged_delivery'] = {'total_bytes': manifest['total_bytes'],
                        'manifest_pages': len(manifest['pages']), 'observed_decision_pages': len(pages),
                        'full_manifest_only_at_offset_zero': True, 'concluded_with_complete_current_request': True}
                outcome['passed'] = True
            except Exception as error:
                log.flush()
                log.seek(0)
                outcome.update(passed=False, error=f'{type(error).__name__}: {error}', stage=provider.stage,
                    actions=provider.actions, failures=provider.failures, host_log_tail=log.read()[-10000:])
                raise
            finally:
                provider.late_release.set()
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                fake.shutdown()
                fake.server_close()
                args.output.parent.mkdir(parents=True, exist_ok=True)
                trace_path = args.output.with_name(args.output.stem + '-trace.json')
                sanitize = lambda value: json.dumps(value, ensure_ascii=False, indent=2).replace(str(workspace), '<isolated-project>').replace(str(root), '<isolated-host>') + '\n'
                trace_path.write_text(sanitize(provider.trace), encoding='utf-8')
                outcome['trace'] = trace_path.name
                args.output.write_text(sanitize(outcome), encoding='utf-8')
    print(json.dumps({'passed': outcome.get('passed'), 'scenario': args.scenario, 'output': str(args.output), 'used_external_model': False}))


if __name__ == '__main__':
    main()
