"""Exercise execution/inbox events against an isolated loopback fake model.

No shared configuration, research data, credentials or external model service is used.
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from uuid import uuid4


class FakeModel(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *_args):
        pass

    def do_POST(self):
        type(self).calls += 1
        length = int(self.headers.get('Content-Length', '0'))
        body = json.loads(self.rfile.read(length) or b'{}')
        chunks = [
            {'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe',
             'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'probe result'}, 'finish_reason': None}]},
            {'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe',
             'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
             'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}},
        ]
        if body.get('stream'):
            data = (''.join('data: ' + json.dumps(item) + '\n\n' for item in chunks) + 'data: [DONE]\n\n').encode()
            kind = 'text/event-stream'
        else:
            data = json.dumps({'id': 'probe', 'object': 'chat.completion', 'created': 1, 'model': 'probe',
                               'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'probe result'}, 'finish_reason': 'stop'}],
                               'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}).encode()
            kind = 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def request(base, path, password, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    auth = base64.b64encode(f'opencode:{password}'.encode()).decode()
    req = urllib.request.Request(base + path, data=data, headers={'Content-Type': 'application/json', 'Authorization': f'Basic {auth}'})
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw) if raw else {}


def main():
    FakeModel.calls = 0
    with tempfile.TemporaryDirectory(prefix='ari-execution-probe-') as temporary:
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
        fake = ThreadingHTTPServer(('127.0.0.1', 0), FakeModel)
        thread = threading.Thread(target=fake.serve_forever, daemon=True)
        thread.start()
        trace = root / 'events.jsonl'
        env['ARI_PROBE_TRACE'] = str(trace)
        plugin_dir = root / 'event-plugin'
        plugin_dir.mkdir()
        (plugin_dir / 'package.json').write_text(json.dumps({'name': 'ari-event-probe', 'type': 'module', 'exports': {'.': './index.js'}}))
        (plugin_dir / 'index.js').write_text('''import {appendFileSync} from "node:fs";
export default {id:"ari-event-probe",async setup(ctx){let open=true;const stream=ctx.event.subscribe();
 appendFileSync(process.env.ARI_PROBE_TRACE,JSON.stringify({type:"probe.ready",directory:ctx.location.directory})+"\\n");
 void (async()=>{try{for await(const e of stream){if(!open)break;
  if(e.data?.sessionID)
   appendFileSync(process.env.ARI_PROBE_TRACE,JSON.stringify({type:e.type,session:e.data?.sessionID,inbox:e.data?.inboxID,seq:e.durable?.seq})+"\\n");
 }}catch{}})(); return ()=>{open=false};}};
''')
        config = root / 'config' / 'opencode'
        config.mkdir()
        (config / 'opencode.jsonc').write_text(json.dumps({
            'plugins': [str(plugin_dir)], 'model': 'ari-probe/probe',
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
                suffix = uuid4().hex
                sid, mid = 'ses_probe_' + suffix, 'msg_probe_' + suffix
                created, _ = request(base, '/api/session', password,
                    {'id': sid, 'title': 'isolated execution probe', 'location': {'directory': str(workspace)},
                     'model': {'providerID': 'ari-probe', 'id': 'probe'}})
                ready_deadline = time.monotonic() + 5
                while time.monotonic() < ready_deadline:
                    if trace.exists() and 'probe.ready' in trace.read_text():
                        break
                    time.sleep(0.05)
                # The iterable registers asynchronously after setup returns.
                time.sleep(0.5)
                submitted, reply = request(base, f'/api/session/{sid}/synthetic', password,
                    {'id': mid, 'text': 'isolated test', 'resume': True})
                events = []
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if trace.exists():
                        events = [json.loads(line) for line in trace.read_text().splitlines() if line]
                    if any(item['type'] in {'session.execution.succeeded', 'session.execution.failed'} and item.get('session') == sid for item in events):
                        break
                    time.sleep(0.1)
                second_id = mid + '_second'
                second_status, _ = request(base, f'/api/session/{sid}/synthetic', password,
                    {'id': second_id, 'text': 'second isolated test', 'resume': True})
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if trace.exists():
                        events = [json.loads(line) for line in trace.read_text().splitlines() if line]
                    if sum(item['type'] in {'session.execution.succeeded', 'session.execution.failed'} and item.get('session') == sid for item in events) >= 2:
                        break
                    time.sleep(0.1)
                context_status, context = request(base, f'/api/session/{sid}/context', password)
                own = [item for item in events if item.get('session') == sid]
                messages = context.get('data', [])
                result = {'create_status': created, 'submit_status': submitted,
                          'second_submit_status': second_status,
                          'same_inbox_id': reply.get('data', {}).get('id') == mid,
                          'fake_model_calls': FakeModel.calls, 'events': own,
                          'context_status': context_status, 'context_types': [item.get('type') for item in messages],
                          'terminal_outcomes': [item.get('outcome') for item in messages if item.get('type') == 'idle'],
                          'kickoff_in_context': any(item.get('id') == mid for item in messages)}
                print(json.dumps(result, sort_keys=True))
                if created != 200 or submitted != 200 or second_status != 200 or FakeModel.calls < 2 or sum(item['type'] == 'session.execution.succeeded' for item in own) < 2:
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
