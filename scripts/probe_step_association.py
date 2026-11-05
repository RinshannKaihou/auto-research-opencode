"""Prove how a real user input maps to the model step that generated a tool call.

The PR #36 plan needs to know whether a user input that arrived *before* a
terminal tool was registered can still be tied to the step that generated that
tool call. This probe uses the same private-XDG / loopback-provider fixture as
``probe_prompt_handoff.py`` and blocks the model request and the tool itself, then
sends real user prompts and records:

- the prompt hook messageID, delivery and the inbox delivery sequence;
- every primary ``context`` request, including its messages' native IDs and
  whether two primary requests ever overlap;
- ``session.step.started``'s ``assistantMessageID``;
- the tool execution ``call.messageID`` / ``call.id``;
- which provider request body first contains each user text.

Nothing is inferred from wall-clock ordering: the probe compares native IDs and
durable event sequence. It only records the contract; it decides nothing for the
plugin.
"""
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

import argparse

from probe_opencode_execution import request

FIRST_TEXT = 'first user input for the step probe'
DURING_GENERATION = 'input sent while the model was generating'
DURING_TOOL = 'input sent while the tool was running'


def message_text(message):
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(str(part.get('text', '')) for part in content if isinstance(part, dict))
    return ''


def body_text(body):
    return '\n'.join(message_text(message) for message in body.get('messages') or [])


class StepProvider(BaseHTTPRequestHandler):
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
        action = None
        if index == 0 and FIRST_TEXT in text:
            # Hold the first model step so a real prompt arrives mid-generation.
            self.first_arrived.set()
            self.first_gate.wait(timeout=20)
            action = 'probe_step'
        delta = ({'tool_calls': [{'index': 0, 'id': 'call_step_one', 'type': 'function',
                                  'function': {'name': action, 'arguments': json.dumps({'reason': 'association'})}}]}
                 if action else {'role': 'assistant', 'content': f'probe reply {index}'})
        chunks = [
            {'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe',
             'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
            {'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'probe',
             'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if action else 'stop'}],
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


INSTRUMENTATION = '''import {appendFileSync, existsSync} from "node:fs";
const log=(event,extra={})=>appendFileSync(process.env.ARI_STEP_TRACE,JSON.stringify({event,...extra})+"\\n");
let active=0;
export default {id:"ari-step-association-probe",async setup(ctx){
 const sleep=(ms)=>new Promise(r=>setTimeout(r,ms));
 await ctx.session.hook("prompt",req=>{
  const text=String(req.prompt?.text??"");
  const before=req.delivery;
  if(process.env.ARI_STEP_DELIVERY==="queue")req.delivery="queue";
  log("prompt",{session:req.sessionID,message:req.messageID,delivery:req.delivery,before,text,
   files:(req.prompt?.files??[]).length});
 });
 await ctx.session.hook("context",req=>{
  active+=1;
  const messages=(req.messages??[]).map(m=>({role:m.role,id:m.id??null,
   types:(m.content??[]).map(p=>p?.type??null)}));
  log("context",{session:req.sessionID,active,messages,
   generation_present:JSON.stringify(req.messages??[]).includes("input sent while the model was generating"),
   tool_present:JSON.stringify(req.messages??[]).includes("input sent while the tool was running")});
  active-=1;
 });
 await ctx.tool.transform(editor=>{
  editor.add({name:"probe_step",description:"association probe tool",input:{},
   options:{codemode:false},
   async execute(input,call){
    let transcript=null;
    try{
     const rows=await ctx.session.context({sessionID:call.sessionID});
     transcript=(rows??[]).map(r=>({id:r.id??null,type:r.type??null}));
    }catch(e){transcript="error:"+String(e)}
    log("tool",{session:call.sessionID,message:call.messageID,id:call.id,transcript});
    for(let i=0;i<200&&!existsSync(process.env.ARI_STEP_TOOL_RELEASE);i++)await sleep(25);
    log("tool.done",{session:call.sessionID,message:call.messageID,id:call.id});
    return {content:"probe tool done"};
   }});
 });
 let open=true;const stream=ctx.event.subscribe();
 void(async()=>{try{for await(const e of stream){if(!open)break;
  if(e.data?.sessionID&&e.type!=="session.text.delta")log("event",{type:e.type,session:e.data.sessionID,
   inbox:e.data.inboxID,seq:e.durable?.seq,assistant:e.data?.assistantMessageID});
 }}catch{}})();
 return ()=>{open=false};
}};
'''


def wait_until(check, label, timeout=25):
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


def first_body_with(bodies, text):
    for index, body in enumerate(bodies):
        if text in body_text(body):
            return index
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delivery', choices=('steer', 'queue'), default='steer',
                        help='delivery for the prompts sent during generation and tool execution')
    args = parser.parse_args()
    StepProvider.bodies = []
    StepProvider.first_arrived.clear()
    StepProvider.first_gate.clear()
    with tempfile.TemporaryDirectory(prefix='ari-step-association-') as temporary:
        root = Path(temporary)
        workspace = root / 'project'
        workspace.mkdir()
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL') if key in os.environ}
        for name in ('DATA', 'CONFIG', 'STATE', 'CACHE'):
            directory = root / name.lower()
            directory.mkdir()
            env[f'XDG_{name}_HOME'] = str(directory)
        password = uuid4().hex
        trace = root / 'steps.jsonl'
        tool_release = root / 'tool.release'
        env.update(OPENCODE_SERVER_PASSWORD=password, OPENCODE_DISABLE_MODELS_FETCH='1',
                   ARI_PROBE_KEY='test-only', ARI_STEP_TRACE=str(trace),
                   ARI_STEP_TOOL_RELEASE=str(tool_release), ARI_STEP_DELIVERY=args.delivery)
        fake = ThreadingHTTPServer(('127.0.0.1', 0), StepProvider)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        plugin_dir = root / 'step-probe'
        plugin_dir.mkdir()
        (plugin_dir / 'package.json').write_text(json.dumps(
            {'name': 'ari-step-association-probe', 'type': 'module', 'exports': {'.': './index.js'}}))
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
                sid = 'ses_step_' + uuid4().hex
                created, _ = request(base, '/api/session', password,
                    {'id': sid, 'title': 'step association fixture', 'location': {'directory': str(workspace)},
                     'model': {'providerID': 'ari-probe', 'id': 'probe'}})
                if created != 200:
                    raise RuntimeError('could not create the isolated session')
                time.sleep(0.5)

                first, _ = request(base, f'/api/session/{sid}/prompt', password,
                    {'id': 'msg_step_one', 'text': FIRST_TEXT})
                if first != 200 or not StepProvider.first_arrived.wait(timeout=20):
                    raise RuntimeError('the first model request never reached the provider')
                # (a) A real prompt arrives while the model step is generating.
                mid, _ = request(base, f'/api/session/{sid}/prompt', password,
                    {'id': 'msg_during_generation', 'text': DURING_GENERATION})
                StepProvider.first_gate.set()
                # (b) A real prompt arrives while the tool is executing.
                wait_until(lambda: any(item.get('event') == 'tool' for item in read_trace(trace)),
                           'the probe tool to start')
                during, _ = request(base, f'/api/session/{sid}/prompt', password,
                    {'id': 'msg_during_tool', 'text': DURING_TOOL})
                tool_release.write_text('go')
                wait_until(lambda: any(item.get('type') == 'session.execution.succeeded'
                                       for item in read_trace(trace)), 'the execution to finish', 30)
                time.sleep(0.3)

                records = read_trace(trace)
                contexts = [item for item in records if item.get('event') == 'context']
                tools = [item for item in records if item.get('event') == 'tool']
                steps = [item for item in records
                         if item.get('event') == 'event' and item.get('type') == 'session.step.started']
                prompts = [item for item in records if item.get('event') == 'prompt']
                bodies = list(StepProvider.bodies)
                result = {
                    'delivery_mode': args.delivery,
                    'prompt_hook_calls': [{'message': item.get('message'), 'text': item.get('text'),
                                           'before': item.get('before'), 'delivery': item.get('delivery')}
                                          for item in prompts],
                    'context_calls': [{'messages': item.get('messages'), 'active': item.get('active'),
                                       'generation_present': item.get('generation_present'),
                                       'tool_present': item.get('tool_present')} for item in contexts],
                    'context_max_active': max((item.get('active') or 0 for item in contexts), default=0),
                    'context_message_ids': [[message.get('id') for message in item.get('messages') or []]
                                            for item in contexts],
                    'step_assistant_ids': [item.get('assistant') for item in steps],
                    'tool_calls': [{'message': item.get('message'), 'id': item.get('id'),
                                    'transcript': item.get('transcript')} for item in tools],
                    'tool_message_matches_step': any(
                        item.get('message') in {step.get('assistant') for step in steps} for item in tools),
                    'generation_input_first_body': first_body_with(bodies, DURING_GENERATION),
                    'tool_input_first_body': first_body_with(bodies, DURING_TOOL),
                    'first_input_first_body': first_body_with(bodies, FIRST_TEXT),
                    'execution_starts': sum(1 for item in records
                                            if item.get('event') == 'event'
                                            and item.get('type') == 'session.execution.started'),
                    'tool_executed': bool(tools),
                    'user_ids_preserved': all(
                        item.get('message') in
                        [message.get('id') for context in contexts for message in context.get('messages') or []]
                        for item in prompts if item.get('message') in {'msg_during_generation', 'msg_during_tool'}),
                    'trace_order': [
                        {'kind': item.get('event'),
                         **({'type': item.get('type'), 'assistant': item.get('assistant')}
                            if item.get('event') == 'event' else
                            {'message': item.get('message'),
                             'ids': [message.get('id') for message in item.get('messages') or []]}
                            if item.get('event') == 'context' else {})}
                        for item in records],
                    'provider_requests': len(bodies),
                    'provider_request_user_ids': [
                        [message.get('id') for message in body.get('messages') or []
                         if message.get('role') == 'user'] for body in bodies],
                    'prompt_statuses': {'first': first, 'during_generation': mid, 'during_tool': during},
                    'used_external_model': False,
                }
                print(json.dumps(result, indent=2, sort_keys=True))
                problems = []
                if not all((first, mid, during)):
                    problems.append('a real prompt was not accepted')
                if not tools:
                    problems.append('the probe tool never executed')
                if result['context_max_active'] > 1:
                    problems.append('primary context requests overlapped in one session')
                if any(not item.get('id') for item in (contexts[0].get('messages') or []) if item.get('role') == 'user'):
                    problems.append('the context request does not expose user message IDs')
                if result['generation_input_first_body'] is None:
                    problems.append('the mid-generation input never reached a request')
                if result['tool_input_first_body'] is None:
                    problems.append('the tool-window input never reached a request')
                if problems:
                    raise RuntimeError('; '.join(problems))
                return 0
            finally:
                tool_release.write_text('go')
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
