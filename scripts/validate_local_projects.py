#!/usr/bin/env python3
"""Probe R1 reads on stable copies of existing projects; never open originals writable."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from auto_research.native_store import NativeStore
from auto_research.errors import ValidationError


def files(database):
    return [database] + [p for suffix in ('-wal',) if (p := Path(str(database)+suffix)).exists()]


def hashes(paths):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def inspect(root):
    source = root / '.research/state.sqlite3'
    paths = files(source)
    before = hashes(paths)
    with TemporaryDirectory(prefix='r1-existing-project-') as directory:
        target = Path(directory)
        meta = target / '.research'
        meta.mkdir()
        for p in paths:
            (meta / p.name).write_bytes(p.read_bytes())
        if before != hashes(files(source)) or before != hashes(files(meta/'state.sqlite3')):
            raise RuntimeError(f'Source changed during copying: {root}')
        store = NativeStore(target, readonly=True)
        with store._read() as db:
            integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
            if integrity != 'ok':
                raise RuntimeError(f'Copy integrity failed: {root}: {integrity}')
            schema = db.execute('PRAGMA user_version').fetchone()[0]
            state = db.execute('SELECT state FROM workflow_project').fetchone()[0]
            identity = db.execute("SELECT w.host_id,w.session_id FROM workflow_sessions w JOIN associations a "
                                  "ON w.host_id=a.host_id AND w.session_id=a.session_id "
                                  "WHERE w.role='main' AND w.detached=0 AND a.ended_at IS NULL LIMIT 1").fetchone()
            ledger_before = {table: db.execute(f'SELECT * FROM {table}').fetchall() for table in
                             ('events','knowledge_revisions','relations','review_todos')}
        pages = {}
        for collection in ('critical_now','findings','completed_findings'):
            cursor = None
            count = calls = 0
            started = perf_counter()
            while True:
                page = store.context_page(collection, cursor=cursor, limit=50)
                count += len(page['items'])
                calls += 1
                cursor = page['cursor']
                if cursor is None:
                    break
            pages[collection] = {'total':page['total'],'read_count':count,'pages':calls,
                                 'elapsed_ms':round((perf_counter()-started)*1000,1)}
            if count != page['total']:
                raise RuntimeError(f'Collection incomplete: {root}: {collection}')
        result = {'root':str(root),'schema':schema,'state':state,'pages':pages, 'source_file_sha256':before}
        if identity:
            started = perf_counter()
            shadow = store.context_shadow(identity['host_id'],identity['session_id'],profile='opencode-auto')
            result['host'] = identity['host_id']
            result['shadow_ms'] = round((perf_counter()-started)*1000,1)
            result['context'] = {key:{'characters':len(v['text']),'source_sequence':v['source_sequence'],
                                     'omitted':v['omitted']} for key,v in shadow.items()}
            if shadow['memory-v2']['source_sequence'] != shadow['memory-r1-v1']['source_sequence']:
                raise RuntimeError(f'Shadow sequence mismatch: {root}')
            preview = store.context_preview(identity['session_id'])
            if not preview['available'] or preview['views'] != shadow:
                raise RuntimeError(f'Preview/shadow mismatch: {root}')
            result['preview'] = {'available':preview['available'], 'policy_version':preview['policy_version'],
                                 'has_saved_context':preview['last_recorded'] is not None,
                                 'source_sequence':preview['views']['memory-r1-v1']['source_sequence']}
        with store._read() as db:
            result['copy_ledger_unchanged'] = all(ledger_before[table] == db.execute(f'SELECT * FROM {table}').fetchall()
                                                for table in ledger_before)
        result['original_db_wal_unchanged'] = before == hashes(files(source))
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('roots',nargs='+',type=Path)
    parser.add_argument('--output',type=Path)
    args = parser.parse_args()
    results = []
    for root in args.roots:
        try:
            results.append(inspect(root))
        except (OSError, sqlite3.Error, ValidationError, RuntimeError) as exc:
            results.append({'root':str(root),'error':str(exc)})
    text = json.dumps({'method':'stable database/WAL copies; no frozen object files copied; stored field checks only',
                       'projects':results},ensure_ascii=False,indent=2)+'\n'
    if args.output:
        args.output.write_text(text)
    print(text)
