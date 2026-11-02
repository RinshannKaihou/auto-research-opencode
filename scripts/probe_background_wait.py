"""Probe background shell hooks using a temporary OpenCode host and fake model."""
import argparse
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


class Model(BaseHTTPRequestHandler):
    calls = 0
    shell_args = {}

    def log_message(self, *_):
        pass

    def do_POST(self):
        type(self).calls += 1
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
        if type(self).calls == 1:
            delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'call_bg_probe',
                'type': 'function', 'function': {'name': 'shell', 'arguments': json.dumps(
                    type(self).shell_args)}}]}
            finish = 'tool_calls'
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'call_bg_probe', 'type': 'function', 'function': delta['tool_calls'][0]['function']}]}
        else:
            delta, finish = {'role': 'assistant', 'content': 'waiting for background task'}, 'stop'
            message = {'role': 'assistant', 'content': 'waiting for background task'}
        if body.get('stream'):
            chunks = [{'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe',
                       'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                      {'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe',
                       'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}],
                       'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}]
            data = (''.join('data: ' + json.dumps(item) + '\n\n' for item in chunks) + 'data: [DONE]\n\n').encode()
            kind = 'text/event-stream'
        else:
            data = json.dumps({'id': 'probe', 'object': 'chat.completion', 'created': 1, 'model': 'probe',
                'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
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
    req = urllib.request.Request(base + path, data=data,
        headers={'Content-Type': 'application/json', 'Authorization': f'Basic {auth}'})
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw) if raw else {}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--scenario',choices=['background','foreground-error','foreground-interrupt','foreground-denied'],default='background')
    args=parser.parse_args()
    Model.calls=0
    with tempfile.TemporaryDirectory(prefix='ari-bg-probe-') as temporary:
        root = Path(temporary)
        workspace = root / 'project'
        workspace.mkdir()
        pid_file=workspace/'probe.pid'
        Model.shell_args={'command':'sleep 2','background':True,'timeout':20000} if args.scenario=='background' else {
            'command':'exit 7' if args.scenario=='foreground-error' else f'echo $$ > {pid_file}; sleep 20', 'timeout':30000}

        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL') if key in os.environ}
        for name in ('DATA', 'CONFIG', 'STATE', 'CACHE'):
            directory = root / name.lower()
            directory.mkdir()
            env[f'XDG_{name}_HOME'] = str(directory)
        password = uuid4().hex
        env.update(OPENCODE_SERVER_PASSWORD=password, OPENCODE_DISABLE_MODELS_FETCH='1', ARI_PROBE_KEY='test-only')
        fake = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        trace = root / 'events.jsonl'
        env['ARI_PROBE_TRACE'] = str(trace)
        env['ARI_PROBE_SCENARIO'] = args.scenario
        plugin_dir = root / 'probe-plugin'
        plugin_dir.mkdir()
        (plugin_dir / 'package.json').write_text(json.dumps({'name': 'ari-bg-probe', 'type': 'module', 'exports': {'.': './index.js'}}))
        (plugin_dir / 'index.js').write_text('''import {appendFileSync} from "node:fs";
const log=x=>appendFileSync(process.env.ARI_PROBE_TRACE,JSON.stringify(x)+"\\n");
export default {id:"ari-bg-probe",async setup(ctx){let open=true;const stream=ctx.event.subscribe();
 await ctx.tool.hook('execute.before',r=>{if(r.tool==='shell'){log({type:'probe.tool.before',id:r.id,background:r.input?.background??false});if(process.env.ARI_PROBE_SCENARIO==='foreground-interrupt')setTimeout(()=>ctx.session.interrupt({sessionID:r.sessionID}),500)}});
 await ctx.tool.hook('execute.after',r=>{if(r.tool==='shell')log({type:'probe.tool.after',id:r.id,session:r.sessionID,status:r.status,metadata:r.result?.metadata})});
 await ctx.session.hook('prompt',r=>log({type:'probe.prompt',session:r.sessionID,metadata:r.metadata,delivery:r.delivery}));
 log({type:'probe.ready'});void (async()=>{try{for await(const e of stream){if(!open)break;
  if(e.data?.sessionID || e.type.startsWith('shell.'))log({type:e.type,id:e.id,data:e.data,seq:e.durable?.seq});
 }}catch{}})();return()=>{open=false};}};
''')
        config = root / 'config' / 'opencode'
        config.mkdir()
        (config / 'opencode.jsonc').write_text(json.dumps({'plugins': [str(plugin_dir)], 'model': 'ari-probe/probe',
            'providers': {'ari-probe': {'name': 'isolated test', 'env': ['ARI_PROBE_KEY'],
                'package': '@opencode/ai/providers/openai-compatible',
                'settings': {'baseURL': f'http://127.0.0.1:{fake.server_port}/v1'},
                'models': {'probe': {'name': 'probe', 'limit': {'context': 200000, 'output': 1024}}}}}}))
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
                        if request(base, '/api/session/active', password)[0] == 200:
                            break
                    except (OSError, urllib.error.URLError):
                        pass
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('Isolated host did not start')
                    time.sleep(0.1)
                suffix = uuid4().hex
                sid, mid = 'ses_probe_' + suffix, 'msg_probe_' + suffix
                created, _ = request(base, '/api/session', password,
                    {'id': sid, 'title': 'background probe', 'location': {'directory': str(workspace)},
                     'model': {'providerID': 'ari-probe', 'id': 'probe'},
                     **({'permissions':[{'action':'shell','resource':'*','effect':'deny'}]} if args.scenario=='foreground-denied' else {})})
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and (not trace.exists() or 'probe.ready' not in trace.read_text()):
                    time.sleep(0.05)
                time.sleep(0.5)
                submitted, _ = request(base, f'/api/session/{sid}/synthetic', password,
                    {'id': mid, 'text': 'Use the shell tool once, then wait.', 'resume': True})
                deadline = time.monotonic() + 15
                events = []
                while time.monotonic() < deadline:
                    if trace.exists():
                        events = [json.loads(line) for line in trace.read_text().splitlines() if line]
                    if (args.scenario=='background' and any(x['type'] == 'shell.exited' for x in events) and Model.calls>=3
                        or args.scenario!='background' and any(x['type'] in {'probe.tool.after','session.tool.failed'} for x in events) and
                        any(x['type'] in {'session.execution.succeeded','session.execution.interrupted'} for x in events)):
                        break
                    time.sleep(0.1)
                _, context = request(base, f'/api/session/{sid}/context', password)
                summary = []
                for item in context.get('data', []):
                    if item.get('type') == 'synthetic':
                        summary.append({'type': 'synthetic', 'id': item.get('id'), 'metadata': item.get('metadata')})
                alive=None
                if pid_file.exists():
                    try:
                        os.kill(int(pid_file.read_text().strip()),0)
                        alive=True
                    except ProcessLookupError:
                        alive=False
                print(json.dumps({'scenario':args.scenario,'foreground_pid_alive':alive,'created': created, 'submitted': submitted, 'model_calls': Model.calls,
                    'events': events, 'synthetic_messages': summary}, ensure_ascii=False).replace(str(root),'<tmp>').replace(str(root.resolve()),'<tmp>'))
                after = next((x for x in events if x['type'] == 'probe.tool.after'), None)
                shell_id = (after or {}).get('metadata', {}).get('shellID')
                enqueued = next((x for x in events if x['type'] == 'session.inbox.enqueued' and
                                 x.get('data', {}).get('item', {}).get('payload', {}).get('metadata', {}).get('shellID') == shell_id), None)
                delivered = enqueued and any(x['type'] == 'session.inbox.delivered' and
                    x.get('data', {}).get('inboxID') == enqueued['data']['inboxID'] for x in events)
                failed = next((x for x in events if x['type']=='session.tool.failed' and x.get('data',{}).get('id')=='call_bg_probe'), None)
                if args.scenario=='background':
                    valid=Model.calls>=3 and shell_id and after['metadata'].get('status')=='running' and delivered
                elif args.scenario=='foreground-interrupt':
                    valid=failed and failed['data']['error']['type']=='aborted' and alive is False
                elif args.scenario=='foreground-denied':
                    valid=failed and failed['data']['error']['type']=='tool.execution' and alive is None
                else:
                    valid=after and after['status']=='completed' and (after.get('metadata') or {}).get('status')!='running' and alive is not True
                if (created != 200 or submitted != 200 or not valid):
                    log.flush()
                    print((root / 'server.log').read_text()[-4000:])
                    raise SystemExit(f'{args.scenario}: host lifecycle was not correlated')
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
