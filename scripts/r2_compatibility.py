#!/usr/bin/env python3
"""Validate R2 on stable DB/WAL copies; source projects are read only."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from auto_research.native_store import NativeStore


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def tables(path):
    with sqlite3.connect(path) as db:
        result={}
        for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            quoted='"'+name.replace('"','""')+'"'
            rows=sorted(db.execute(f'SELECT * FROM {quoted}'),key=repr)
            result[name]={'count':len(rows),'sha256':hashlib.sha256(repr(rows).encode()).hexdigest()}
        return result


def validate(root, label):
    source=root/'.research'/'state.sqlite3'
    files=[source,Path(str(source)+'-wal')]
    before={p.name:digest(p) for p in files}
    with tempfile.TemporaryDirectory(prefix='r2-compat-') as directory:
        copy=Path(directory); metadata=copy/'.research'; metadata.mkdir()
        for path in files:
            if path.exists(): shutil.copyfile(path,metadata/path.name)
        if before!={p.name:digest(p) for p in files}:
            raise RuntimeError(f'{root.name}: source changed during copy; no stable sample')
        objects=root/'.research'/'objects'
        if objects.exists(): (metadata/'objects').symlink_to(objects,target_is_directory=True)
        copied=metadata/'state.sqlite3'
        old=tables(copied)
        with sqlite3.connect(copied) as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
        NativeStore(copy,readonly=True).control_state(None,None)
        assert old==tables(copied), 'read-only view changed tables'
        store=NativeStore(copy)
        upgraded=tables(copied)
        differences=[name for name,value in old.items() if upgraded[name]!=value]
        assert not differences, f'historical tables changed: {differences}'
        state=store.control_state(None,None)
        candidates=store.review_page('review_candidates')
        assert candidates['total']==old['review_todos']['count']
        after={p.name:digest(p) for p in files}
        assert before==after, 'source changed during verification; inspect external activity'
        return {'project':label,'old_schema':version,'new_schema':state['schema_version'],
                'historical_tables':len(old),'historical_rows':sum(value['count'] for value in old.values()),
                'historical_tables_unchanged':True,'source_db_wal_unchanged':True,
                'candidates':candidates['total'],'obligations_created_by_migration':store.review_page('review_obligations')['total']}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roots',type=Path,required=True,help='Local JSON containing project roots; roots are not included in the output')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    projects=json.loads(args.roots.read_text())['projects']
    report={'method':'stable DB/WAL copies with read-only object symlinks; sources never opened for writes',
            'projects':[validate(Path(item['root']),f'project-{index:02d}') for index,item in enumerate(projects,1)]}
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(f"validated {len(report['projects'])} projects")
