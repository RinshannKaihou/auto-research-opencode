"""Run a tiny isolated research lifecycle with a scripted local provider."""
import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
from uuid import uuid4

from probe_opencode_execution import request


def _message_text(message):
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(str(part.get('text', '')) for part in content if isinstance(part, dict))
    return ''


def _cache_markers(value):
    return json.dumps(value).count('cache_control')


def _has_tail(body):
    return bool(body.get('messages')) and _message_text(body['messages'][-1]).startswith('<research_state>')


def _tool_pairing_ok(messages):
    """Every assistant tool call is followed by its matching tool result."""
    pending = []
    for message in messages:
        if message.get('role') == 'assistant':
            for call in message.get('tool_calls') or []:
                pending.append(call.get('id'))
        elif message.get('role') == 'tool':
            if not pending or message.get('tool_call_id') != pending.pop(0):
                return False
    return not pending


def _finishing_kind(body):
    for message in body.get('messages') or []:
        if message.get('role') == 'system':
            for kind in ('wait', 'finish'):
                if f'A research {kind} was accepted' in _message_text(message):
                    return kind
    return None


POST_TERMINAL_MARKER = 'A user message arrived while this terminal action was still pending'


def _post_terminal(body):
    """The tool-free step that carries a delivered queued user input."""
    return any(POST_TERMINAL_MARKER in _message_text(message)
               for message in body.get('messages') or [] if message.get('role') == 'system')


QUEUED_TEXT = 'queued user instruction for the next round'

# Records which request carried the queued input and whether a later request in
# the same session was a normal round (tools exposed, no terminal instruction).
HANDOFF_INSTRUMENTATION = '''import {appendFileSync} from "node:fs";
const log=(event,extra={})=>appendFileSync(process.env.ARI_HANDOFF_TRACE,JSON.stringify({event,...extra})+"\\n");
export default {id:"ari-handoff-trace",async setup(ctx){
 await ctx.session.hook("context",req=>{
  const system=(req.system??[]).map(p=>String(p.text??"")).join("\\n");
  log("context",{session:req.sessionID,tools:Object.keys(req.tools??{}).length,
   queued:JSON.stringify(req.messages??[]).includes("queued user instruction for the next round"),
   finishing:system.includes("was accepted in this execution"),
   post_terminal:system.includes("arrived while this terminal action was still pending")});
 });
 let open=true;const stream=ctx.event.subscribe();
 void(async()=>{try{for await(const e of stream){if(!open)break;
  if(e.data?.sessionID)log("event",{type:e.type,session:e.data.sessionID,inbox:e.data.inboxID,seq:e.durable?.seq});
 }}catch{}})();
 return ()=>{open=false};
}};
'''


def handoff_gate(checks):
    """The agreed acceptance: the queued input later reaches a normal round.

    A normal round is a request in the same session that still carries the
    queued text, exposes tools, and is neither the finishing request nor the
    post-terminal step. Merely keeping the text in history is not enough.
    """
    if not checks.get('normal_round'):
        return {'gate': 'blocked',
                'reason': ('no_later_execution' if not checks.get('new_executions_after_delivery')
                           else 'no_tool_request_after_delivery')}
    return {'gate': 'passed', 'reason': None}


def queued_handoff_checks(bodies, finishing_index, text=QUEUED_TEXT):
    """Where a real user prompt appears relative to the accepted wait.

    The pinned host delivers a queued prompt at the next step of the running
    execution, so the finishing step must stay clean and the step that carries
    the text must still expose no tools. The wake is the first later request
    with tools again.
    """
    containing = [index for index, body in enumerate(bodies) if text in json.dumps(body.get('messages') or [])]
    after = [index for index in containing if index > finishing_index] if finishing_index is not None else []
    later_with_tools = [index for index in after if bodies[index].get('tools')]
    return {
        'finishing_index': finishing_index,
        'finishing_had_text': finishing_index is not None and finishing_index in containing,
        'text_request_indexes': containing,
        'first_text_request': after[0] if after else None,
        'first_text_request_tool_count': len(bodies[after[0]].get('tools') or []) if after else None,
        'first_text_request_finishing_kind': _finishing_kind(bodies[after[0]]) if after else None,
        'requests_after_text_with_tools': later_with_tools,
        'wake_request_index': later_with_tools[0] if later_with_tools else None,
    }


def context_placement_checks(bodies, placement, persisted_context):
    """Checks the temporary research-state message and the cache breakpoints
    the SDK actually put on the wire. A tail-mode wait/node-completion request is
    the one deliberate exception: it carries the full context in system and
    exposes no tools, so it must have no temporary tail."""
    research = []
    for body in bodies:
        names = {(tool.get('function') or {}).get('name', '') for tool in body.get('tools') or []}
        overlay = _finishing_kind(body) is not None or _post_terminal(body)
        if any(name.startswith('research_') for name in names) or overlay:
            research.append(body)
    checks = {'requests': len(bodies), 'research_requests': len(research)}
    if placement != 'tail':
        checks['max_cache_markers'] = max((_cache_markers(body) for body in research), default=0)
        return checks
    tails = [body for body in research if _has_tail(body)]
    finishing = [body for body in research if _finishing_kind(body) is not None]
    post_terminal = [body for body in research if _post_terminal(body)]
    overlay = [body for body in research if body in finishing or body in post_terminal]
    tails = [body for body in tails if body not in overlay]
    tool_continuations = [body for body in research
                          if len(body.get('messages') or []) > 1
                          and body['messages'][-2].get('role') == 'tool']
    regular_continuations = [body for body in tool_continuations if body not in overlay]
    checks.update({
        'tail_requests': len(tails),
        'finishing_requests': len(finishing),
        'post_terminal_requests': len(post_terminal),
        'overlay_requests': len(overlay),
        'wait_finishing_requests': sum(_finishing_kind(body) == 'wait' for body in finishing),
        'node_finishing_requests': sum(_finishing_kind(body) == 'finish' for body in finishing),
        'unaccounted_requests': len(research) - len(tails) - len(overlay),
        'all_research_requests_accounted_for': len(tails) + len(overlay) == len(research),
        'tail_is_last': all(_has_tail(body) for body in research if body not in overlay),
        'tail_absent_from_earlier_messages': all(
            not any('Injected research state' in _message_text(message) for message in body['messages'][:-1])
            for body in tails),
        'tool_continuations': len(tool_continuations),
        'regular_tool_continuations': len(regular_continuations),
        'tail_follows_every_regular_tool_continuation': all(_has_tail(body) for body in regular_continuations),
        'finishing_requests_have_no_tools_and_no_tail': all(
            not body.get('tools') and not _has_tail(body) for body in finishing),
        'overlay_requests_have_no_tools_and_no_tail': all(
            not body.get('tools') and not _has_tail(body) for body in overlay),
        'tool_pairing_ok': all(_tool_pairing_ok(
            body['messages'][:-1] if _has_tail(body) else body['messages']) for body in research),
        'max_cache_markers': max((_cache_markers(body) for body in research), default=0),
        'tail_has_no_cache_marker': all(_cache_markers(body['messages'][-1]) == 0 for body in tails),
        'boundary_before_tail': all(
            len(body['messages']) > 1 and _cache_markers(body['messages'][-2]) > 0 for body in tails),
        'persisted_history_has_tail': 'Injected research state' in persisted_context,
    })
    return checks


def context_placement_ok(checks):
    """The gate for the controlled tail-placement smoke run."""
    return bool(
        checks.get('research_requests')
        and checks.get('all_research_requests_accounted_for')
        and checks.get('tail_is_last')
        and checks.get('tail_absent_from_earlier_messages')
        and checks.get('regular_tool_continuations')
        and checks.get('tail_follows_every_regular_tool_continuation')
        and checks.get('overlay_requests_have_no_tools_and_no_tail')
        and checks.get('tool_pairing_ok')
        and checks.get('tail_has_no_cache_marker')
        and checks.get('boundary_before_tail')
        and checks.get('max_cache_markers', 99) <= 4
        and not checks.get('persisted_history_has_tail')
    )


class ResearchProvider(BaseHTTPRequestHandler):
    actions = []
    requests = []
    bodies = []
    pause_node = False
    use_specialist = False
    hold_specialist = False
    hold_node = False
    background_node = False
    wait_finish = False
    queued_input = False
    queued_input_kind = None
    node_waiting = threading.Event()
    node_gate = threading.Event()
    coordinator_gate = threading.Event()
    specialist_waiting = threading.Event()
    specialist_gate = threading.Event()
    finishing_waiting = threading.Event()
    finishing_gate = threading.Event()

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
        self.bodies.append(body)
        names = {item.get('function', {}).get('name') for item in body.get('tools', []) if item.get('type') == 'function'}
        self.requests.append({'dispatch': 'research_dispatch' in names, 'finish': 'research_finish' in names,
                              'query': 'research_query' in names, 'shell': 'shell' in names, 'tool_count': len(names)})
        if self.queued_input_kind and _finishing_kind(body) == self.queued_input_kind:
            self.finishing_waiting.set()
            self.finishing_gate.wait(timeout=20)
        if self.hold_node and 'dispatch' in self.actions and 'research_dispatch' not in names:
            self.node_waiting.set()
            self.node_gate.wait(timeout=12)
        if self.hold_node and 'dispatch' in self.actions and 'research_dispatch' in names:
            self.coordinator_gate.wait(timeout=15)
        action = None
        args = None
        if 'research_dispatch' in names:
            if 'propose' not in self.actions:
                action, args = 'research_propose', {'question': 'Is the isolated probe working?', 'why_now': 'integration probe',
                                                     'plan': 'Report a controlled result', 'root_reason': 'test fixture'}
                self.actions.append('propose')
            elif 'dispatch' not in self.actions:
                action, args = 'research_dispatch', {'node_id': 'X-001'}
                self.actions.append('dispatch')
            elif self.wait_finish and 'wait' not in self.actions:
                # The scripted model only calls wait while tools are exposed;
                # the finishing request offers none, so it then replies text.
                task_id = None
                for message in reversed(body.get('messages') or []):
                    try:
                        value = json.loads(_message_text(message))
                    except (TypeError, ValueError):
                        continue
                    if isinstance(value, dict) and isinstance(value.get('task_id'), str):
                        task_id = value['task_id']
                        break
                if task_id:
                    action, args = 'research_wait', {'task_ids': [task_id]}
                    self.actions.append('wait')
        elif 'research_finish' in names and 'node_finish' not in self.actions:
            if self.background_node and 'background_start' not in self.actions:
                action, args = 'shell', {'command': 'sleep 2', 'background': True, 'timeout': 20000}
                self.actions.append('background_start')
            elif self.background_node and 'background_wait' not in self.actions:
                self.actions.append('background_wait')
            elif self.use_specialist and 'delegate' not in self.actions:
                action, args = 'research_delegate', {'label': 'independent review', 'question': 'Check the fixture',
                    'purpose': 'independent review', 'deliverable': 'a short report',
                    'completion_criteria': 'provide an assessment', 'report_requirements': 'state the basis'}
                self.actions.append('delegate')
            else:
                if self.pause_node:
                    self.node_waiting.set()
                    self.node_gate.wait(timeout=12)
                action, args = 'research_finish', {'state': 'finished', 'summary': 'isolated scripted result'}
                self.actions.append('node_finish')
        if action:
            delta = {'tool_calls': [{'index': 0, 'id': 'call_' + str(len(self.actions)), 'type': 'function',
                                     'function': {'name': action, 'arguments': json.dumps(args)}}]}
        else:
            if self.hold_specialist and 'research_query' in names and 'research_dispatch' not in names and 'research_finish' not in names and 'shell' not in names:
                self.specialist_waiting.set()
                self.specialist_gate.wait(timeout=15)
            delta = {'role': 'assistant', 'content': 'isolated probe turn complete'}
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
            pass  # An interrupted local provider request may have disconnected.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pause-node', action='store_true')
    parser.add_argument('--resume-after-pause', action='store_true')
    parser.add_argument('--specialist', action='store_true')
    parser.add_argument('--stop-during-specialist', action='store_true')
    parser.add_argument('--lost-report-response', action='store_true', help='requires an instrumented local test plugin')
    parser.add_argument('--lost-kickoff-response', action='store_true', help='requires an instrumented local test plugin')
    parser.add_argument('--cold-restart-node', action='store_true')
    parser.add_argument('--cold-restart-specialist', action='store_true')
    parser.add_argument('--warm-reload-specialist', action='store_true')
    parser.add_argument('--plugin', type=Path, help='plugin directory to load (defaults to the source tree)')
    parser.add_argument('--context-placement', choices=('system', 'tail'),
                        help='plugin contextPlacement option; tail exercises the temporary research-state message')
    parser.add_argument('--request-dump', type=Path, help='write every provider request body to this JSON file')
    parser.add_argument('--background-node', action='store_true', help='exercise a native background shell and completion wake')
    parser.add_argument('--wait-finish', action='store_true',
                        help='script an accepted coordinator wait and assert the tool-free finishing request')
    parser.add_argument('--queued-input', action='store_true',
                        help='send a real user prompt while the accepted wait is finishing; requires --wait-finish and tail')
    parser.add_argument('--queued-input-node', action='store_true',
                        help='send a real user prompt while the accepted node finish is preparing; requires tail')
    parser.add_argument('--expect-blocked', action='store_true',
                        help='record the host-contract block instead of failing when the queued input never gets a normal round')
    args = parser.parse_args()
    if args.queued_input and (not args.wait_finish or args.context_placement != 'tail'):
        parser.error('--queued-input requires --wait-finish and --context-placement tail')
    if args.queued_input_node and (args.context_placement != 'tail' or args.queued_input):
        parser.error('--queued-input-node requires --context-placement tail and is separate from --queued-input')
    if args.resume_after_pause and not args.pause_node:
        parser.error('--resume-after-pause requires --pause-node')
    if args.stop_during_specialist and (not args.specialist or args.pause_node):
        parser.error('--stop-during-specialist requires --specialist and cannot be combined with --pause-node')
    if args.lost_report_response and (args.pause_node or args.stop_during_specialist):
        parser.error('--lost-report-response cannot be combined with pause or stop probes')
    if args.lost_kickoff_response and (args.pause_node or args.stop_during_specialist or args.lost_report_response or args.specialist):
        parser.error('--lost-kickoff-response is a standalone probe')
    if args.cold_restart_node and (args.pause_node or args.stop_during_specialist or args.lost_report_response or args.lost_kickoff_response or args.specialist):
        parser.error('--cold-restart-node is a standalone probe')
    if args.cold_restart_specialist and (not args.specialist or args.pause_node or args.stop_during_specialist or args.lost_report_response or args.lost_kickoff_response or args.cold_restart_node or args.warm_reload_specialist):
        parser.error('--cold-restart-specialist requires --specialist and is otherwise standalone')
    if args.warm_reload_specialist and (not args.specialist or args.pause_node or args.stop_during_specialist or args.lost_report_response or args.lost_kickoff_response or args.cold_restart_node):
        parser.error('--warm-reload-specialist requires --specialist and is otherwise standalone')
    ResearchProvider.actions = []
    ResearchProvider.requests = []
    ResearchProvider.bodies = []
    ResearchProvider.pause_node = args.pause_node
    ResearchProvider.use_specialist = args.specialist
    ResearchProvider.hold_specialist = args.stop_during_specialist or args.warm_reload_specialist or args.cold_restart_specialist
    ResearchProvider.hold_node = args.lost_kickoff_response or args.cold_restart_node
    ResearchProvider.background_node = args.background_node
    ResearchProvider.wait_finish = args.wait_finish
    ResearchProvider.queued_input = args.queued_input or args.queued_input_node
    ResearchProvider.queued_input_kind = ('finish' if args.queued_input_node
                                          else 'wait' if args.queued_input else None)
    ResearchProvider.node_waiting.clear()
    ResearchProvider.node_gate.clear()
    ResearchProvider.coordinator_gate.clear()
    ResearchProvider.specialist_waiting.clear()
    ResearchProvider.specialist_gate.clear()
    ResearchProvider.finishing_waiting.clear()
    ResearchProvider.finishing_gate.clear()
    with tempfile.TemporaryDirectory(prefix='ari-research-probe-') as temporary:
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
        if args.lost_report_response:
            env['ARI_PROBE_LOSE_REPORT'] = '1'
        if args.lost_kickoff_response:
            env['ARI_PROBE_LOSE_KICKOFF'] = '1'
        fake = ThreadingHTTPServer(('127.0.0.1', 0), ResearchProvider)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        config = root / 'config' / 'opencode'
        config.mkdir()
        plugin = (args.plugin or Path(__file__).resolve().parents[1] / 'plugin').resolve()
        plugin_options = {'contextPlacement': args.context_placement} if args.context_placement else {}
        plugins = [{'package': str(plugin), 'options': plugin_options}]
        handoff_trace = root / 'handoff.jsonl'
        if args.queued_input or args.queued_input_node:
            trace_dir = root / 'handoff-trace'
            trace_dir.mkdir()
            (trace_dir / 'package.json').write_text(json.dumps(
                {'name': 'ari-handoff-trace', 'type': 'module', 'exports': {'.': './index.js'}}))
            (trace_dir / 'index.js').write_text(HANDOFF_INSTRUMENTATION)
            plugins.append({'package': str(trace_dir), 'options': {}})
            env['ARI_HANDOFF_TRACE'] = str(handoff_trace)
        if args.warm_reload_specialist:
            trigger = root / 'reload-trigger'
            trigger.mkdir()
            (trigger / 'package.json').write_text(json.dumps({'name': 'ari-reload-probe', 'type': 'module', 'exports': {'.': './index.js'}}))
            env.update(ARI_RELOAD_FLAG=str(root / 'reload.flag'), ARI_RELOAD_RESULT=str(root / 'reload.result'))
            (trigger / 'index.js').write_text('''import {existsSync,writeFileSync} from "node:fs";
import research from ''' + json.dumps((plugin / 'index.ts').as_uri()) + ''';
export default {id:"ari-reload-probe",async setup(ctx){
 const key=Symbol.for("ari.reload.probe"); if(globalThis[key])return ()=>{}; globalThis[key]=true;
 const timer=setInterval(async()=>{if(!existsSync(process.env.ARI_RELOAD_FLAG))return;clearInterval(timer);
   try{await research.setup(ctx);writeFileSync(process.env.ARI_RELOAD_RESULT,"ready");}
   catch(e){writeFileSync(process.env.ARI_RELOAD_RESULT,"error:"+String(e));}},50);
 return ()=>clearInterval(timer);}};
''')
            plugins.append({'package': str(trigger), 'options': {}})
        (config / 'opencode.jsonc').write_text(json.dumps({
            'plugins': plugins, 'model': 'ari-probe/probe',
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
                        raise RuntimeError('Isolated host did not start')
                    time.sleep(0.1)
                sid = 'ses_research_probe_' + uuid4().hex
                status, _ = request(base, '/api/session', password,
                    {'id': sid, 'title': 'isolated lifecycle', 'location': {'directory': str(workspace)},
                     'model': {'providerID': 'ari-probe', 'id': 'probe'}})
                if status != 200:
                    raise RuntimeError('Could not create coordinator')
                def command(action, args):
                    deadline = time.monotonic() + 8
                    while True:
                        status, response = request(base, '/api/rpc/auto-research/command', password,
                            {'input': {'action': action, 'sessionID': sid, 'directory': str(workspace), 'args': args}})
                        if status == 200 or time.monotonic() >= deadline:
                            return status, response.get('output', {})
                        time.sleep(0.1)
                init_status, init = command('init', {'goal': 'isolated lifecycle test'})
                budget_status, budget = command('budget', {'limit': 'turns', 'value': '4', 'reason': 'bounded test'})
                auto_status, auto = command('auto', {})
                if any(status != 200 or result.get('error') for status, result in ((init_status, init), (budget_status, budget), (auto_status, auto))):
                    print(json.dumps({'init_error': init.get('error'), 'budget_error': budget.get('error'), 'auto_error': auto.get('error')}))
                    raise SystemExit(1)
                database = workspace / '.research' / 'state.sqlite3'
                queued = None
                if args.queued_input or args.queued_input_node:
                    if not ResearchProvider.finishing_waiting.wait(timeout=20):
                        raise RuntimeError('The accepted terminal action did not produce a finishing request')
                    target = sid
                    if args.queued_input_node:
                        with sqlite3.connect(database) as db:
                            row = db.execute("SELECT session_id FROM exploration_tasks WHERE state='running'").fetchone()
                        if not row:
                            raise RuntimeError('Could not locate the running node session')
                        target = row[0]
                    prompt_status, prompt_reply = request(base, f'/api/session/{target}/prompt', password,
                        {'id': 'msg_queued_user', 'text': QUEUED_TEXT})
                    if prompt_status != 200:
                        raise RuntimeError(f'Queued prompt failed: {prompt_status} {prompt_reply}')
                    _, inbox = request(base, f'/api/session/{target}/inbox', password)
                    inbox_items = [item for item in inbox.get('data', []) if item.get('id') == 'msg_queued_user']
                    queued = {'status': prompt_status, 'session': target,
                              'delivery': inbox_items[0].get('delivery') if inbox_items else None,
                              'bodies_before_release': len(ResearchProvider.bodies)}
                    ResearchProvider.finishing_gate.set()
                reservations_at_pause = None
                if args.warm_reload_specialist:
                    if not ResearchProvider.specialist_waiting.wait(timeout=12):
                        raise RuntimeError('Specialist did not reach the controlled warm-reload point')
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        with sqlite3.connect(database) as db:
                            owner = db.execute("SELECT state,details FROM workflow_intents WHERE kind='controller_owner'").fetchone()
                            launch = db.execute("SELECT state,details FROM workflow_intents WHERE kind='specialist_launch'").fetchone()
                        if owner and owner[0] == 'ready' and launch and launch[0] == 'submitted':
                            break
                        time.sleep(0.1)
                    if not owner or not launch or owner[0] != 'ready' or launch[0] != 'submitted':
                        raise RuntimeError('Specialist was not fully launched before warm reload')
                    previous_owner, previous_launch = json.loads(owner[1]), json.loads(launch[1])
                    (root / 'reload.flag').write_text('reload')
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        with sqlite3.connect(database) as db:
                            owner = db.execute("SELECT state,details FROM workflow_intents WHERE kind='controller_owner'").fetchone()
                            state = db.execute('SELECT state FROM workflow_project WHERE id=1').fetchone()[0]
                            expert = db.execute('SELECT state FROM specialist_tasks').fetchone()[0]
                        marker = root / 'reload.result'
                        if marker.exists() and marker.read_text() != 'ready':
                            raise RuntimeError('Warm reload trigger failed: ' + marker.read_text())
                        if owner and owner[0] == 'ready' and json.loads(owner[1])['fence'] > previous_owner['fence'] and marker.exists():
                            break
                        time.sleep(0.1)
                    current_owner = json.loads(owner[1]) if owner else {}
                    if state != 'running' or expert != 'running' or current_owner.get('pid') != previous_owner['pid'] or current_owner.get('fence', 0) <= previous_owner['fence']:
                        raise RuntimeError('Warm reload changed project state or lost the specialist')
                    ResearchProvider.specialist_gate.set()
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        with sqlite3.connect(database) as db:
                            expert = db.execute('SELECT state,exit_verified FROM specialist_tasks').fetchone()
                            launches = db.execute("SELECT details FROM workflow_intents WHERE kind='specialist_launch'").fetchall()
                        if expert == ('completed', 1):
                            break
                        time.sleep(0.1)
                    current_launch = json.loads(launches[0][0]) if len(launches) == 1 else {}
                    preserved = all(current_launch.get(key) == previous_launch.get(key) for key in ('session_id', 'message_id', 'tools', 'deadline_at'))
                    result = {'same_host_process': current_owner.get('pid') == previous_owner['pid'],
                              'controller_fence_advanced': current_owner.get('fence', 0) > previous_owner['fence'],
                              'run_state_during_reload': state, 'specialist': expert,
                              'specialist_launches': len(launches), 'identity_scope_deadline_preserved': preserved,
                              'used_external_model': False}
                    print(json.dumps(result, sort_keys=True))
                    if expert != ('completed', 1) or len(launches) != 1 or not preserved:
                        raise RuntimeError('Warm specialist recovery did not preserve and settle the original execution')
                    return
                if args.cold_restart_node or args.cold_restart_specialist:
                    waiting = ResearchProvider.specialist_waiting if args.cold_restart_specialist else ResearchProvider.node_waiting
                    if not waiting.wait(timeout=12):
                        raise RuntimeError('Execution did not reach the controlled cold-restart point')
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        with sqlite3.connect(database) as db:
                            task = db.execute('SELECT task_id,state,session_id FROM exploration_tasks').fetchone()
                            launch = db.execute("SELECT state FROM workflow_intents WHERE kind='node_launch'").fetchone()
                            specialist = db.execute('SELECT task_id,state FROM specialist_tasks').fetchone() if args.cold_restart_specialist else None
                            expert_launch = db.execute("SELECT state FROM workflow_intents WHERE kind='specialist_launch'").fetchone() if args.cold_restart_specialist else None
                            reservations_before = db.execute('SELECT COUNT(*) FROM runtime_budget_reservations WHERE session_id=?', (task[2],)).fetchone()[0]
                        if task[1] == 'running' and launch[0] == 'submitted' and (not args.cold_restart_specialist or
                                specialist and specialist[1] == 'running' and expert_launch and expert_launch[0] == 'submitted'):
                            break
                        time.sleep(0.1)
                    if task[1] != 'running' or launch[0] != 'submitted' or args.cold_restart_specialist and (
                            not specialist or specialist[1] != 'running' or not expert_launch or expert_launch[0] != 'submitted'):
                        raise RuntimeError('Node launch was not confirmed before the isolated crash')
                    old_pid = process.pid
                    process.kill()
                    process.wait(timeout=5)
                    ResearchProvider.node_gate.set()
                    ResearchProvider.coordinator_gate.set()
                    ResearchProvider.specialist_gate.set()
                    ResearchProvider.hold_node = False
                    ResearchProvider.hold_specialist = False
                    process = subprocess.Popen(['opencode', 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
                                               cwd=workspace, env=env, stdout=log, stderr=log)
                    deadline = time.monotonic() + 20
                    while True:
                        try:
                            status, _ = request(base, '/api/session/active', password)
                            if status == 200:
                                break
                        except (OSError, urllib.error.URLError):
                            pass
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError('Isolated host did not restart')
                        time.sleep(0.1)
                    activation_status, activation = request(base, '/api/session', password,
                        {'id': 'ses_after_crash_' + uuid4().hex, 'title': 'activate recovered plugin',
                         'location': {'directory': str(workspace)}, 'model': {'providerID': 'ari-probe', 'id': 'probe'}})
                    probe_status, probe = command('status', {})
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        with sqlite3.connect(database) as db:
                            run_state = db.execute('SELECT state FROM workflow_project WHERE id=1').fetchone()[0]
                            task_state = db.execute('SELECT state FROM exploration_tasks WHERE task_id=?', (task[0],)).fetchone()[0]
                            expert_state = db.execute('SELECT state FROM specialist_tasks WHERE task_id=?', (specialist[0],)).fetchone()[0] if specialist else None
                            controller = db.execute("SELECT state,details FROM workflow_intents WHERE kind='controller_owner'").fetchone()
                        if run_state == 'paused' and task_state == 'unverified' and controller and controller[0] == 'ready' and (not specialist or expert_state == 'unverified'):
                            break
                        time.sleep(0.1)
                    if run_state != 'paused' or task_state != 'unverified' or specialist and expert_state != 'unverified' or not controller or json.loads(controller[1])['pid'] != process.pid:
                        log.flush()
                        log.seek(0)
                        print(json.dumps({'run_state': run_state, 'task_state': task_state, 'controller': controller,
                                          'old_pid': old_pid, 'new_pid': process.pid,
                                          'activation_status': activation_status, 'status_rpc': probe_status,
                                          'status_error': probe.get('error'),
                                          'diagnostics': [line for line in log.read().splitlines() if 'Research' in line][-20:]}, sort_keys=True))
                        raise RuntimeError('Cold restart did not preserve an unverified execution and pause the project')
                    _, status = command('status', {})
                    if task[0] not in status.get('message', '') or specialist and specialist[0] not in status.get('message', ''):
                        raise RuntimeError('Recovered status did not expose the unverified task')
                    resume_status, resumed = command('resume', {})
                    if resume_status != 200 or resumed.get('error'):
                        raise RuntimeError(f"Cold-restarted project could not be explicitly resumed: {resumed.get('error')}")
                    time.sleep(0.3)
                    with sqlite3.connect(database) as db:
                        task_after = db.execute('SELECT state FROM exploration_tasks WHERE task_id=?', (task[0],)).fetchone()[0]
                        launch_count = db.execute("SELECT COUNT(*) FROM workflow_intents WHERE kind='node_launch'").fetchone()[0]
                        specialist_after = db.execute('SELECT state FROM specialist_tasks WHERE task_id=?', (specialist[0],)).fetchone()[0] if specialist else None
                        specialist_launch_count = db.execute("SELECT COUNT(*) FROM workflow_intents WHERE kind='specialist_launch'").fetchone()[0]
                        reservations_after = db.execute('SELECT COUNT(*) FROM runtime_budget_reservations WHERE session_id=?', (task[2],)).fetchone()[0]
                    result = {'host_process_changed': old_pid != process.pid, 'state_after_restart': run_state,
                              'task_after_restart': task_state, 'task_after_resume': task_after, 'launch_count': launch_count,
                              'specialist_after_restart': expert_state, 'specialist_after_resume': specialist_after,
                              'specialist_launch_count': specialist_launch_count,
                              'node_reservations_before': reservations_before, 'node_reservations_after': reservations_after,
                              'used_external_model': False}
                    print(json.dumps(result, sort_keys=True))
                    if task_after != 'unverified' or launch_count != 1 or reservations_before != reservations_after or specialist and (specialist_after != 'unverified' or specialist_launch_count != 1):
                        raise RuntimeError('Resume duplicated an unverified node after a genuine host restart')
                    return
                if args.lost_kickoff_response:
                    if not ResearchProvider.node_waiting.wait(timeout=12):
                        raise RuntimeError('Node model did not reach the controlled lost-response point')
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        with sqlite3.connect(database) as db:
                            task = db.execute('SELECT task_id,state,session_id FROM exploration_tasks').fetchone()
                            launches = db.execute("SELECT state,details FROM workflow_intents WHERE kind='node_launch'").fetchall()
                            reservations_before = db.execute('SELECT COUNT(*) FROM runtime_budget_reservations WHERE session_id=?', (task[2],)).fetchone()[0]
                        if task[1] == 'unverified':
                            break
                        time.sleep(0.1)
                    _, status = command('status', {})
                    if task[1] != 'unverified' or len(launches) != 1 or task[0] not in status.get('message', ''):
                        raise RuntimeError('Lost kickoff response did not expose one unverified task')
                    ResearchProvider.node_gate.set()
                    deadline = time.monotonic() + 8
                    outcomes = []
                    while time.monotonic() < deadline:
                        _, context = request(base, f'/api/session/{task[2]}/context', password)
                        outcomes = [item.get('outcome') for item in context.get('data', []) if item.get('type') == 'idle']
                        if outcomes:
                            break
                        time.sleep(0.1)
                    time.sleep(0.3)
                    with sqlite3.connect(database) as db:
                        after = db.execute('SELECT state FROM exploration_tasks WHERE task_id=?', (task[0],)).fetchone()[0]
                        launch_count = db.execute("SELECT COUNT(*) FROM workflow_intents WHERE kind='node_launch'").fetchone()[0]
                        reservations_after = db.execute('SELECT COUNT(*) FROM runtime_budget_reservations WHERE session_id=?', (task[2],)).fetchone()[0]
                    result = {'actions': ResearchProvider.actions, 'task_state': after, 'launch_count': launch_count,
                              'node_reservations_before': reservations_before, 'node_reservations_after': reservations_after,
                              'node_native_outcomes': outcomes, 'status_exposed_task': True, 'used_external_model': False}
                    if after != 'unverified' or launch_count != 1 or reservations_before != reservations_after or not outcomes:
                        raise RuntimeError('An unverified node was automatically resumed or duplicated')
                    checked_status, checked = command('recover', {'kind': 'task', 'task_id': task[0],
                                                                  'reason': 'isolated host and processes verified exited'})
                    if checked_status != 200 or checked.get('error'):
                        raise RuntimeError(f"Could not record the explicit exit check: {checked.get('error')}")
                    limit_status, limit = command('budget', {'limit': 'turns', 'value': '20', 'reason': 'bounded isolated retry'})
                    if limit_status != 200 or limit.get('error'):
                        raise RuntimeError(f"Could not adjust the isolated retry budget: {limit.get('error')}")
                    resume_status, resume = command('resume', {})
                    if resume_status != 200 or resume.get('error'):
                        raise RuntimeError(f"Could not resume the isolated coordinator: {resume.get('error')}")
                    retry_status, retried = command('retry', {'task_id': task[0], 'reason': 'explicit isolated retry'})
                    with sqlite3.connect(database) as db:
                        retry_launches = db.execute("SELECT details FROM workflow_intents WHERE kind='node_launch' ORDER BY CAST(json_extract(details,'$.launch_no') AS INTEGER)").fetchall()
                        retry_state = db.execute('SELECT state,error FROM exploration_tasks WHERE task_id=?', (task[0],)).fetchone()
                    ids = [json.loads(row[0])['message_id'] for row in retry_launches]
                    result['explicit_recovery'] = {'error': retried.get('error'), 'launch_count': len(ids),
                                                    'new_message_id': len(ids) == 2 and ids[0] != ids[1], 'task_state': retry_state}
                    if retry_status != 200 or retried.get('error') or len(ids) != 2 or ids[0] == ids[1]:
                        print(json.dumps(result, sort_keys=True))
                        raise RuntimeError('Explicit verified retry did not create a distinct launch')
                    print(json.dumps(result, sort_keys=True))
                    return
                if args.stop_during_specialist:
                    if not ResearchProvider.specialist_waiting.wait(timeout=12):
                        raise RuntimeError('Specialist model did not reach the controlled stop point')
                    stop_status, stop = command('stop', {})
                    if stop_status != 200 or stop.get('error'):
                        raise RuntimeError(f"Could not stop the isolated run: {stop_status} {stop.get('error')}")
                    deadline = time.monotonic() + 8
                    outcomes = []
                    while time.monotonic() < deadline:
                        with sqlite3.connect(database) as db:
                            state = db.execute('SELECT state FROM workflow_project WHERE id=1').fetchone()[0]
                            tasks = db.execute('SELECT state FROM exploration_tasks').fetchall()
                            specialist = db.execute('SELECT state,exit_verified,child_session_id FROM specialist_tasks').fetchone()
                        _, context = request(base, f'/api/session/{specialist[2]}/context', password)
                        outcomes = [item.get('outcome') for item in context.get('data', []) if item.get('type') == 'idle']
                        if state == 'stopped' and outcomes:
                            break
                        time.sleep(0.1)
                    result = {'actions': ResearchProvider.actions, 'run_state': state,
                              'task_states': [row[0] for row in tasks], 'specialist_state': specialist[0],
                              'specialist_exit_verified': bool(specialist[1]), 'specialist_native_outcomes': outcomes,
                              'used_external_model': False}
                    print(json.dumps(result, sort_keys=True))
                    if (state != 'stopped' or [row[0] for row in tasks] != ['unverified']
                            or specialist[0] not in {'unverified', 'cancelled', 'incomplete'}
                            or specialist[1] and (not outcomes or outcomes[-1] != 'interrupted')):
                        raise RuntimeError('Stop did not preserve the uncertainty of in-flight work')
                    return
                if ResearchProvider.pause_node:
                    if not ResearchProvider.node_waiting.wait(timeout=12):
                        raise RuntimeError('Node model did not reach the controlled pause point')
                    pause_status, pause = command('pause', {})
                    if pause_status != 200 or pause.get('error'):
                        raise RuntimeError(f"Could not pause the isolated research run: {pause_status} {pause.get('error')}")
                    with sqlite3.connect(database) as db:
                        reservations_at_pause = db.execute('SELECT COUNT(*) FROM runtime_budget_reservations').fetchone()[0]
                    ResearchProvider.node_gate.set()
                tasks = []
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if database.exists():
                        with sqlite3.connect(database) as db:
                            tasks = db.execute('SELECT state FROM exploration_tasks').fetchall()
                    if tasks and tasks[0][0] == 'finished':
                        break
                    time.sleep(0.1)
                with sqlite3.connect(database) as db:
                    state = db.execute('SELECT state FROM workflow_project WHERE id=1').fetchone()[0]
                    node_count = db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0]
                    intent_states = db.execute("SELECT kind,state FROM workflow_intents WHERE kind IN ('node_launch','node_finish') ORDER BY kind").fetchall()
                    sessions = db.execute('SELECT role,pause_reason,detached FROM workflow_sessions ORDER BY role').fetchall()
                    role_sessions = db.execute('SELECT role,session_id FROM workflow_sessions ORDER BY role').fetchall()
                    auth_states = db.execute("SELECT kind,state FROM workflow_intents WHERE kind IN ('execution_start','turn_authorization') ORDER BY kind,state").fetchall()
                    specialist_states = db.execute('SELECT state,exit_verified FROM specialist_tasks').fetchall()
                    specialist_launches = db.execute("SELECT state FROM workflow_intents WHERE kind='specialist_launch'").fetchall()
                    reservations_final = db.execute('SELECT COUNT(*) FROM runtime_budget_reservations').fetchone()[0]
                    binding_sources = db.execute("SELECT json_extract(details,'$.binding_source') FROM workflow_intents WHERE kind='execution_start'").fetchall()
                    shells = db.execute("SELECT state,details FROM workflow_intents WHERE kind='native_shell'").fetchall()
                result = {'actions': ResearchProvider.actions, 'run_state': state, 'node_count': node_count,
                          'task_states': [item[0] for item in tasks], 'intents': intent_states,
                          'requests': ResearchProvider.requests, 'sessions': sessions, 'auth_states': auth_states,
                          'specialists': specialist_states, 'specialist_launches': specialist_launches,
                          'paused_before_node_finish': ResearchProvider.pause_node,
                          'reservations_at_pause': reservations_at_pause, 'reservations_final': reservations_final,
                          'background_states': [row[0] for row in shells],
                          'execution_binding_sources': [row[0] for row in binding_sources],
                          'used_external_model': False}
                if args.request_dump:
                    args.request_dump.write_text(json.dumps(ResearchProvider.bodies, indent=2))
                if args.queued_input or args.queued_input_node:
                    finishing_index = queued['bodies_before_release'] - 1 if queued else None
                    handoff = queued_handoff_checks(ResearchProvider.bodies, finishing_index)
                    with sqlite3.connect(database) as db:
                        notes = db.execute("SELECT COUNT(*) FROM notes WHERE body LIKE ?", (f'%{QUEUED_TEXT}%',)).fetchone()[0]
                    target = queued['session'] if queued else None
                    records = ([json.loads(line) for line in handoff_trace.read_text().splitlines() if line.strip()]
                               if handoff_trace.exists() else [])
                    contexts = [item for item in records
                                if item.get('event') == 'context' and item.get('session') == target]
                    events = [item for item in records
                              if item.get('event') == 'event' and item.get('session') == target]
                    delivery = next((item.get('seq') for item in events
                                     if item.get('type') == 'session.inbox.delivered'
                                     and item.get('inbox') == 'msg_queued_user'), None)
                    starts_after = [item for item in events
                                    if item.get('type') == 'session.execution.started'
                                    and delivery is not None and (item.get('seq') or 0) > delivery]
                    normal = [index for index, item in enumerate(contexts)
                              if item.get('queued') and (item.get('tools') or 0) > 0
                              and not item.get('finishing') and not item.get('post_terminal')]
                    post_terminal = [index for index, item in enumerate(contexts) if item.get('post_terminal')]
                    handoff.update({'delivery': queued['delivery'] if queued else None,
                                    'prompt_status': queued['status'] if queued else None,
                                    'target_session': target,
                                    'guidance_notes': notes,
                                    'delivery_seq': delivery,
                                    'new_executions_after_delivery': len(starts_after),
                                    'normal_round': bool(normal),
                                    'normal_round_index': normal[0] if normal else None,
                                    'post_terminal_step': bool(post_terminal),
                                    'post_terminal_index': post_terminal[0] if post_terminal else None,
                                    'context_rounds': contexts})
                    handoff.update(handoff_gate(handoff))
                    result['queued_handoff'] = handoff
                    problems = []
                    if handoff['delivery'] != 'queue':
                        problems.append('the prompt was not queued')
                    if handoff['finishing_had_text']:
                        problems.append('the queued prompt entered the finishing request')
                    if not handoff['text_request_indexes']:
                        problems.append('the queued prompt was never delivered')
                    if handoff['first_text_request_tool_count'] != 0:
                        problems.append('the queued prompt step exposed tools after an accepted terminal action')
                    if not handoff['post_terminal_step']:
                        problems.append('the queued prompt step kept the old finishing instruction')
                    if args.queued_input_node:
                        if handoff['gate'] == 'passed':
                            problems.append('the node case reached a normal round; revisit the blocking record')
                        elif not args.expect_blocked:
                            problems.append(f"blocked by the host contract: {handoff['reason']}; "
                                            'use --expect-blocked to record the block')
                        if notes != 0:
                            problems.append('a closed node segment filed a guidance note')
                    else:
                        if handoff['gate'] != 'passed':
                            problems.append(f"the queued input never reached a normal round: {handoff['reason']}")
                        if notes != 1:
                            problems.append('the guidance note was not filed exactly once')
                    if problems:
                        print(json.dumps(result, sort_keys=True))
                        raise RuntimeError('; '.join(problems))
                if args.context_placement:
                    persisted = ''
                    for role, session in role_sessions:
                        if role not in {'main', 'node_core', 'specialist'}:
                            continue
                        _, context = request(base, f'/api/session/{session}/context', password)
                        persisted += json.dumps(context.get('data', []))
                    result['context_placement'] = context_placement_checks(
                        ResearchProvider.bodies, args.context_placement, persisted)
                    checks = result['context_placement']
                    if args.context_placement == 'tail' and not context_placement_ok(checks):
                        print(json.dumps(result, sort_keys=True))
                        raise RuntimeError('Tail context placement did not reach the wire as expected')
                    if args.context_placement == 'tail' and checks.get('node_finishing_requests', 0) < 1:
                        print(json.dumps(result, sort_keys=True))
                        raise RuntimeError('The scripted node finish did not produce a tool-free finishing request')
                    if (args.queued_input or args.queued_input_node) and checks.get('post_terminal_requests', 0) < 1:
                        print(json.dumps(result, sort_keys=True))
                        raise RuntimeError('The queued input did not produce a post-terminal request')
                    if args.wait_finish:
                        if checks.get('wait_finishing_requests', 0) < 1:
                            print(json.dumps(result, sort_keys=True))
                            raise RuntimeError('The scripted accepted wait did not produce a tool-free finishing request')
                        if ResearchProvider.actions.count('wait') != 1:
                            print(json.dumps(result, sort_keys=True))
                            raise RuntimeError('The scripted model repeated the wait or never called it once')
                if (not tasks or tasks[0][0] != 'finished' or args.specialist and
                        (specialist_states != [('completed', 1)] or specialist_launches != [('submitted',)]) or ResearchProvider.pause_node and
                        (state != 'paused' or reservations_at_pause != reservations_final)):
                    log.flush()
                    log.seek(0)
                    print('\n'.join(line for line in log.read().splitlines() if 'Research execution controller' in line or 'Research controller initialization' in line or 'probe ' in line)[-6000:])
                    raise SystemExit(1)
                if args.background_node and (not {'background_start', 'background_wait', 'node_finish'}.issubset(ResearchProvider.actions)
                        or [row[0] for row in shells] != ['completed']):
                    print(json.dumps(result, sort_keys=True))
                    raise RuntimeError('Background shell did not settle once before node completion')
                if args.resume_after_pause:
                    resume_status, resume = command('resume', {})
                    with sqlite3.connect(database) as db:
                        resume_state = db.execute('SELECT state FROM workflow_project WHERE id=1').fetchone()[0]
                        launch_count = db.execute("SELECT COUNT(*) FROM workflow_intents WHERE kind='node_launch'").fetchone()[0]
                        task_states = [row[0] for row in db.execute('SELECT state FROM exploration_tasks')]
                    result['explicit_resume'] = {'state': resume_state, 'launch_count': launch_count,
                                                 'task_states': task_states, 'error': resume.get('error')}
                    if resume_status != 200 or resume.get('error') or resume_state != 'running' or launch_count != 1 or task_states != ['finished']:
                        raise RuntimeError('Explicit resume did not preserve the completed node')
                if args.lost_report_response:
                    deadline = time.monotonic() + 8
                    delivery = None
                    while time.monotonic() < deadline:
                        with sqlite3.connect(database) as db:
                            delivery = db.execute("SELECT state,details FROM workflow_intents WHERE kind='notification_delivery'").fetchone()
                        if delivery and delivery[0] == 'submitting':
                            break
                        time.sleep(0.1)
                    if not delivery or delivery[0] != 'submitting':
                        raise RuntimeError('Injected report response loss did not leave an uncertain delivery')
                    details = json.loads(delivery[1])
                    _, inbox = request(base, f'/api/session/{sid}/inbox', password)
                    _, context = request(base, f'/api/session/{sid}/context', password)
                    found = any(item.get('id') == details['message_id'] for item in inbox.get('data', []) + context.get('data', []))
                    _, status = command('status', {})
                    if not found or '通知未核实' not in status.get('message', ''):
                        raise RuntimeError('Accepted report was not visible for the operator to verify')
                    recover_status, recover = command('recover', {'kind': 'delivery', 'delivery_kind': 'report',
                        'notification_id': details['notification_id'], 'disposition': 'delivered', 'reason': 'verified in isolated host'})
                    with sqlite3.connect(database) as db:
                        final_delivery = db.execute("SELECT state,details FROM workflow_intents WHERE kind='notification_delivery'").fetchone()
                        notification = db.execute('SELECT state FROM workflow_notifications WHERE notification_id=?', (details['notification_id'],)).fetchone()
                    stable_id = json.loads(final_delivery[1])['message_id'] == details['message_id']
                    result['uncertain_report'] = {'host_accepted': found, 'status_exposed_uncertainty': True,
                                                  'confirmed_state': final_delivery[0], 'notification_state': notification[0],
                                                  'same_message_id': stable_id, 'recovery_error': recover.get('error')}
                    if recover_status != 200 or recover.get('error') or final_delivery[0] != 'delivered' or notification[0] != 'delivered' or not stable_id:
                        raise RuntimeError('Operator-confirmed report recovery failed')
                print(json.dumps(result, sort_keys=True))
            finally:
                ResearchProvider.node_gate.set()
                ResearchProvider.coordinator_gate.set()
                ResearchProvider.specialist_gate.set()
                ResearchProvider.finishing_gate.set()
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
