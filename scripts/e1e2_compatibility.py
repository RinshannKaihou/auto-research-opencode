#!/usr/bin/env python3
"""Check historical columns and E1/E2 migration on stable copies of project databases."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from auto_research.native_store import NativeStore


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def inventory(path, columns=None):
    with sqlite3.connect(path) as db:
        if columns is None:
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            columns = {name: [row[1] for row in db.execute('PRAGMA table_info("' + name.replace('"', '""') + '")')]
                       for name in names}
        result = {}
        for name, fields in columns.items():
            table = '"' + name.replace('"', '""') + '"'
            projection = ','.join('"' + field.replace('"', '""') + '"' for field in fields)
            rows = sorted(db.execute(f'SELECT {projection} FROM {table}'), key=repr)
            result[name] = {'count':len(rows), 'sha256':hashlib.sha256(repr(rows).encode()).hexdigest()}
        return columns, result


def validate(root, label):
    source = root / '.research' / 'state.sqlite3'
    files = [source, Path(str(source) + '-wal')]
    before = {path.name:digest(path) for path in files}
    with tempfile.TemporaryDirectory(prefix='ari-e1e2-compat-') as directory:
        copy = Path(directory)
        metadata = copy / '.research'
        metadata.mkdir()
        for path in files:
            if path.exists():
                shutil.copyfile(path, metadata / path.name)
        if before != {path.name:digest(path) for path in files}:
            raise RuntimeError(f'{label}: source changed during copy')
        objects = root / '.research' / 'objects'
        if objects.exists():
            (metadata / 'objects').symlink_to(objects, target_is_directory=True)
        database = metadata / 'state.sqlite3'
        columns, old = inventory(database)
        with sqlite3.connect(database) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
        NativeStore(copy, readonly=True).control_state(None, None)
        assert inventory(database, columns)[1] == old, 'read-only view changed historical data'
        store = NativeStore(copy)
        _, migrated = inventory(database, columns)
        differences = [name for name in old if old[name] != migrated[name]]
        assert not differences, f'{label}: historical column values changed: {differences}'
        with store._read() as db:
            names = (
                'exploration_scopes','exploration_routes','exploration_decisions','exploration_results',
                'review_assessments','conclusion_drafts','conclusion_evidence')
            count = {name:db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0] - old.get(name, {}).get('count', 0)
                     for name in names}
            backups = [number for number in range(version, 14) if (metadata / f'schema-{number}-backup.sqlite3').exists()]
            new_version = db.execute('PRAGMA user_version').fetchone()[0]
        assert not any(count.values()), f'{label}: migration created E1/E2 scientific records'
        assert before == {path.name:digest(path) for path in files}, f'{label}: source changed during verification'
        return {'project':label, 'old_schema':version, 'new_schema':new_version,
                'historical_tables':len(old), 'historical_rows':sum(value['count'] for value in old.values()),
                'historical_columns_unchanged':True, 'source_db_wal_unchanged':True,
                'new_scientific_records':count, 'backups':backups}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roots', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    projects = json.loads(args.roots.read_text())['projects']
    result = {'method':'stable DB/WAL copies; compare every historical column; original sources are not opened for writing',
              'projects':[validate(Path(item['root']), f'project-{index:02d}') for index,item in enumerate(projects, 1)]}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(f"validated {len(result['projects'])} projects")


if __name__ == '__main__':
    main()
