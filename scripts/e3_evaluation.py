#!/usr/bin/env python3
"""Prepare isolated paired E3 tasks; serve measurements; explicitly run or score."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import random
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


VERSION = 'e3-evaluation-v1'
TASKS = {
    'E3-01-combination': {
        'goal': '在 steady 负载下评估 A、B 和 A+B，选择满足正确性检查的最低平均延迟实现；如果组合退化，保留可恢复的单独实现。',
        'condition': 'steady', 'limit': 4, 'variants': {'baseline': 100, 'A': 70, 'B': 80, 'AB': 120},
        'expected': 'A', 'required': ['A', 'B', 'AB'],
        'public': 'A 与 B 分别修改不同模块，可无冲突地合并。文件兼容不能证明效果相加。请实际评估组合并给出退化后的选择；不要改测量环境。'},
    'E3-02-reactivation': {
        'goal': '生产条件变为 burst 后重新判断已暂缓路线 B；保存旧负结果的条件，提出当前采用方案及恢复理由。',
        'condition': 'burst', 'limit': 3, 'variants': {'baseline': 130, 'A': 140, 'B': 65, 'AB': 150},
        'expected': 'B', 'required': ['A', 'B'],
        'public': '旧 steady 记录：同一基线下 A=70 ms、B=90 ms，因此 B 暂缓；恢复条件是工作负载改变。当前生产负载为 burst，旧测量不能直接作为新条件下的排名。'},
    'E3-03-shared-async': {
        'goal': '用一次公共校准支撑 A/B 比较；两条路线可异步返回。最终选择必须考虑有效迟到证据，并说明临时判断与最终判断的关系。',
        'condition': 'production', 'limit': 3, 'variants': {'A': 80, 'B': 60},
        'expected': 'B', 'required': ['A', 'B'],
        'public': '首次测量需要公共 calibrate。校准对 A/B 通用；重复校准相同基线会返回相同身份，费用只记一次。A 通常先返回，B 较慢；先完成不等于更优。'},
    'E3-04-direct': {
        'goal': '将 delivery.py 中函数 recieve 更名为 receive，保持返回内容；运行现有测试，交付结果。',
        'condition': 'direct', 'limit': 0, 'variants': {}, 'expected': None, 'required': [],
        'public': '这是明确交付任务，不需要生成替代路线。运行 python3 -B -m unittest -q。'},
}

CLIENT = '''#!/usr/bin/env python3
import argparse, json
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
p=argparse.ArgumentParser(description="Read one controlled measurement; no model calls")
p.add_argument("action",choices=["calibrate","measure"])
p.add_argument("--variant",choices=["baseline","A","B","AB"])
a=p.parse_args()
c=json.loads(Path(__file__).with_name("measurement.json").read_text())
if not c.get("url"): raise SystemExit("Operator must start the campaign measurement broker first")
q=Request(c["url"]+"/measure",data=json.dumps({"token":c["token"],"action":a.action,"variant":a.variant}).encode(),headers={"Content-Type":"application/json"})
try:
    with urlopen(q,timeout=30) as r: print(r.read().decode())
except HTTPError as e:
    print(e.read().decode()); raise SystemExit(2)
'''


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def configuration(value):
    model, limits = value.get('model', {}), value.get('limits', {})
    if not all(isinstance(model.get(k), str) and model[k].strip() for k in ('provider', 'model', 'variant')):
        raise ValueError('Freeze provider, model and variant explicitly; no default paid model')
    if not isinstance(value.get('tools'), list) or not value['tools'] or not value.get('context_policy'):
        raise ValueError('Freeze tools and context_policy explicitly')
    for key in ('max_model_requests', 'max_tokens', 'max_wall_seconds', 'max_measurements'):
        if type(limits.get(key)) is not int or limits[key] <= 0:
            raise ValueError(f'limits.{key} must be a positive integer')
    cost = limits.get('max_cost')
    if cost is not None and (type(cost) not in {int, float} or not 0 < cost < float('inf')):
        raise ValueError('max_cost must be positive or explicit null (unmetered/unknown)')
    arms = value.get('arms', {})
    if set(arms) != {'e1e2', 'e3'} or any(not arm.get('revision') for arm in arms.values()):
        raise ValueError('Freeze exact e1e2/e3 software revisions')
    for arm in arms.values():
        command = arm.get('command')
        if command is not None and (not isinstance(command, list) or not command
                or any(not isinstance(part, str) for part in command)):
            raise ValueError('command must be an explicit argv array or null; no implicit runner')
    return value


def freeze(config, destination, tasks=None, repeats=3, seed=17):
    config = configuration(config)
    root = Path(destination).resolve()
    if repeats < 1:
        raise ValueError('repeats must be positive')
    selected = tasks or list(TASKS)
    if any(task not in TASKS for task in selected):
        raise ValueError('Unknown evaluation task')
    root.mkdir()
    (root / 'private').mkdir()
    (root / 'projects').mkdir()
    runs = []
    order = [(task, repeat, arm) for task in selected for repeat in range(repeats) for arm in config['arms']]
    random.Random(seed).shuffle(order)
    for index, (task_id, repeat, arm) in enumerate(order):
        run_id = f'run-{index + 1:03d}'
        spec = TASKS[task_id]
        project = root / 'projects' / run_id
        project.mkdir()
        pair_seed = int(digest([seed, task_id, repeat])[:8], 16)
        token = secrets.token_urlsafe(32)
        lines = [f"# {spec['goal']}", '', spec['public'], '',
                 f"当前条件：{spec['condition']}。测量工作额度：{min(config['limits']['max_measurements'], spec['limit'])}。",
                 '模型调用、token 和墙钟上限由评估宿主的固定配置约束；未知用量不得按零报告。', '',
                 '## 执行与交付',
                 '普通节点执行 `python3 measure.py calibrate` 或 `python3 measure.py measure --variant A`（B/AB/baseline 同理）。',
                 '测量端只暴露当前条件，返回固定基线、数据/负载、指标/单位、环境、重复与不确定性。',
                 '将最终判断写入 answer.json：status、choice、condition、measurement_refs、limitations、stop_reason；',
                 '涉及恢复时附 reactivation_reason；发生临时选择时可附 provisional_history。保留正常研究账本和报告。',
                 'measurement_refs 填实际返回的 measurement_id，不能用文件名或未到结果代替。',
                 '本任务不授予确认性资格；可诚实提交 partial/unresolved 和缺口。']
        if task_id == 'E3-04-direct':
            lines = [f"# {spec['goal']}", '', spec['public'], '',
                     '将最终交付记录写入 answer.json：status、condition="direct"、measurement_refs=[]、limitations、stop_reason。',
                     '记录真实测试结果；没有测量任务，不需要调用测量服务或生成替代算法。']
            (project / 'delivery.py').write_text('def recieve(name):\n    return f"received {name}"\n', encoding='utf-8')
            (project / 'test_delivery.py').write_text('import unittest\nfrom delivery import receive\n'
                'class DeliveryTest(unittest.TestCase):\n    def test_output(self):\n'
                '        self.assertEqual(receive("work"), "received work")\n', encoding='utf-8')
        else:
            (project / 'measure.py').write_text(CLIENT, encoding='utf-8')
            write(project / 'measurement.json', {'url': None, 'token': token})
        (project / 'TASK.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        runs.append({'run_id': run_id, 'task_id': task_id, 'repeat': repeat, 'arm': arm,
                     'pair_seed': pair_seed, 'token': token, 'project': str(project),
                     'input_digest': digest({'task': spec['goal'], 'public': spec['public'], 'seed': pair_seed}),
                     'state': 'prepared', 'usage': {'tokens': None, 'cost': None, 'model_requests': None}})
    manifest = {'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
                'config': config, 'task_spec_digest': digest(TASKS), 'seed': seed, 'runs': runs,
                'real_model_effect': 'not_run', 'blind_scoring': 'manual criteria outside research projects'}
    write(root / 'private' / 'manifest.json', manifest)
    write(root / 'private' / 'scoring-key.json', TASKS)
    write(root / 'private' / 'measurements.json', {'runs': {run['run_id']: {'used': 0, 'events': [], 'cache': {}} for run in runs}})
    return root / 'private' / 'manifest.json'


class Broker:
    def __init__(self, manifest_path):
        self.manifest_path = Path(manifest_path).resolve()
        self.manifest = json.loads(self.manifest_path.read_text())
        self.state_path = self.manifest_path.with_name('measurements.json')
        self.state = json.loads(self.state_path.read_text())
        self.lock = threading.Lock()
        self.waiters = {}

    def measure(self, request):
        run = next((run for run in self.manifest['runs'] if secrets.compare_digest(run['token'], request.get('token', ''))), None)
        if not run:
            return 403, {'error': 'unknown_run'}
        action, variant = request.get('action'), request.get('variant')
        spec = TASKS[run['task_id']]
        if action not in {'calibrate', 'measure'} or action == 'measure' and variant not in spec['variants']:
            return 400, {'error': 'unavailable_measurement'}
        key = 'calibrate' if action == 'calibrate' else 'measure:' + variant
        state = self.state['runs'][run['run_id']]
        with self.lock:
            cached = state['cache'].get(key)
            if cached:
                if cached['state'] == 'complete':
                    state['events'].append({'kind': 'reuse', 'key': key, 'at': time.time()})
                    write(self.state_path, self.state)
                    return 200, {**cached['result'], 'reused': True}
                waiter = self.waiters.get((run['run_id'], key))
                if waiter is None:
                    return 409, {'error': 'measurement_exit_unknown', 'reserved_units': 1}
            else:
                if action == 'measure' and spec['condition'] != 'direct' and 'calibrate' not in state['cache']:
                    return 409, {'error': 'shared_calibration_required'}
                if action == 'measure' and state['cache']['calibrate']['state'] != 'complete':
                    return 409, {'error': 'calibration_not_ready'}
                limit = min(self.manifest['config']['limits']['max_measurements'], spec['limit'])
                if state['used'] >= limit:
                    state['events'].append({'kind': 'budget_rejected', 'key': key, 'at': time.time()})
                    write(self.state_path, self.state)
                    return 409, {'error': 'measurement_budget_exhausted', 'used': state['used'], 'limit': limit}
                state['used'] += 1
                state['cache'][key] = {'state': 'pending'}
                state['events'].append({'kind': 'begin', 'key': key, 'at': time.time(), 'work_units': 1})
                waiter = self.waiters[(run['run_id'], key)] = threading.Event()
                write(self.state_path, self.state)
                cached = None
        if cached:
            if not waiter.wait(10):
                return 409, {'error': 'measurement_still_pending'}
            with self.lock:
                return 200, {**state['cache'][key]['result'], 'reused': True}
        # Deliberately outside the allocation lock: independent measurements can return out of order.
        time.sleep(0.7 if run['task_id'] == 'E3-03-shared-async' and variant == 'B' else 0.04)
        result = {'measurement_id': 'M-' + digest([run['pair_seed'], key])[:16],
                  'baseline_ref': 'fixture/baseline@1', 'data_ref': 'fixture/' + spec['condition'] + '@1',
                  'workload': spec['condition'], 'metric': 'request_latency_mean', 'unit': 'ms',
                  'environment': 'isolated-synthetic-service-v1', 'repeats': 5,
                  'uncertainty': {'kind': 'deterministic_fixture_range', 'half_width': 0.5},
                  'correctness': 'pass', 'synthetic_measurement': True, 'variant': variant, 'action': action}
        if action == 'measure':
            generator = random.Random(run['pair_seed'] ^ int(digest(variant)[:8], 16))
            result['samples'] = [round(spec['variants'][variant] + generator.uniform(-0.5, 0.5), 3) for _ in range(5)]
            result['mean'] = sum(result['samples']) / len(result['samples'])
        with self.lock:
            state['cache'][key] = {'state': 'complete', 'result': result}
            state['events'].append({'kind': 'complete', 'key': key, 'at': time.time(),
                                    'measurement_id': result['measurement_id']})
            write(self.state_path, self.state)
            waiter.set()
        return 200, result

    def server(self):
        broker = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                if self.path != '/measure':
                    self.send_error(404)
                    return
                try:
                    request = json.loads(self.rfile.read(min(int(self.headers.get('Content-Length', 0)), 8192)))
                    status, result = broker.measure(request)
                except (ValueError, TypeError, AttributeError):
                    status, result = 400, {'error': 'invalid_measurement_request'}
                body = json.dumps(result).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        for run in self.manifest['runs']:
            config = Path(run['project']) / 'measurement.json'
            if config.exists():
                write(config, {'url': f'http://127.0.0.1:{server.server_port}', 'token': run['token']})
        return server


def score(manifest_path, run_id):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    run = next(run for run in manifest['runs'] if run['run_id'] == run_id)
    spec = TASKS[run['task_id']]
    answer_file = Path(run['project']) / 'answer.json'
    answer_error = None
    try:
        answer = json.loads(answer_file.read_text())
        if not isinstance(answer, dict):
            raise ValueError('answer.json must contain an object')
    except (OSError, ValueError) as error:
        answer, answer_error = {}, str(error)
    measurements = json.loads(manifest_path.with_name('measurements.json').read_text())['runs'][run_id]
    complete = [item['result'] for item in measurements['cache'].values() if item['state'] == 'complete']
    raw_refs = answer.get('measurement_refs', [])
    refs_valid = isinstance(raw_refs, list) and all(isinstance(ref, str) for ref in raw_refs)
    refs = raw_refs if refs_valid else []
    fixed = {item['measurement_id']: item for item in complete}
    claimed = [fixed[ref] for ref in refs if ref in fixed]
    checks = {'answer_present': bool(answer), 'reference_format_valid': refs_valid,
              'scope_declared': answer.get('condition') == spec['condition'],
              'refs_exist_in_this_run': bool(refs) and all(ref in fixed for ref in refs),
              'required_measurements_considered': all(any(item.get('variant') == variant for item in claimed) for variant in spec['required']),
              'measurement_budget_respected': not any(item['kind'] == 'budget_rejected' for item in measurements['events'])}
    if run['task_id'] == 'E3-04-direct':
        try:
            test = subprocess.run([sys.executable, '-B', '-m', 'unittest', '-q'], cwd=run['project'],
                                  capture_output=True, text=True, timeout=10)
            passed = test.returncode == 0
        except subprocess.TimeoutExpired:
            passed = False
        checks.update({'direct_tests_pass': passed, 'refs_exist_in_this_run': not refs})
    else:
        checks['choice_matches_fixture'] = answer.get('choice') == spec['expected']
    if run['task_id'] == 'E3-02-reactivation':
        checks['reactivation_reason_provided'] = bool(answer.get('reactivation_reason'))
    return {'version': VERSION, 'run_id': run_id, 'task_id': run['task_id'],
            'automatic_checks': checks, 'automatic_pass': all(checks.values()), 'answer_error': answer_error,
            'work_units': measurements['used'], 'usage': run['usage'],
            'manual_quality_score': None, 'manual_review_required': True,
            'observed_model_match': run.get('configuration_match'),
            'observed_software_match': run.get('software_match'),
            'paired_effect_eligible': False,
            'quality_note': 'Identity/fixture checks do not grade reasoning, honest limitations, or general research quality.',
            'group_blind': True, 'real_model_effect': manifest['real_model_effect']}


def execute(manifest_path, run_id, approved=False):
    if not approved:
        raise ValueError('Explicit --execute is required; no model service is selected or called by preparation')
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    run = next(run for run in manifest['runs'] if run['run_id'] == run_id)
    command = manifest['config']['arms'][run['arm']].get('command')
    if not command:
        raise ValueError('No frozen command for this arm; use an explicitly configured isolated host')
    if run['state'] != 'prepared':
        raise ValueError('Run already attempted; preserve it and prepare a new paired repeat')
    replacements = {'project': run['project'], **manifest['config']['model']}
    argv = []
    for part in command:
        for name, value in replacements.items():
            part = part.replace('{' + name + '}', value)
        argv.append(part)
    run.update(state='started', started_at=time.time(), executed_argv=argv)
    write(manifest_path, manifest)
    private = manifest_path.parent
    environment = dict(os.environ)
    for name in ('OPENCODE_SERVER_URL', 'OPENCODE_CONFIG', 'OPENCODE_CONFIG_CONTENT'):
        environment.pop(name, None)
    runtime = private / 'runtime' / run_id
    for variable, directory in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                                ('XDG_STATE_HOME', 'state'), ('XDG_CACHE_HOME', 'cache')]:
        target = runtime / directory
        target.mkdir(parents=True, exist_ok=True)
        environment[variable] = str(target)
    host_config = manifest['config']['arms'][run['arm']].get('host_config')
    if host_config:
        environment['OPENCODE_CONFIG'] = str(Path(host_config).resolve())
    try:
        result = subprocess.run(argv, cwd=run['project'], capture_output=True, text=True,
            timeout=manifest['config']['limits']['max_wall_seconds'], env=environment)
        (private / f'{run_id}-stdout.txt').write_text(result.stdout, encoding='utf-8')
        (private / f'{run_id}-stderr.txt').write_text(result.stderr, encoding='utf-8')
        run.update(state='completed' if result.returncode == 0 else 'failed', returncode=result.returncode)
    except subprocess.TimeoutExpired as error:
        run.update(state='interrupted', interruption='runner_wall_limit', external_exit='unknown')
        (private / f'{run_id}-stdout.txt').write_bytes(error.stdout or b'')
        (private / f'{run_id}-stderr.txt').write_bytes(error.stderr or b'')
    except OSError as error:
        run.update(state='failed', launcher_error=str(error), external_exit='not_started')
        (private / f'{run_id}-stderr.txt').write_text(str(error), encoding='utf-8')
    except KeyboardInterrupt:
        run.update(state='interrupted', interruption='operator_interrupted_runner', external_exit='unknown')
        raise
    finally:
        run.update(ended_at=time.time(), elapsed_seconds=time.time() - run['started_at'])
        manifest['real_model_effect'] = 'external_runs_present_unverified'
        write(manifest_path, manifest)
    return {'run_id': run_id, 'state': run['state'], 'usage': run['usage']}


def record(manifest_path, run_id, result_path):
    """Import operator-side raw run observations; retain missing usage as unknown."""
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    run = next(run for run in manifest['runs'] if run['run_id'] == run_id)
    result = json.loads(Path(result_path).read_text())
    if result.get('state') not in {'completed', 'failed', 'interrupted'}:
        raise ValueError('Record completed/failed/interrupted, including unsuccessful runs')
    usage = result.get('usage') or {}
    for field in ('tokens', 'cost', 'model_requests'):
        value = usage.get(field)
        if value is not None and (type(value) not in {int, float} or not 0 <= value < float('inf')):
            raise ValueError('Observed usage must be nonnegative finite numbers or null unknown')
    observed_model = result.get('observed_model')
    result['model_matches_frozen_configuration'] = (observed_model == manifest['config']['model']) if observed_model else None
    result['declared_software_revision'] = manifest['config']['arms'][run['arm']]['revision']
    result['software_matches_frozen_configuration'] = (result.get('observed_software_revision') == result['declared_software_revision']
                                                      if result.get('observed_software_revision') else None)
    archived = manifest_path.parent / f'{run_id}-observations-{time.time_ns()}.json'
    write(archived, result)
    run.update(state=result['state'], usage={field: usage.get(field) for field in ('tokens', 'cost', 'model_requests')},
               observed_model=observed_model, observations=str(archived),
               configuration_match=result['model_matches_frozen_configuration'],
               software_match=result['software_matches_frozen_configuration'])
    manifest['real_model_effect'] = 'recorded_runs_require_independent_scoring'
    write(manifest_path, manifest)
    return {'run_id': run_id, 'state': run['state'], 'usage': run['usage'], 'model_match': run['configuration_match'],
            'software_match': run['software_match'], 'quality_effect': 'unverified'}


def selftest(output):
    """Exercise real subprocess/HTTP fixtures with scripted answers, never a model."""
    with tempfile.TemporaryDirectory(prefix='e3-evaluation-selftest-') as temp:
        stub_command = [sys.executable, '-c', 'import os,json; print(json.dumps(dict(config=os.environ.get("XDG_CONFIG_HOME"),server=os.environ.get("OPENCODE_SERVER_URL"))))']
        config = {'model': {'provider': 'fixture', 'model': 'no-model', 'variant': 'scripted'},
                  'context_policy': 'memory-r1-v1', 'tools': ['research', 'python'],
                  'limits': {'max_model_requests': 20, 'max_tokens': 10000, 'max_cost': None,
                             'max_wall_seconds': 60, 'max_measurements': 4},
                  'arms': {'e1e2': {'revision': 'fixture-baseline', 'command': stub_command},
                           'e3': {'revision': 'fixture-treatment', 'command': stub_command}}}
        path = freeze(config, Path(temp) / 'campaign', repeats=1)
        broker = Broker(path)
        server = broker.server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        reports, async_checked, budget_checked = [], False, False
        try:
            for run in broker.manifest['runs']:
                project = Path(run['project'])
                spec = TASKS[run['task_id']]
                def request(action, variant=None):
                    body = json.dumps({'token': run['token'], 'action': action, 'variant': variant}).encode()
                    with urlopen(Request(f'http://127.0.0.1:{server.server_port}/measure', data=body,
                                         headers={'Content-Type': 'application/json'}), timeout=10) as response:
                        return json.loads(response.read())
                results = []
                if spec['variants']:
                    client = subprocess.run([sys.executable, 'measure.py', 'calibrate'], cwd=project, capture_output=True, text=True, timeout=10)
                    assert client.returncode == 0, client.stderr
                    first = json.loads(client.stdout)
                    reused = request('calibrate')
                    assert first['measurement_id'] == reused['measurement_id'] and reused['reused']
                    if run['task_id'] == 'E3-03-shared-async':
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            results = list(pool.map(lambda variant: request('measure', variant), ['A', 'B']))
                        events = broker.state['runs'][run['run_id']]['events']
                        begun = [event['at'] for event in events if event['kind'] == 'begin' and event['key'].startswith('measure:')]
                        done = [event['at'] for event in events if event['kind'] == 'complete' and event['key'].startswith('measure:')]
                        assert max(begun) < min(done)
                        assert broker.state['runs'][run['run_id']]['used'] == 3
                        async_checked = True
                    else:
                        results = [request('measure', variant) for variant in spec['required']]
                else:
                    (project / 'delivery.py').write_text('def receive(name):\n    return f"received {name}"\n', encoding='utf-8')
                answer = {'status': 'answered', 'condition': spec['condition'], 'choice': spec['expected'],
                          'measurement_refs': [result['measurement_id'] for result in results],
                          'limitations': ['Synthetic task only'], 'stop_reason': 'Assigned scope measured',
                          'reactivation_reason': 'Workload changed from steady to burst'}
                write(project / 'answer.json', answer)
                report = score(path, run['run_id'])
                assert report['automatic_pass'], report
                # A plausible-looking answer with an unreceived result must fail.
                write(project / 'answer.json', {**answer, 'measurement_refs': ['M-not-received']})
                assert not score(path, run['run_id'])['automatic_pass']
                (project / 'answer.json').write_text('invalid-json', encoding='utf-8')
                assert score(path, run['run_id'])['answer_error']
                write(project / 'answer.json', answer)
                reports.append(report)
            try:
                execute(path, broker.manifest['runs'][0]['run_id'])
                raise AssertionError('Implicit model runner was accepted')
            except ValueError as error:
                assert '--execute' in str(error)
            sample_run = broker.manifest['runs'][0]['run_id']
            launched = execute(path, sample_run, approved=True)
            assert launched['state'] == 'completed'
            environment = json.loads((path.parent / f'{sample_run}-stdout.txt').read_text())
            assert environment['server'] is None and str(path.parent / 'runtime') in environment['config']
            imported = path.parent / 'synthetic-observations.json'
            write(imported, {'state': 'interrupted', 'usage': {'tokens': None, 'cost': None},
                             'observed_model': {'provider': 'different', 'model': 'stub', 'variant': 'stub'}})
            observation = record(path, sample_run, imported)
            assert observation['model_match'] is False and observation['usage']['tokens'] is None
            sample = next(run for run in broker.manifest['runs'] if run['task_id'] == 'E3-01-combination')
            status, result = broker.measure({'token': sample['token'], 'action': 'measure', 'variant': 'baseline'})
            assert status == 409 and result['error'] == 'measurement_budget_exhausted'
            budget_checked = True
            assert all(not (Path(run['project']) / 'scoring-key.json').exists() for run in broker.manifest['runs'])
            for task_id in TASKS:
                paired = [run for run in broker.manifest['runs'] if run['task_id'] == task_id]
                assert len({run['pair_seed'] for run in paired}) == 1
                assert len({(Path(run['project']) / 'TASK.md').read_text() for run in paired}) == 1
                left, right = (broker.state['runs'][run['run_id']]['cache'] for run in paired)
                assert {key: value['result'] for key, value in left.items()} == {
                    key: value['result'] for key, value in right.items()}
            trace = Path(output).with_name('selftest-measurements.json')
            write(trace, {'kind': 'scripted_fixture_http_measurements', 'real_model_effect': 'not_run',
                          'runs': broker.state['runs']})
            summary = {'version': VERSION, 'kind': 'synthetic_fixture_and_scorer_selftest', 'real_model_effect': 'not_run',
                       'runs_checked': len(reports), 'tasks_checked': sorted(TASKS), 'shared_once': True,
                       'async_overlap': async_checked, 'budget_rejection': budget_checked,
                       'fabricated_reference_rejected': True, 'implicit_execution_rejected': True,
                       'explicit_no_model_runner_checked': True, 'isolated_xdg_checked': True,
                       'configuration_mismatch_retained': True, 'missing_usage_remains_unknown': True,
                       'paired_inputs_and_measurements_match': True, 'measurement_trace': trace.name,
                       'scoring_key_outside_projects': True, 'automatic_reports': reports}
            write(output, summary)
            return summary
        finally:
            server.shutdown()
            server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('freeze')
    create.add_argument('--configuration', type=Path, required=True)
    create.add_argument('--destination', type=Path, required=True)
    create.add_argument('--repeats', type=int, default=3)
    create.add_argument('--seed', type=int, default=17)
    create.add_argument('--tasks', nargs='+', choices=list(TASKS))
    serve = sub.add_parser('serve')
    serve.add_argument('--manifest', type=Path, required=True)
    for name in ('run', 'score'):
        command = sub.add_parser(name)
        command.add_argument('--manifest', type=Path, required=True)
        command.add_argument('--run-id', required=True)
        command.add_argument('--output', type=Path)
        if name == 'run':
            command.add_argument('--execute', action='store_true')
    ingest = sub.add_parser('record')
    ingest.add_argument('--manifest', type=Path, required=True)
    ingest.add_argument('--run-id', required=True)
    ingest.add_argument('--result', type=Path, required=True)
    ingest.add_argument('--output', type=Path)
    check = sub.add_parser('selftest')
    check.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'freeze':
        path = freeze(json.loads(args.configuration.read_text()), args.destination, args.tasks, args.repeats, args.seed)
        print(f'Prepared paired projects; private manifest: {path}')
    elif args.action == 'serve':
        server = Broker(args.manifest).server()
        print(f'Measurement broker on localhost:{server.server_port}; no model calls', flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    else:
        result = (record(args.manifest, args.run_id, args.result) if args.action == 'record' else
                  selftest(args.output) if args.action == 'selftest' else
                  execute(args.manifest, args.run_id, args.execute) if args.action == 'run' else score(args.manifest, args.run_id))
        if args.output:
            write(args.output, result)
        print(json.dumps({'action': args.action, 'result': 'saved' if args.output else result}, ensure_ascii=False))


if __name__ == '__main__':
    main()
