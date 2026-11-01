"""Model-free scripted provider probe of the actual plugin's coordinator boundary."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
from uuid import uuid4

from probe_opencode_execution import request


class ScriptedProvider(BaseHTTPRequestHandler):
    mode = 'baseline'
    tool = None
    sentinel = None
    attempted = set()
    advertised = {}

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
        content = json.dumps(body.get('messages', []), ensure_ascii=False)
        mode = ('coordinator' if 'test coordinator boundary' in content else
                'baseline_action' if 'baseline execution probe' in content else 'baseline')
        names = [item.get('function', {}).get('name') for item in body.get('tools', []) if item.get('type') == 'function']
        type(self).advertised.setdefault(mode, names)
        tool_call = mode in {'coordinator', 'baseline_action'} and mode not in type(self).attempted
        if tool_call:
            type(self).attempted.add(mode)
            target = type(self).sentinel if mode == 'coordinator' else type(self).sentinel.parent / 'ordinary-side-effect'
            command = 'touch ' + str(target)
            delta = {'tool_calls': [{'index': 0, 'id': 'call_probe', 'type': 'function',
                                     'function': {'name': type(self).tool,
                                                  'arguments': json.dumps({'command': command, 'description': 'isolated probe'})}}]}
        else:
            delta = {'role': 'assistant', 'content': 'probe complete'}
        chunks = [
            {'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe',
             'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
            {'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe',
             'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if tool_call else 'stop'}],
             'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}},
        ]
        data = (''.join('data: ' + json.dumps(item) + '\n\n' for item in chunks) + 'data: [DONE]\n\n').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    force_tool = '--force-tool' in sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix='ari-boundary-probe-') as temporary:
        root = Path(temporary)
        workspace = root / 'project'
        workspace.mkdir()
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL') if key in os.environ}
        for name in ('DATA', 'CONFIG', 'STATE', 'CACHE'):
            directory = root / name.lower()
            directory.mkdir()
            env[f'XDG_{name}_HOME'] = str(directory)
        password = uuid4().hex
        env.update(OPENCODE_SERVER_PASSWORD=password, OPENCODE_DISABLE_MODELS_FETCH='1', ARI_PROBE_KEY='test-only')
        fake = ThreadingHTTPServer(('127.0.0.1', 0), ScriptedProvider)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        ScriptedProvider.mode = 'baseline'
        ScriptedProvider.advertised = {}
        ScriptedProvider.attempted = set()
        ScriptedProvider.sentinel = root / 'unexpected-side-effect'
        config = root / 'config' / 'opencode'
        config.mkdir()
        plugin = Path(__file__).resolve().parents[1] / 'plugin'
        suffix = uuid4().hex
        ordinary = 'ses_plain_' + suffix
        coordinator = 'ses_main_' + suffix
        plugins = [str(plugin)]
        force_flag = root / 'force-tool'
        if force_tool:
            facade = root / 'facade-plugin'
            facade.mkdir()
            (facade / 'package.json').write_text(json.dumps({'name': 'ari-facade-probe', 'type': 'module', 'exports': {'.': './index.js'}}))
            (facade / 'index.js').write_text('''import {existsSync} from "node:fs";
export default {id:"ari-facade-probe",async setup(ctx){let saved;
 await ctx.session.hook("context",r=>{if(!existsSync(process.env.ARI_FORCE_FLAG)){if(r.tools.shell)saved=r.tools.shell;}
 else if(r.sessionID===process.env.ARI_COORDINATOR && saved)r.tools.shell=saved;}); return ()=>{};}};
''')
            env.update(ARI_FORCE_FLAG=str(force_flag), ARI_COORDINATOR=coordinator)
            plugins.append(str(facade))
        (config / 'opencode.jsonc').write_text(json.dumps({
            'plugins': plugins, 'model': 'ari-probe/probe',
            'providers': {'ari-probe': {'name': 'isolated test', 'env': ['ARI_PROBE_KEY'],
                'package': '@opencode/ai/providers/openai-compatible',
                'settings': {'baseURL': f'http://127.0.0.1:{fake.server_port}/v1'},
                'models': {'probe': {'name': 'probe', 'limit': {'context': 200000, 'output': 1024}}}}},
        }))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        with (root / 'server.log').open('w+') as log:
            process = subprocess.Popen(['opencode', 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
                                       cwd=workspace, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        status, _ = request(base, '/api/session/active', password)
                        if status == 200:
                            break
                    except (OSError, urllib.error.URLError):
                        pass
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('Isolated host did not start')
                    time.sleep(0.1)
                model = {'providerID': 'ari-probe', 'id': 'probe'}
                for sid in (ordinary, coordinator):
                    status, _ = request(base, '/api/session', password,
                        {'id': sid, 'title': 'isolated probe', 'location': {'directory': str(workspace)}, 'model': model})
                    if status != 200:
                        raise RuntimeError('Could not create test session')
                request(base, f'/api/session/{ordinary}/synthetic', password,
                        {'id': 'msg_plain_' + suffix, 'text': 'baseline', 'resume': True})
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    _, ordinary_context = request(base, f'/api/session/{ordinary}/context', password)
                    ordinary_messages = ordinary_context.get('data', [])
                    if 'baseline' in ScriptedProvider.advertised and ordinary_messages and ordinary_messages[-1].get('type') == 'idle':
                        break
                    time.sleep(0.1)
                baseline = ScriptedProvider.advertised.get('baseline', [])
                selected = next((name for name in ('shell', 'bash') if name in baseline), None)
                if not selected:
                    print(json.dumps({'baseline_tools': baseline, 'error': 'native execution tool was not advertised'}))
                    raise SystemExit(1)
                ScriptedProvider.tool = selected
                request(base, f'/api/session/{ordinary}/synthetic', password,
                        {'id': 'msg_plain_action_' + suffix, 'text': 'baseline execution probe', 'resume': True})
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if (root / 'ordinary-side-effect').exists():
                        break
                    time.sleep(0.1)
                if not (root / 'ordinary-side-effect').exists():
                    print(json.dumps({'error': 'ordinary session did not execute the control tool', 'tool': selected}))
                    raise SystemExit(1)
                deadline = time.monotonic() + 10
                rpc = None
                while time.monotonic() < deadline:
                    status, rpc = request(base, '/api/rpc/auto-research/command', password,
                        {'input': {'action': 'init', 'sessionID': coordinator, 'directory': str(workspace), 'args': {'goal': 'isolated boundary probe'}}})
                    if status == 200:
                        break
                    time.sleep(0.1)
                if status != 200 or rpc.get('output', {}).get('error'):
                    print(json.dumps({'rpc_status': status, 'rpc_error': rpc.get('output', {}).get('error')}))
                    raise SystemExit(1)
                if force_tool:
                    force_flag.touch()
                sent, _ = request(base, f'/api/session/{coordinator}/synthetic', password,
                                  {'id': 'msg_main_' + suffix, 'text': 'test coordinator boundary', 'resume': True})
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    _, context = request(base, f'/api/session/{coordinator}/context', password)
                    messages = context.get('data', [])
                    if 'coordinator' in ScriptedProvider.attempted and messages and messages[-1].get('type') == 'idle':
                        break
                    time.sleep(0.1)
                names = ScriptedProvider.advertised.get('coordinator', [])
                result = {'rpc_status': status, 'submit_status': sent, 'tool': selected,
                          'registered_role': rpc.get('output', {}).get('state', {}).get('workflow', {}).get('session', {}).get('role'),
                          'baseline_advertised_tool': selected in baseline,
                          'ordinary_tool_created_sentinel': (root / 'ordinary-side-effect').exists(),
                          'coordinator_advertised_tool': selected in names,
                          'forced_after_filter': force_tool,
                          'scripted_tool_call_attempted': 'coordinator' in ScriptedProvider.attempted,
                          'sentinel_created': ScriptedProvider.sentinel.exists(),
                          'terminal_outcomes': [item.get('outcome') for item in messages if item.get('type') == 'idle']}
                print(json.dumps(result, sort_keys=True))
                if (sent != 200 or 'coordinator' not in ScriptedProvider.attempted or (selected in names) != force_tool
                        or ScriptedProvider.sentinel.exists()):
                    raise SystemExit(1)
            finally:
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
