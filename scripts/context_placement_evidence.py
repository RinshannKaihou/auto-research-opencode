#!/usr/bin/env python3
"""Offline extraction for the context-placement Step 3 evidence.

Two modes:

  extract  read one isolated arm root (research ledger + host DB) and write a
           sanitized record with model usage, wait messages, notifications and
           stable-part digests. It never writes prompt or report text.
  report   read the two sanitized records and print the corrected tables.

The script is evidence tooling, not part of the plugin or the test suite.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import statistics
from collections import Counter
from pathlib import Path

STABLE = {'role_instructions', 'control_instructions', 'identity', 'project', 'task',
          'specialist_task', 'frozen_context', 'method_guidance', 'index'}
KEPT_PHASES = {'native_usage', 'model_request', 'provider_retry', 'coverage_gap'}


def stable_digest(body):
    stable = []
    volatile = 0
    for chunk in body.split('\n\n'):
        match = re.match(r'^## ([^\n]+)\n', chunk)
        if match and match.group(1) in STABLE:
            stable.append(chunk)
        elif match:
            volatile += len(chunk)
    joined = '\n\n'.join(stable)
    return hashlib.sha256(joined.encode()).hexdigest()[:12], len(joined), volatile


def extract(root):
    root = Path(root)
    project = root / 'project'
    ledger = project / '.research' / 'state.sqlite3'
    observations = []
    for path in sorted((project / '.research' / 'observations').glob('*.jsonl')):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get('phase') not in KEPT_PHASES:
                continue
            if row.get('phase') == 'native_usage' and row.get('usage_phase') != 'completed':
                continue
            keep = {key: row[key] for key in
                    ('phase', 'usage_phase', 'source_key', 'session_id', 'tokens', 'cost',
                     'kind', 'model', 'error_code', 'reason', 'will_retry')
                    if key in row}
            observations.append(keep)
    usage = {row['source_key']: row['tokens'] for row in observations
             if row.get('phase') == 'native_usage' and row.get('usage_phase') == 'completed'
             and isinstance(row.get('tokens'), dict)}

    research = sqlite3.connect(ledger)
    research.row_factory = sqlite3.Row
    sessions = {row['session_id']: {'role': row['role'], 'node_id': row['node_id'],
                                    'pause_reason': row['pause_reason']}
                for row in research.execute(
                    'SELECT session_id,role,node_id,pause_reason FROM workflow_sessions')}
    notifications = [dict(row) for row in research.execute(
        'SELECT notification_id,task_id,kind,state,created_at FROM workflow_notifications '
        'ORDER BY rowid')]
    stability = {}
    for row in research.execute('SELECT r.session_id,r.context_id,p.body FROM context_requests r '
                                'JOIN context_packs p USING(pack_id) ORDER BY r.context_id'):
        digest, stable_chars, volatile_chars = stable_digest(row['body'])
        stability.setdefault(row['session_id'], []).append({
            'context_id': row['context_id'], 'digest': digest,
            'stable_chars': stable_chars, 'volatile_chars': volatile_chars})
    conclusion = research.execute('SELECT outcome,final_ref,length(summary) AS summary_chars '
                                  'FROM project_conclusions LIMIT 1').fetchone()
    conclusion = dict(conclusion) if conclusion else None

    host = sqlite3.connect(root / 'data' / 'opencode' / 'opencode.db')
    host.row_factory = sqlite3.Row
    host_sessions = {}
    for row in host.execute('SELECT id,title,tokens_input,tokens_output,tokens_reasoning,'
                            'tokens_cache_read,tokens_cache_write,idle_outcome FROM session_v2'):
        data = dict(row)
        host_sessions[data['id']] = {
            'input': data['tokens_input'], 'output': data['tokens_output'],
            'reasoning': data['tokens_reasoning'], 'cache_read': data['tokens_cache_read'],
            'cache_write': data['tokens_cache_write'], 'idle_outcome': data['idle_outcome'],
        }
    main_messages = {}
    for row in host.execute("SELECT id,session_id,data FROM session_message "
                            "WHERE type='assistant' ORDER BY seq"):
        if sessions.get(row['session_id'], {}).get('role') != 'main':
            continue
        data = json.loads(row['data'])
        tools = [part.get('name') for part in data.get('content', [])
                 if part.get('type') == 'tool']
        main_messages.setdefault(row['session_id'], []).append({
            'id': row['id'], 'created': data.get('time', {}).get('created'),
            'tools': tools, 'usage': usage.get(f"step:{row['session_id']}:{row['id']}"),
        })
    compaction = []
    for row in host.execute("SELECT id,session_id,data FROM session_message WHERE type='compaction'"):
        data = json.loads(row['data'])
        compaction.append({'session_id': row['session_id'], 'id': row['id'],
                           'created': data.get('time', {}).get('created'),
                           'status': data.get('status'), 'reason': data.get('reason'),
                           'tokens': data.get('tokens'), 'cost': data.get('cost')})
    completed = set(usage)
    unknown = [{'session_id': row['session_id'], 'id': row['id']}
               for row in host.execute("SELECT id,session_id FROM session_message "
                                       "WHERE type='assistant'")
               if f"step:{row['session_id']}:{row['id']}" not in completed]

    observed = {}
    for source_key, tokens in usage.items():
        session = source_key.split(':', 2)[1]
        cache = tokens.get('cache') or {}
        entry = observed.setdefault(session, {'requests': 0, 'input': 0, 'cache_read': 0,
                                              'cache_write': 0, 'output': 0, 'reasoning': 0})
        entry['requests'] += 1
        entry['input'] += int(tokens.get('input') or 0)
        entry['cache_read'] += int(cache.get('read') or 0)
        entry['cache_write'] += int(cache.get('write') or 0)
        entry['output'] += int(tokens.get('output') or 0)
        entry['reasoning'] += int(tokens.get('reasoning') or 0)
    for session, host_row in host_sessions.items():
        seen = observed.get(session, {'requests': 0, 'input': 0, 'cache_read': 0,
                                      'cache_write': 0, 'output': 0, 'reasoning': 0})
        host_row['observed'] = seen
        host_row['delta'] = {
            'input': host_row['input'] - seen['input'],
            'cache_read': host_row['cache_read'] - seen['cache_read'],
            'cache_write': host_row['cache_write'] - seen['cache_write'],
            'output': host_row['output'] - seen['output'],
            'reasoning': host_row['reasoning'] - seen['reasoning'],
        }
    return {
        'sessions': sessions, 'host_sessions': host_sessions, 'observed': observed,
        'observations': observations, 'main_messages': main_messages,
        'compaction': compaction, 'unknown_usage': unknown,
        'notifications': notifications, 'stability': stability, 'conclusion': conclusion,
    }


def write_records(record, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'sessions.json').write_text(json.dumps({
        'sessions': record['sessions'],
        'host_sessions': record['host_sessions'],
        'compaction': record['compaction'],
        'unknown_usage': record['unknown_usage'],
        'conclusion': record['conclusion'],
    }, indent=2, ensure_ascii=False))
    (output / 'notifications.json').write_text(json.dumps(record['notifications'], indent=2, ensure_ascii=False))

    def jsonl(name, rows):
        (output / name).write_text(''.join(
            json.dumps(row, separators=(',', ':'), ensure_ascii=False) + '\n' for row in rows))

    jsonl('primary-usage.jsonl', [row for row in record['observations']
                                  if row.get('phase') == 'native_usage'])
    jsonl('model-requests.jsonl', [row for row in record['observations']
                                   if row.get('phase') == 'model_request'])
    jsonl('main-messages.jsonl', [{'session_id': session, **message}
                                  for session, messages in record['main_messages'].items()
                                  for message in messages])
    jsonl('stability.jsonl', [{'session_id': session, **entry}
                              for session, entries in record['stability'].items()
                              for entry in entries])


def _read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def load_records(directory):
    directory = Path(directory)
    summary = json.loads((directory / 'sessions.json').read_text())
    observed = {}
    for row in _read_jsonl(directory / 'primary-usage.jsonl'):
        tokens = row['tokens']
        cache = tokens.get('cache') or {}
        session = row['session_id']
        entry = observed.setdefault(session, {'requests': 0, 'input': 0, 'cache_read': 0,
                                              'cache_write': 0, 'output': 0, 'reasoning': 0})
        entry['requests'] += 1
        entry['input'] += int(tokens.get('input') or 0)
        entry['cache_read'] += int(cache.get('read') or 0)
        entry['cache_write'] += int(cache.get('write') or 0)
        entry['output'] += int(tokens.get('output') or 0)
        entry['reasoning'] += int(tokens.get('reasoning') or 0)
    main_messages = {}
    for message in _read_jsonl(directory / 'main-messages.jsonl'):
        session = message.pop('session_id')
        main_messages.setdefault(session, []).append(message)
    stability = {}
    for entry in _read_jsonl(directory / 'stability.jsonl'):
        session = entry.pop('session_id')
        stability.setdefault(session, []).append(entry)
    return {
        'sessions': summary['sessions'],
        'host_sessions': summary['host_sessions'],
        'compaction': summary['compaction'],
        'unknown_usage': summary['unknown_usage'],
        'conclusion': summary.get('conclusion'),
        'notifications': json.loads((directory / 'notifications.json').read_text()),
        'main_messages': main_messages,
        'stability': stability,
        'observed': observed,
    }


def totals(entries):
    keys = ('requests', 'input', 'cache_read', 'cache_write', 'output', 'reasoning')
    return {key: sum(entry.get(key, 0) for entry in entries) for key in keys}


def add_prompt(entry):
    entry['prompt'] = entry['input'] + entry['cache_read'] + entry['cache_write']
    entry['hit_ratio'] = (entry['cache_read'] / entry['prompt']) if entry['prompt'] else None
    return entry


def wait_report(record):
    report = {}
    for session, messages in record['main_messages'].items():
        if record['sessions'].get(session, {}).get('role') != 'main':
            continue
        waits = [message for message in messages
                 if 'research_wait' in message.get('tools', [])]
        non_wait = [message for message in messages if message not in waits]
        wait_usage = [message for message in waits if message.get('usage')]
        non_wait_usage = [message for message in non_wait if message.get('usage')]
        def sum_messages(items):
            entry = {'requests': 0, 'input': 0, 'cache_read': 0, 'cache_write': 0,
                     'output': 0, 'reasoning': 0}
            for message in items:
                tokens = message['usage']
                cache = tokens.get('cache') or {}
                entry['requests'] += 1
                entry['input'] += int(tokens.get('input') or 0)
                entry['cache_read'] += int(cache.get('read') or 0)
                entry['cache_write'] += int(cache.get('write') or 0)
                entry['output'] += int(tokens.get('output') or 0)
                entry['reasoning'] += int(tokens.get('reasoning') or 0)
            return add_prompt(entry)
        times = [message['created'] for message in waits if message.get('created')]
        gaps = [(b - a) / 1000 for a, b in zip(times, times[1:])]
        first_finish = next((row['created_at'] for row in record['notifications']
                             if row['kind'] == 'finished'), None)
        before = None
        if first_finish and times:
            before = sum(1 for item in times if item < _ms(first_finish))
        report[session] = {
            'messages': len(messages), 'wait_messages': len(waits),
            'wait_only': sum(1 for message in waits if len(message.get('tools', [])) == 1),
            'mixed': sum(1 for message in waits if len(message.get('tools', [])) > 1),
            'waits_before_first_finish': before,
            'waits_after_first_finish': None if before is None else len(waits) - before,
            'gap_seconds': {'min': min(gaps) if gaps else None,
                            'median': statistics.median(gaps) if gaps else None,
                            'max': max(gaps) if gaps else None,
                            'under_1s': sum(1 for gap in gaps if gap < 1)},
            'wait': sum_messages(wait_usage),
            'wait_without_usage': len(waits) - len(wait_usage),
            'non_wait': sum_messages(non_wait_usage),
            'non_wait_without_usage': len(non_wait) - len(non_wait_usage),
        }
    return report


def _ms(timestamp):
    from datetime import datetime
    return datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp() * 1000


def summarize(record):
    sessions = record['sessions']
    completed_compactions = [item for item in record['compaction']
                             if item.get('status') == 'completed']
    aux_count = Counter(item['session_id'] for item in completed_compactions)
    observed_by_role = {}
    for session, entry in record['observed'].items():
        role = sessions.get(session, {}).get('role', 'unknown')
        bucket = observed_by_role.setdefault(role, {'requests': 0, 'input': 0, 'cache_read': 0,
                                                    'cache_write': 0, 'output': 0, 'reasoning': 0})
        for key in bucket:
            bucket[key] += entry.get(key, 0)
    whole_by_role = {}
    for session, entry in record['host_sessions'].items():
        role = sessions.get(session, {}).get('role', 'unknown')
        bucket = whole_by_role.setdefault(role, {'requests': 0, 'input': 0, 'cache_read': 0,
                                                 'cache_write': 0, 'output': 0, 'reasoning': 0})
        bucket['requests'] += entry['observed']['requests'] + aux_count.get(session, 0)
        for key in ('input', 'cache_read', 'cache_write', 'output', 'reasoning'):
            bucket[key] += entry[key]
    for bucket in observed_by_role.values():
        add_prompt(bucket)
    for bucket in whole_by_role.values():
        add_prompt(bucket)
    whole_totals = totals(record['host_sessions'].values())
    whole_totals['requests'] = sum(bucket['requests'] for bucket in whole_by_role.values())
    return {
        'primary': {'by_role': observed_by_role, 'totals': add_prompt(totals(record['observed'].values()))},
        'whole_run_known': {'by_role': whole_by_role, 'totals': add_prompt(whole_totals)},
        'unknown_usage': record['unknown_usage'],
        'compaction': record['compaction'],
        'waits': wait_report(record),
        'stability': {session: {
            'requests': len(entries), 'distinct': len({entry['digest'] for entry in entries}),
            'changes': sum(1 for a, b in zip(entries, entries[1:]) if a['digest'] != b['digest']),
        } for session, entries in record['stability'].items()},
        'conclusion': record['conclusion'],
    }


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    extract_parser = sub.add_parser('extract')
    extract_parser.add_argument('--root', required=True, type=Path)
    extract_parser.add_argument('--output', required=True, type=Path,
                                help='directory for the sanitized record files')
    report_parser = sub.add_parser('report')
    report_parser.add_argument('records', nargs='+', type=Path,
                               help='record directories written by extract')
    report_parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command == 'extract':
        record = extract(args.root)
        write_records(record, args.output)
        print(json.dumps(summarize(record), indent=2, ensure_ascii=False))
    else:
        report = {}
        for path in args.records:
            record = load_records(path)
            report[Path(path).name] = summarize(record)
        text = json.dumps(report, indent=2, ensure_ascii=False)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text)
        print(text)


if __name__ == '__main__':
    main()
