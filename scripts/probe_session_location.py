"""OpenCode 2.2.5/2.2.6 location regression, using private XDG state and a loopback model.

Modes: reproduce the installed bug; block nested/direct moves; diagnose an
out-of-band move and recover the same session; filter the actual model catalog. Never uses a real research DB.
Artifacts persist in --output (which must be a new directory).
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import threading
import time
from uuid import uuid4

from probe_opencode_execution import request


def eventually(check, label, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('timed out: ' + label)


def decoded_values(value):
    if isinstance(value, str):
        try:
            yield from decoded_values(json.loads(value))
        except (ValueError, TypeError):
            pass
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            yield from decoded_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from decoded_values(child)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin', required=True, type=Path)
    parser.add_argument('--opencode', default='opencode', help='isolated host executable (defaults to PATH)')
    parser.add_argument('--mode', choices=['baseline', 'nested', 'direct', 'drift', 'catalog'], required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    project = root / 'project'
    workspace = project / 'workspaces' / 'drift'
    workspace.mkdir(parents=True)
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL') if k in os.environ}
    for name in ['DATA', 'CONFIG', 'STATE', 'CACHE']:
        directory = root / name.lower()
        directory.mkdir()
        env['XDG_' + name + '_HOME'] = str(directory)
    password = uuid4().hex
    env.update(OPENCODE_SERVER_PASSWORD=password, OPENCODE_DISABLE_MODELS_FETCH='1', ARI_PROBE_KEY='test-only')
    version = subprocess.check_output([args.opencode, '--version'], env=env, text=True).strip()
    assert version.removeprefix('opencode v') in ('2.2.5', '2.2.6'), version
    trace = root / 'trace.jsonl'
    env['ARI_LOCATION_TRACE'] = str(trace)
    instrumentation = root / 'trace-plugin'
    instrumentation.mkdir()
    (instrumentation / 'package.json').write_text(json.dumps({'name': 'location-trace', 'type': 'module', 'exports': {'.': './index.js'}}))
    (instrumentation / 'index.js').write_text('''import {appendFileSync} from 'node:fs';
export default {id:'location-trace',async setup(ctx){
 const log=x=>appendFileSync(process.env.ARI_LOCATION_TRACE,JSON.stringify({directory:ctx.location.directory,...x})+'\\n');
 log({type:'ready'});
 await ctx.tool.hook('execute.before',r=>log({type:'before',...r}));
 await ctx.tool.hook('execute.after',r=>log({type:'after',...r}));
 await ctx.session.hook('context',r=>log({type:'context',sessionID:r.sessionID,tools:Object.keys(r.tools)}));
 let on=true;const abort=new AbortController();
 void(async()=>{try{for await(const e of ctx.event.subscribe({signal:abort.signal})){if(!on)break;
  if(e.data?.sessionID)log({type:e.type,id:e.id,data:e.data});}}catch{}})();
 return()=>{on=false;abort.abort()};}}
''')
    node_gate = threading.Event()
    node_waiting = threading.Event()
    actions = []
    catalogs = []
    provider_errors = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with (root / 'requests.jsonl').open('a') as f:
                    f.write(json.dumps(body) + '\n')
                names = [t['function']['name'] for t in body.get('tools', []) if t['type'] == 'function']
                catalogs.append(names)
                action, values = None, None
                if 'research_dispatch' in names:
                    if 'propose' not in actions:
                        actions.append('propose')
                        action, values = 'research_propose', {'question': 'location probe', 'why_now': 'test', 'plan': 'test', 'root_reason': 'test'}
                    elif 'dispatch' not in actions:
                        actions.append('dispatch')
                        action, values = 'research_dispatch', {'node_id': 'X-001'}
                elif 'research_finish' in names:
                    if args.mode == 'drift' and 'external_move' not in actions:
                        node_waiting.set()
                        assert node_gate.wait(20), 'drift gate'
                        actions.append('external_move')
                    elif args.mode in ('baseline', 'nested', 'direct') and 'move' not in actions:
                        actions.append('move')
                        if args.mode == 'direct':
                            action, values = 'opencode_session_move', {'directory': str(workspace)}
                        else:
                            # Dynamic lookup proves the guard is not a JS source-text matcher.
                            action, values = 'execute', {'code': 'const k = "session_" + "move"; await tools.opencode[k]({directory:' + json.dumps(str(workspace)) + '})'}
                    elif 'query' not in actions:
                        actions.append('query')
                        action, values = 'research_query', {'collection': 'checkpoint'}
                    elif 'publish' not in actions:
                        actions.append('publish')
                        action, values = 'research_publish', {'status': 'complete', 'summary': 'isolated publication', 'items': []}
                    elif 'checkpoint' not in actions:
                        actions.append('checkpoint')
                        candidates = [v['base'] for v in decoded_values(body['messages']) if 'base' in v]
                        assert candidates, 'checkpoint response must deliver base'
                        action, values = 'research_checkpoint', {'base': candidates[-1], 'state': {'next': 'finish'}}
                    elif 'finish' not in actions:
                        actions.append('finish')
                        action, values = 'research_finish', {'state': 'finished', 'summary': 'isolated location test completed'}
                delta = {'tool_calls': [{'index': 0, 'id': 'call_' + str(time.time_ns()), 'type': 'function', 'function': {'name': action, 'arguments': json.dumps(values)}}]} if action else {'role': 'assistant', 'content': 'isolated turn complete'}
                chunks = [
                    {'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                    {'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if action else 'stop'}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}},
                ]
                data = (''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as error:
                provider_errors.append(repr(error))
                raise

    fake = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    config = root / 'config' / 'opencode'
    config.mkdir(exist_ok=True)
    plugins = [str(args.plugin.resolve()), str(instrumentation)]
    if args.mode == 'catalog':
        # A host-side permission/filter removes the tool from the context map,
        # while the registered tool table remains complete. Restore it by flag.
        stripper = root / 'catalog-filter'
        stripper.mkdir()
        env['ARI_CATALOG_RESTORED'] = str(root / 'catalog-restored')
        (stripper / 'package.json').write_text(json.dumps({'name': 'catalog-filter', 'type': 'module', 'exports': {'.': './index.js'}}))
        (stripper / 'index.js').write_text("import {existsSync} from 'node:fs'; export default {id:'catalog-filter',async setup(ctx){await ctx.session.hook('context',r=>{if(!existsSync(process.env.ARI_CATALOG_RESTORED))delete r.tools.research_finish})}}")
        plugins.insert(0, str(stripper))
    (config / 'opencode.jsonc').write_text(json.dumps({
        'plugins': plugins, 'model': 'ari-probe/probe',
        'providers': {'ari-probe': {'env': ['ARI_PROBE_KEY'], 'package': '@opencode/ai/providers/openai-compatible',
            'settings': {'baseURL': f'http://127.0.0.1:{fake.server_port}/v1'}, 'models': {'probe': {'limit': {'context': 200000, 'output': 2048}}}}},
    }))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    log = (root / 'server.log').open('w')
    process = subprocess.Popen([args.opencode, 'serve', '--hostname', '127.0.0.1', '--port', str(port)], cwd=project, env=env, stdout=log, stderr=log)
    database = project / '.research' / 'state.sqlite3'
    sid = 'ses_location_probe_' + uuid4().hex

    def command(action, values=None):
        status, value = request(url, '/api/rpc/auto-research/command', password, {'input': {'action': action, 'sessionID': sid, 'directory': str(project), 'args': values or {}}})
        assert status == 200, (status, value)
        return value.get('output', {})

    def rows(sql):
        if not database.exists():
            return []
        with sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True) as db:
            return db.execute(sql).fetchall()

    result = {'host': version, 'mode': args.mode, 'plugin': str(args.plugin.resolve()), 'evidence': str(root)}
    try:
        def host_ready():
            try:
                return request(url, '/api/session/active', password)[0] == 200
            except OSError:
                return False
        eventually(host_ready, 'private host startup')
        status, _ = request(url, '/api/session', password, {'id': sid, 'title': 'location test', 'location': {'directory': str(project)}, 'model': {'id': 'probe', 'providerID': 'ari-probe'}})
        assert status == 200
        for action, values in [('init', {'goal': 'isolated location regression'}), ('budget', {'limit': 'turns', 'value': '5', 'reason': 'bounded fixture'}), ('auto', {})]:
            value = command(action, values)
            assert not value.get('error'), value
        child = eventually(lambda: rows("SELECT session_id FROM exploration_tasks WHERE substr(session_id,1,4)='ses_'") or None, 'node dispatch')[0][0]
        if args.mode == 'drift':
            assert node_waiting.wait(15), provider_errors
            status, value = request(url, f'/api/session/{child}/move', password, {'directory': str(workspace)})
            assert status == 204, (status, value)
            node_gate.set()
            eventually(lambda: rows("SELECT pause_reason FROM workflow_sessions WHERE role='node_core'") == [('fault',)], 'first-turn drift pause')
            before = rows("SELECT count(*) FROM runtime_budget_reservations")
            refused = command('resume')
            assert 'research_session_location' in refused.get('error', ''), refused
            assert rows("SELECT count(*) FROM runtime_budget_reservations") == before
            # Exercise the moved instance itself. A context failure must abort
            # before the local provider sees an ordinary downgraded request.
            count_before = len(catalogs)
            status, value = request(url, f'/api/session/{child}/prompt', password, {'text': 'check location diagnosis', 'resume': True})
            assert status == 200, (status, value)
            eventually(lambda: len(catalogs) > count_before, 'diagnostic model request')
            assert len(catalogs) > count_before and all(not names for names in catalogs[count_before:]), 'drifted request must have zero tools and a diagnosis'
            assert 'research_session_location' in (root / 'requests.jsonl').read_text()
            result['resume_refused'] = refused['error']
            status, value = request(url, f'/api/session/{child}/move', password, {'directory': str(project)})
            assert status == 204, (status, value)
            status, value = request(url, f'/api/session/{child}/prompt', password, {'text': 'confirm explicit return to research root', 'resume': True})
            assert status == 200, (status, value)
            eventually(lambda: Path(request(url, f'/api/session/{child}', password)[1]['data']['location']['directory']).resolve() == project.resolve(), 'host confirms return to root')
            recovered = command('resume')
            assert not recovered.get('error'), recovered
        if args.mode == 'catalog':
            eventually(lambda: rows("SELECT pause_reason FROM workflow_sessions WHERE role='node_core'") == [('fault',)], 'model catalog gap pause')
            eventually(lambda: any(json.loads(line)['type'] == 'session.execution.succeeded' and json.loads(line).get('data', {}).get('sessionID') == child for line in trace.read_text().splitlines()), 'diagnostic response ends')
            assert any(not names for names in catalogs), 'fault request must have zero tools'
            assert 'research_tool_entry_unavailable' in (root / 'requests.jsonl').read_text()
            assert rows("SELECT count(*) FROM runtime_budget_reservations WHERE session_id='" + child + "'") == [(1,)], 'no automatic second turn'
            assert not command('budget').get('error'), 'read-only budget remains available'
            (root / 'catalog-restored').touch()
            assert not command('resume').get('error'), 'explicit resume after restoring visibility'
            result['actual_model_catalog_gap_paused_without_continuation'] = True
        if args.mode == 'baseline':
            eventually(lambda: any('research_finish' not in names and 'shell' in names and 'subagent' in names for names in catalogs), 'tool loss after migration')
            eventually(lambda: rows("SELECT pause_reason FROM workflow_sessions WHERE role='node_core'") == [('no_progress',)], 'original idle loop')
            result['reproduced_tool_loss_and_no_progress'] = True
        else:
            eventually(lambda: rows("SELECT state FROM exploration_tasks") == [('finished',)], 'same node finishes', timeout=25)
            assert rows('SELECT count(*) FROM publications') == [(1,)]
            assert rows('SELECT count(*) FROM node_checkpoints') == [(1,)]
            result['publication_checkpoint_finish'] = True
            result['task_count'] = rows('SELECT count(*) FROM exploration_tasks')[0][0]
            if args.mode in ('nested', 'direct'):
                trace_text = trace.read_text()
                assert 'research_session_move_unsupported' in trace_text, 'must hit actual host hook'
                result['move_blocked_at_host_hook'] = True
            status, session = request(url, f'/api/session/{child}', password)
            assert status == 200, (status, session)
            assert Path(session['data']['location']['directory']).resolve() == project.resolve(), session
        assert not provider_errors, provider_errors
        result.update(actions=actions, model_requests=len(catalogs), passed=True)
    except BaseException as error:
        result.update(passed=False, error=repr(error), provider_errors=provider_errors, actions=actions)
        raise
    finally:
        node_gate.set()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        fake.shutdown()
        log.close()
        (root / 'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
