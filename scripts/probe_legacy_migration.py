"""Verify schema migration on a private copy of a stable historical project."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile


def snapshot(db):
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    result = {}
    for table in tables:
        columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")') if row[1] != 'control_revision']
        selection = ','.join('"' + item.replace('"', '""') + '"' for item in columns)
        rows = sorted(repr(tuple(row)) for row in db.execute(f'SELECT {selection} FROM "{table}"'))
        result[table] = {'count': len(rows), 'sha256': hashlib.sha256('\n'.join(rows).encode()).hexdigest()}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--legacy-project', required=True, type=Path)
    parser.add_argument('--old-src', required=True, type=Path, help='source tree of the merged schema-11 baseline')
    args = parser.parse_args()
    source = args.legacy_project.resolve() / '.research' / 'state.sqlite3'
    wal = source.with_name(source.name + '-wal')
    if not source.is_file() or (wal.exists() and wal.stat().st_size):
        raise RuntimeError('Choose a stable project with no uncheckpointed WAL; do not inspect an active source')
    signature = (source.stat().st_size, source.stat().st_mtime_ns)
    current_src = Path(__file__).resolve().parents[1] / 'src'
    with tempfile.TemporaryDirectory(prefix='ari-legacy-migration-') as temporary:
        root = Path(temporary) / 'project'
        (root / '.research').mkdir(parents=True)
        database = root / '.research' / 'state.sqlite3'
        with sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True) as old, sqlite3.connect(database) as copied:
            version = old.execute('PRAGMA user_version').fetchone()[0]
            old.backup(copied)
        if version not in (9, 10, 11):
            raise RuntimeError(f'Expected a historical schema 9, 10, or 11, found {version}')
        command = [sys.executable, '-c', 'import sys; from auto_research.native_store import NativeStore; NativeStore(sys.argv[1])', str(root)]
        env = dict(os.environ, PYTHONPATH=str(args.old_src.resolve()), PYTHONDONTWRITEBYTECODE='1')
        subprocess.run(command, cwd=root, env=env, check=True, capture_output=True, text=True, timeout=20)
        with sqlite3.connect(database) as db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 11:
                raise RuntimeError('Old baseline did not produce schema 11')
            generation = db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()[0]
            # The original schema-9 project predates durable budgets. Add one
            # synthetic row to verify its preservation in the 11→12 step.
            added_budget = db.execute('SELECT 1 FROM runtime_budgets WHERE generation=?', (generation,)).fetchone() is None
            if added_budget:
                db.execute('INSERT INTO runtime_budgets VALUES(?,?,?,?,?)',
                           (generation, '{"max_session_turns":5}', '2026-01-01T00:00:00+00:00',
                            '2026-01-02T00:00:00+00:00', '2026-01-01T00:00:00+00:00'))
            db.commit()
            before = snapshot(db)
        env['PYTHONPATH'] = str(current_src)
        subprocess.run(command, cwd=root, env=env, check=True, capture_output=True, text=True, timeout=20)
        with sqlite3.connect(database) as db:
            after_version = db.execute('PRAGMA user_version').fetchone()[0]
            after = snapshot(db)
            integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        differences = [name for name, value in before.items() if after.get(name) != value]
        backup = root / '.research' / 'schema-11-backup.sqlite3'
        with sqlite3.connect(backup) as saved:
            backup_version = saved.execute('PRAGMA user_version').fetchone()[0]
            backup_integrity = saved.execute('PRAGMA integrity_check').fetchone()[0]
        if differences or after_version != 12 or integrity != 'ok' or backup_version != 11 or backup_integrity != 'ok':
            raise RuntimeError(f'Migration verification failed: {differences}')
        if signature != (source.stat().st_size, source.stat().st_mtime_ns):
            raise RuntimeError('Source changed during the snapshot')
        print(json.dumps({'source_schema': version, 'baseline_schema': 11, 'result_schema': after_version,
                          'historical_tables_compared': len(before), 'table_contents_preserved': not differences,
                          'events': before.get('events', {}).get('count'), 'requests': before.get('requests', {}).get('count'),
                          'synthetic_budget_added': added_budget, 'backup_schema': backup_version,
                          'source_unchanged_during_copy': True, 'integrity': integrity}, sort_keys=True))


if __name__ == '__main__':
    main()
