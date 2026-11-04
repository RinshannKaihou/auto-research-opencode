"""Check auxiliary routing and reasoning replay for the opt-in tail placement.

Uses the same private-server/local-provider pattern as ``probe_research_run.py``:
an isolated XDG tree, a loopback OpenAI-compatible provider, fake credentials and
no external model. The instrumentation plugin records which session hooks the
installed host invokes; the provider records the final request bodies.

Two checks:

1. Compaction. One primary request calls a research tool, then the host runs a
   real manual compaction, then another primary request runs. The temporary
   tail must appear on both primary requests and not in the compaction request
   or the persisted summary/history.
2. Reasoning. The installed OpenAI-compatible adapter lowers an assistant
   reasoning part with a native signature and tool call, with and without a
   trailing temporary user message. The wire body must preserve the reasoning
   text, signature metadata, tool IDs and result ordering. This is wire
   preservation only, not provider acceptance.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from probe_opencode_execution import request

TAIL_MARKER = 'Injected research state'


def message_text(message):
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(str(part.get('text', '')) for part in content if isinstance(part, dict))
    return ''


def has_tail(body):
    return bool(body.get('messages')) and message_text(body['messages'][-1]).startswith('<research_state>')


def tool_result_ids(body):
    return [message.get('tool_call_id') for message in body.get('messages') or []
            if message.get('role') == 'tool']


def tool_names(body):
    return {(tool.get('function') or {}).get('name') for tool in body.get('tools') or []}


def is_compaction_body(body):
    last = message_text((body.get('messages') or [{}])[-1])
    return 'summarize the conversation above' in last or 'required summary template' in last


class BoundaryProvider(BaseHTTPRequestHandler):
    bodies = []
    primary_tool_sent = False

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
        self.bodies.append(body)
        names = tool_names(body)
        messages = body.get('messages') or []
        last_role = messages[-1].get('role') if messages else None
        action = None
        args = None
        if 'research_query' in names and not type(self).primary_tool_sent:
            type(self).primary_tool_sent = True
            calls = [
                {'index': 0, 'id': 'call_probe_query', 'type': 'function',
                 'function': {'name': 'research_query', 'arguments': json.dumps({'collection': 'nodes'})}},
                {'index': 1, 'id': 'call_probe_wait', 'type': 'function',
                 'function': {'name': 'research_wait', 'arguments': json.dumps({'task_ids': ['T_probe_missing']})}},
            ]
            delta = {'tool_calls': calls}
            finish = 'tool_calls'
        elif is_compaction_body(body):
            delta = {'role': 'assistant', 'content': (
                '## Objective\n- Isolated compaction boundary check.\n\n'
                '## Requirements\n- (none)\n\n'
                '## Decisions\n- (none)\n\n'
                '## Work State\n### Completed\n- (none)\n\n'
                '### Active\n- (none)\n\n### Blocked\n- (none)\n\n'
                '## Next Move\n1. (none)\n\n'
                '## Relevant Files\n- (none)\n\n'
                '## Important Context\n- Isolated fixture only.')}
            finish = 'stop'
        else:
            delta = {'role': 'assistant', 'content': 'Isolated probe turn complete.'}
            finish = 'stop'
        chunks = [
            {'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe',
             'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
            {'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe',
             'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}],
             'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}},
        ]
        data = (''.join('data: ' + json.dumps(item) + '\n\n' for item in chunks) + 'data: [DONE]\n\n').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


INSTRUMENTATION = '''import {appendFileSync} from "node:fs";
const log=(event,req,extra={})=>appendFileSync(process.env.ARI_PROBE_TRACE,JSON.stringify({
 event,session:req.sessionID,agent:req.agent,messages:req.messages?.length,
 last_role:req.messages?.at(-1)?.role,
 has_tail:JSON.stringify(req.messages??[]).includes("Injected research state"),
 ...extra})+"\\n");
export default {id:"ari-boundary-probe",async setup(ctx){
 await ctx.session.hook("context",req=>log("context",req));
 await ctx.session.hook("compaction",req=>log("compaction",req));
 await ctx.session.hook("model.request",req=>log("model.request",req,{request_kind:req.kind}));
 return ()=>{};
}};
'''


def idle_count(context):
    return sum(1 for item in context.get('data', []) if item.get('type') == 'idle')


def wait_idle(base, password, sid, baseline=0, timeout=20, debug=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, context = request(base, f'/api/session/{sid}/context', password)
        if status == 200 and idle_count(context) > baseline:
            return context
        time.sleep(0.1)
    if debug is not None:
        debug()
    raise RuntimeError('isolated session did not become idle')


def wait_compaction(base, password, sid, previous, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, context = request(base, f'/api/session/{sid}/context', password)
        rows = [item for item in context.get('data', []) if item.get('type') == 'compaction']
        if status == 200 and len(rows) > previous:
            return context, rows[-1]
        time.sleep(0.1)
    raise RuntimeError('isolated compaction did not complete')


def submit(base, password, sid, message_id, text):
    status, reply = request(base, f'/api/session/{sid}/synthetic', password,
                            {'id': message_id, 'text': text, 'resume': True})
    if status != 200:
        raise RuntimeError(f'could not submit {message_id}: {status} {reply}')
    return reply


def main():
    BoundaryProvider.bodies = []
    BoundaryProvider.primary_tool_sent = False
    with tempfile.TemporaryDirectory(prefix='ari-placement-boundary-') as temporary:
        root = Path(temporary)
        workspace = root / 'project'
        workspace.mkdir()
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL') if key in os.environ}
        for name in ('DATA', 'CONFIG', 'STATE', 'CACHE'):
            directory = root / name.lower()
            directory.mkdir()
            env[f'XDG_{name}_HOME'] = str(directory)
        password = uuid4().hex
        trace = root / 'hooks.jsonl'
        env.update(OPENCODE_SERVER_PASSWORD=password, OPENCODE_DISABLE_MODELS_FETCH='1',
                   ARI_PROBE_KEY='test-only', ARI_PROBE_TRACE=str(trace))
        fake = ThreadingHTTPServer(('127.0.0.1', 0), BoundaryProvider)
        threading.Thread(target=fake.serve_forever, daemon=True).start()

        instrumentation = root / 'instrumentation'
        instrumentation.mkdir()
        (instrumentation / 'package.json').write_text(json.dumps(
            {'name': 'ari-boundary-probe', 'type': 'module', 'exports': {'.': './index.js'}}))
        (instrumentation / 'index.js').write_text(INSTRUMENTATION)

        plugin = (Path(__file__).resolve().parents[1] / 'plugin').resolve()
        config = root / 'config' / 'opencode'
        config.mkdir()
        (config / 'opencode.jsonc').write_text(json.dumps({
            'plugins': [
                {'package': str(plugin), 'options': {'contextPlacement': 'tail'}},
                {'package': str(instrumentation), 'options': {}},
            ],
            'model': 'ari-probe/probe',
            'providers': {'ari-probe': {'name': 'isolated test', 'env': ['ARI_PROBE_KEY'],
                'package': '@opencode/ai/providers/openai-compatible',
                'compatibility': {'cacheFormat': 'anthropic'},
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
                        raise RuntimeError('isolated host did not start')
                    time.sleep(0.1)
                sid = 'ses_boundary_' + uuid4().hex
                status, _ = request(base, '/api/session', password,
                    {'id': sid, 'title': 'placement boundary fixture', 'location': {'directory': str(workspace)},
                     'model': {'providerID': 'ari-probe', 'id': 'probe'}})
                if status != 200:
                    raise RuntimeError('could not create the isolated session')
                deadline = time.monotonic() + 8
                while True:
                    command_status, response = request(base, '/api/rpc/auto-research/command', password,
                        {'input': {'action': 'init', 'sessionID': sid, 'directory': str(workspace),
                                   'args': {'goal': 'isolated placement boundary check'}}})
                    if command_status == 200 or time.monotonic() >= deadline:
                        break
                    time.sleep(0.1)
                if command_status != 200 or response.get('output', {}).get('error'):
                    raise RuntimeError(f"init failed: {response.get('output')}")
                time.sleep(0.5)

                def debug():
                    try:
                        _, current = request(base, f'/api/session/{sid}/context', password)
                    except Exception as error:
                        current = {'error': str(error)}
                    log.flush()
                    log.seek(0)
                    print(json.dumps({
                        'context': current,
                        'hooks': trace.read_text() if trace.exists() else '',
                        'bodies': [{'names': sorted(name for name in tool_names(body) if name),
                                    'last_role': (body.get('messages') or [{}])[-1].get('role'),
                                    'last_text': message_text((body.get('messages') or [{}])[-1])[:200]}
                                   for body in BoundaryProvider.bodies],
                        'server': [line for line in log.read().splitlines()
                                   if 'Research' in line or 'error' in line.lower()][-20:],
                    }, indent=2))

                _, before_first = request(base, f'/api/session/{sid}/context', password)
                submit(base, password, sid, 'msg_boundary_one', 'first isolated step')
                wait_idle(base, password, sid, idle_count(before_first), debug=debug)
                primary_before = [body for body in BoundaryProvider.bodies if 'research_query' in tool_names(body)]
                if len(primary_before) != 2 or not all(has_tail(body) for body in primary_before):
                    raise RuntimeError(f'expected two tailed primary requests, got {len(primary_before)}')
                mixed_group = tool_result_ids(primary_before[1])
                if sorted(mixed_group) != ['call_probe_query', 'call_probe_wait']:
                    raise RuntimeError(f'host did not preserve the mixed wait/query group: {mixed_group}')
                bodies_before = len(BoundaryProvider.bodies)
                _, before_compact = request(base, f'/api/session/{sid}/context', password)
                compactions_before = sum(1 for item in before_compact.get('data', []) if item.get('type') == 'compaction')

                compact_id = 'msg_compact_' + uuid4().hex
                compact_status, compact_reply = request(base, f'/api/session/{sid}/compact', password,
                                                        {'id': compact_id})
                if compact_status != 200:
                    raise RuntimeError(f'compact endpoint failed: {compact_status} {compact_reply}')
                deadline = time.monotonic() + 20
                compaction_body = None
                while time.monotonic() < deadline:
                    new_bodies = BoundaryProvider.bodies[bodies_before:]
                    compaction_body = next((body for body in new_bodies if is_compaction_body(body)), None)
                    if compaction_body is not None:
                        break
                    time.sleep(0.1)
                if compaction_body is None:
                    log.flush()
                    log.seek(0)
                    if os.environ.get('ARI_BOUNDARY_DUMP'):
                        Path(os.environ['ARI_BOUNDARY_DUMP']).write_text(json.dumps(BoundaryProvider.bodies, indent=2))
                    print(json.dumps({
                        'compact_status': compact_status,
                        'compact_reply': compact_reply,
                        'new_bodies': [{'names': sorted(name for name in tool_names(body) if name),
                                        'last_role': (body.get('messages') or [{}])[-1].get('role'),
                                        'last_text': message_text((body.get('messages') or [{}])[-1])[:200]}
                                       for body in BoundaryProvider.bodies[bodies_before:]],
                        'hooks': trace.read_text() if trace.exists() else '',
                        'compaction_bodies': new_bodies,
                        'server': [line for line in log.read().splitlines()
                                   if 'Research' in line or 'error' in line.lower()][-20:],
                    }, indent=2))
                    raise RuntimeError('host did not issue a compaction model request')
                after_compact, compaction_message = wait_compaction(base, password, sid, compactions_before)
                if compaction_message.get('status') != 'completed':
                    raise RuntimeError(f"compaction did not complete: {compaction_message.get('status')}")
                compaction_bodies = [body for body in BoundaryProvider.bodies[bodies_before:] if is_compaction_body(body)]
                if not compaction_bodies or any(has_tail(body) for body in compaction_bodies):
                    raise RuntimeError('compaction request received the temporary tail')
                if os.environ.get('ARI_BOUNDARY_DUMP'):
                    Path(os.environ['ARI_BOUNDARY_DUMP']).write_text(json.dumps(BoundaryProvider.bodies, indent=2))

                submit(base, password, sid, 'msg_boundary_two', 'second isolated step')
                wait_idle(base, password, sid, idle_count(after_compact))
                primary_after = [body for body in BoundaryProvider.bodies
                                 if 'research_query' in tool_names(body) and not is_compaction_body(body)][len(primary_before):]
                if len(primary_after) != 1 or not has_tail(primary_after[0]):
                    raise RuntimeError(f'expected one tailed primary request after compaction, got {len(primary_after)}')

                _, context = request(base, f'/api/session/{sid}/context', password)
                persisted = json.dumps(context.get('data', []))
                hooks = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
                for hook in hooks:
                    hook['session_match'] = hook.get('session') == sid
                compaction_hooks = [item for item in hooks if item.get('session_match')
                                    and item.get('event') in {'compaction', 'context', 'model.request'}]
                context_rows = [item for item in compaction_hooks if item['event'] == 'context']
                compaction_rows = [item for item in compaction_hooks if item['event'] == 'compaction']
                model_rows = [item for item in compaction_hooks if item['event'] == 'model.request']
                result = {
                    'primary_before': len(primary_before),
                    'primary_after': len(primary_after),
                    'mixed_group_results': mixed_group,
                    'mixed_group_complete': sorted(mixed_group) == ['call_probe_query', 'call_probe_wait'],
                    'all_primary_have_tail': all(has_tail(body) for body in primary_before + primary_after),
                    'tail_is_last': all(message_text(body['messages'][-1]).startswith('<research_state>')
                                        for body in primary_before + primary_after),
                    'compaction_requests': len(compaction_bodies),
                    'compaction_completed': compaction_message.get('status') == 'completed',
                    'compaction_tool_count': len(tool_names(compaction_body)),
                    'compaction_has_tail': any(has_tail(body) for body in compaction_bodies),
                    'compaction_last_text': message_text(compaction_body['messages'][-1])[:120],
                    'persisted_has_tail': TAIL_MARKER in persisted,
                    'persisted_summary_has_tail': TAIL_MARKER in str(compaction_message.get('summary', '')),
                    'context_hook_requests': len(context_rows),
                    'compaction_hook_requests': len(compaction_rows),
                    'model_request_kinds': [item.get('request_kind') for item in model_rows],
                    'compaction_hook_has_tail': any(item.get('has_tail') for item in compaction_rows),
                    'hooks': compaction_hooks,
                }
                print(json.dumps(result, indent=2, sort_keys=True))
                if not result['all_primary_have_tail'] or not result['tail_is_last']:
                    raise RuntimeError('primary requests did not carry exactly the trailing temporary message')
                if result['compaction_has_tail'] or result['compaction_hook_has_tail']:
                    raise RuntimeError('compaction request received the temporary tail')
                if result['persisted_has_tail'] or result['persisted_summary_has_tail']:
                    raise RuntimeError('persisted history contains the temporary tail')
                if len(context_rows) != len(primary_before) + len(primary_after):
                    raise RuntimeError('the context hook ran for a non-primary request')
                if len(compaction_rows) != len(compaction_bodies):
                    raise RuntimeError('the compaction hook did not cover every compaction request')
                if 'compaction' not in result['model_request_kinds']:
                    raise RuntimeError('model.request did not report the compaction kind')
                return 0
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
    sys.exit(main())
