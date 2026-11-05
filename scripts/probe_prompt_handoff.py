"""Lock the host's prompt-queue contract with an isolated loopback fixture.

The plan ``docs/Prompt cache terminal handoff repair plan.md`` depends on the
installed host honouring a prompt hook that rewrites ``delivery`` from ``steer``
to ``queue``. This probe proves that contract against OpenCode 2.2.5 rather than
a fake host: a private XDG tree, a loopback OpenAI-compatible provider with fake
credentials and no external model or research data.

Observed facts on OpenCode 2.2.5:

1. a real user prompt that arrives while a session is busy is held out of the
   request already in flight and delivered at the **next step of the same
   execution**; it does not create a new execution boundary. Only a prompt sent
   to an idle session starts its own execution;
2. the prompt hook runs once at enqueue, and the ``messageID`` it sees is the
   native inbound id;
3. message ID, text, attachments and order survive the queue;
4. ``synthetic`` notifications (``resume: false``) do not reach the prompt hook;
5. ``steer`` and ``queue`` differ: with the same barrier and send time, ``steer``
   delivers both pending inputs at the next step, while ``queue`` delivers them
   one per step. ``scripts/probe_step_association.py`` records that control.

The probe decides nothing for the plugin: it only records what the host does.
"""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
from uuid import uuid4

from probe_opencode_execution import request

QUEUED_TEXT = 'queue me while the first turn is still running'
FIRST_TEXT = 'first isolated user input'
SYNTHETIC_TEXT = 'synthetic notification that is not user input'
ATTACHMENT_TEXT = 'attachment marker 47'
IDLE_TEXT = 'queued prompt for an idle session'


def message_text(message):
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(str(part.get('text', '')) for part in content if isinstance(part, dict))
    return ''


def body_text(body):
    return '\n'.join(message_text(message) for message in body.get('messages') or [])


def is_primary(body):
    return bool(body.get('tools'))


class HandoffProvider(BaseHTTPRequestHandler):
    """Answers the first prompt only after the probe released a controlled gate."""

    bodies = []
    lock = threading.Lock()
    first_arrived = threading.Event()
    first_gate = threading.Event()

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
        with self.lock:
            index = len(self.bodies)
            self.bodies.append(body)
        text = body_text(body)
        if index == 0 and FIRST_TEXT in text:
            self.first_arrived.set()
            self.first_gate.wait(timeout=30)
        chunks = [
            {'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe',
             'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': f'probe reply {index}'},
                          'finish_reason': None}]},
            {'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe',
             'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
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
const log=(event,extra={})=>appendFileSync(process.env.ARI_HANDOFF_TRACE,JSON.stringify({event,...extra})+"\\n");
export default {id:"ari-prompt-handoff-probe",async setup(ctx){
 await ctx.session.hook("prompt",req=>{
  const text=String(req.prompt?.text??"");
  log("prompt",{session:req.sessionID,message:req.messageID,delivery:req.delivery,text,
   files:(req.prompt?.files??[]).length,metadata:req.metadata??null});
  if(text.includes("queue me while")){req.delivery="queue";log("prompt.delivery",{message:req.messageID,delivery:req.delivery});}
 });
 await ctx.session.hook("context",req=>{
  const text=JSON.stringify(req.messages??[]);
  log("context",{session:req.sessionID,messages:(req.messages??[]).length,
   queued_text_present:text.includes("queue me while"),first_text_present:text.includes("first isolated user input"),
   attachment_present:text.includes("attachment marker 47")});
 });
 let open=true;
 const stream=ctx.event.subscribe();
 void (async()=>{try{for await(const e of stream){if(!open)break;
  if(e.data?.sessionID)log("event",{type:e.type,session:e.data.sessionID,inbox:e.data.inboxID,
   delivery:e.data?.item?.payload?.delivery,source:e.data?.item?.payload?.metadata?.source,seq:e.durable?.seq});
 }}catch{}})();
 return ()=>{open=false};
}};
'''


def wait_until(check, label, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise RuntimeError(f'timed out waiting for {label}')


def read_trace(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def events_of(trace, sid, kind):
    return [item for item in trace if item.get('event') == 'event' and item.get('session') == sid and item.get('type') == kind]


def delivery_scope(events, inbox_id):
    """Where the host delivered a queued prompt relative to execution boundaries.

    ``new_execution`` means a fresh execution started between the enqueue and
    the delivery. ``same_execution`` means the host delivered it at the next
    step of the running execution, which is what OpenCode 2.2.5 does for a
    ``queue`` prompt. Both are recorded, never assumed.
    """
    ordered = [item for item in events if isinstance(item.get('seq'), int)]
    enqueued = next((item for item in ordered
                     if item['type'] == 'session.inbox.enqueued' and item.get('inbox') == inbox_id), None)
    delivered = next((item for item in ordered
                      if item['type'] == 'session.inbox.delivered' and item.get('inbox') == inbox_id), None)
    if not enqueued or not delivered:
        return 'undelivered'
    starts = [item for item in ordered if item['type'] == 'session.execution.started']
    # The execution the prompt was queued behind, if one was already running.
    prior = [item for item in starts if item['seq'] < enqueued['seq']]
    if not prior:
        return 'new_execution' if any(item['seq'] < delivered['seq'] for item in starts) else 'same_execution'
    baseline = prior[-1]['seq']
    if any(baseline < item['seq'] < delivered['seq'] for item in starts):
        return 'new_execution'
    return 'same_execution'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dump', type=Path, help='write every provider request body here')
    args = parser.parse_args()
    HandoffProvider.bodies = []
    HandoffProvider.first_arrived.clear()
    HandoffProvider.first_gate.clear()
    with tempfile.TemporaryDirectory(prefix='ari-prompt-handoff-') as temporary:
        root = Path(temporary)
        workspace = root / 'project'
        workspace.mkdir()
        attachment = workspace / 'note.txt'
        attachment.write_text(ATTACHMENT_TEXT + '\n')
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL') if key in os.environ}
        for name in ('DATA', 'CONFIG', 'STATE', 'CACHE'):
            directory = root / name.lower()
            directory.mkdir()
            env[f'XDG_{name}_HOME'] = str(directory)
        password = uuid4().hex
        trace_path = root / 'hook.jsonl'
        env.update(OPENCODE_SERVER_PASSWORD=password, OPENCODE_DISABLE_MODELS_FETCH='1',
                   ARI_PROBE_KEY='test-only', ARI_HANDOFF_TRACE=str(trace_path))
        fake = ThreadingHTTPServer(('127.0.0.1', 0), HandoffProvider)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        plugin_dir = root / 'handoff-probe'
        plugin_dir.mkdir()
        (plugin_dir / 'package.json').write_text(json.dumps(
            {'name': 'ari-prompt-handoff-probe', 'type': 'module', 'exports': {'.': './index.js'}}))
        (plugin_dir / 'index.js').write_text(INSTRUMENTATION)
        config_dir = root / 'config' / 'opencode'
        config_dir.mkdir()
        (config_dir / 'opencode.jsonc').write_text(json.dumps({
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
                        raise RuntimeError('isolated host did not start')
                    time.sleep(0.1)
                sid = 'ses_handoff_' + uuid4().hex
                created, _ = request(base, '/api/session', password,
                    {'id': sid, 'title': 'prompt handoff fixture', 'location': {'directory': str(workspace)},
                     'model': {'providerID': 'ari-probe', 'id': 'probe'}})
                if created != 200:
                    raise RuntimeError('could not create the isolated session')
                time.sleep(0.5)

                first_status, _ = request(base, f'/api/session/{sid}/prompt', password,
                    {'id': 'msg_handoff_one', 'text': FIRST_TEXT})
                if first_status != 200:
                    raise RuntimeError(f'first prompt failed: {first_status}')
                if not HandoffProvider.first_arrived.wait(timeout=20):
                    raise RuntimeError('the first prompt never reached the provider')

                queued_status, queued_reply = request(base, f'/api/session/{sid}/prompt', password,
                    {'id': 'msg_handoff_two', 'text': QUEUED_TEXT,
                     'files': [{'uri': attachment.as_uri(), 'name': 'note.txt'}]})
                if queued_status != 200:
                    raise RuntimeError(f'busy prompt failed: {queued_status} {queued_reply}')
                stream_during_busy = len(HandoffProvider.bodies)
                inbox_status, inbox = request(base, f'/api/session/{sid}/inbox', password)
                inbox_items = [item for item in inbox.get('data', []) if item.get('id') == 'msg_handoff_two']
                inbox_delivery = inbox_items[0].get('delivery') if inbox_items else None
                queued_while_busy = HandoffProvider.first_gate.is_set() is False and stream_during_busy == 1

                HandoffProvider.first_gate.set()
                wait_until(lambda: len(HandoffProvider.bodies) >= 2, 'the queued prompt to run', 25)
                wait_until(lambda: any(item.get('type') == 'session.execution.succeeded'
                                       for item in events_of(read_trace(trace_path), sid, 'session.execution.succeeded')),
                           'the first execution to succeed', 25)

                third_status, _ = request(base, f'/api/session/{sid}/prompt', password,
                    {'id': 'msg_handoff_three', 'text': IDLE_TEXT, 'delivery': 'queue'})
                if third_status != 200:
                    raise RuntimeError(f'idle queued prompt failed: {third_status}')
                wait_until(lambda: len(HandoffProvider.bodies) >= 3, 'the idle queued prompt', 25)
                wait_until(lambda: any(item.get('type') == 'idle' and item.get('outcome') == 'succeeded'
                                       for item in request(base, f'/api/session/{sid}/context', password)[1].get('data', [])),
                           'the queued idle execution to finish', 25)

                synthetic_status, _ = request(base, f'/api/session/{sid}/synthetic', password,
                    {'id': 'msg_handoff_synthetic', 'text': SYNTHETIC_TEXT, 'resume': False})
                time.sleep(0.5)

                time.sleep(0.5)
                trace = read_trace(trace_path)
                prompts = [item for item in trace if item.get('event') == 'prompt' and item.get('session') == sid]
                contexts = [item for item in trace if item.get('event') == 'context' and item.get('session') == sid]
                bodies = list(HandoffProvider.bodies)
                first_body, queued_body, idle_body = bodies[0], bodies[1], bodies[2]
                context_status, context = request(base, f'/api/session/{sid}/context', password)
                persisted = json.dumps(context.get('data', []))
                transcript_types = {item.get('id'): item.get('type') for item in context.get('data', [])}
                result = {
                    'host_version': subprocess.check_output(['opencode', '--version'], env=env, text=True).strip(),
                    'prompt_hook_calls': [{'message': item.get('message'), 'delivery': item.get('delivery'),
                                           'text': item.get('text'), 'files': item.get('files')} for item in prompts],
                    'prompt_hook_messages': [item.get('message') for item in prompts],
                    'queued_message_prompt_hook_count': sum(item.get('message') == 'msg_handoff_two' for item in prompts),
                    'delivery_before_hook': [item.get('delivery') for item in prompts
                                             if item.get('message') == 'msg_handoff_two'],
                    'enqueued_events': [{'inbox': item.get('inbox'), 'delivery': item.get('delivery')}
                                        for item in events_of(trace, sid, 'session.inbox.enqueued')],
                    'delivered_events': [item.get('inbox') for item in events_of(trace, sid, 'session.inbox.delivered')],
                    'execution_starts': len(events_of(trace, sid, 'session.execution.started')),
                    'execution_successes': len(events_of(trace, sid, 'session.execution.succeeded')),
                    'busy_queue_delivery_scope': delivery_scope(trace, 'msg_handoff_two'),
                    'idle_queue_delivery_scope': delivery_scope(trace, 'msg_handoff_three'),
                    'synthetic_reached_prompt_hook': any(item.get('text') == SYNTHETIC_TEXT for item in prompts),
                    'synthetic_status': synthetic_status,
                    'first_request_had_queued_text': QUEUED_TEXT in body_text(first_body),
                    'queued_request_had_queued_text': QUEUED_TEXT in body_text(queued_body),
                    'queued_request_had_first_text': FIRST_TEXT in body_text(queued_body),
                    'queued_request_had_attachment_text': ATTACHMENT_TEXT in body_text(queued_body),
                    'idle_request_had_idle_text': IDLE_TEXT in body_text(idle_body),
                    'requests_during_busy': stream_during_busy,
                    'inbox_delivery_while_busy': inbox_delivery,
                    'queued_while_busy': queued_while_busy,
                    'queued_ids_persisted': 'msg_handoff_two' in persisted,
                    'real_prompt_transcript_type': transcript_types.get('msg_handoff_two'),
                    'synthetic_transcript_type': transcript_types.get('msg_handoff_synthetic'),
                    'context_hook_bodies_with_queued_text': sum(item.get('queued_text_present') for item in contexts),
                    'context_status': context_status,
                    'session_events': [item for item in trace
                                       if item.get('session') == sid and item.get('event') == 'event'],
                    'context_hook_calls': contexts,
                    'used_external_model': False,
                }
                if args.dump:
                    args.dump.write_text(json.dumps(bodies, indent=2))
                print(json.dumps(result, indent=2, sort_keys=True))
                problems = []
                if result['queued_message_prompt_hook_count'] != 1:
                    problems.append('the prompt hook did not run exactly once for the queued prompt')
                if result['inbox_delivery_while_busy'] != 'queue':
                    problems.append('the host did not record the rewritten delivery as queue')
                if not queued_while_busy:
                    problems.append('the queued prompt was not held until the busy execution moved on')
                if result['first_request_had_queued_text']:
                    problems.append('the queued text leaked into the busy execution request')
                if not result['queued_request_had_queued_text']:
                    problems.append('the queued prompt was not delivered to a later request')
                if not result['queued_request_had_attachment_text']:
                    problems.append('the attachment did not survive the queue')
                if result['busy_queue_delivery_scope'] == 'undelivered':
                    problems.append('the host never delivered the queued prompt')
                if result['execution_starts'] < 2:
                    problems.append('the idle queued prompt did not start its own execution')
                if result['idle_queue_delivery_scope'] == 'undelivered':
                    problems.append('the host never delivered the idle queued prompt')
                if result['synthetic_reached_prompt_hook']:
                    problems.append('a resume:false synthetic reached the user prompt hook')
                if result['real_prompt_transcript_type'] != 'user':
                    problems.append('a real prompt is not a user entry in the transcript')
                if result['synthetic_transcript_type'] == 'user':
                    problems.append('a resume:false synthetic was persisted as a real user entry')
                if problems:
                    raise RuntimeError('; '.join(problems))
                return 0
            finally:
                HandoffProvider.first_gate.set()
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
