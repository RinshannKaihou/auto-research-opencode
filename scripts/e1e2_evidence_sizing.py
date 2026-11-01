#!/usr/bin/env python3
"""Measure structural evidence views on isolated copies of existing projects.

Only anonymous counts and sizes are written. This does not prepare a real
project's conclusion or classify its scientific risks.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time

from auto_research import frozen_refs
from auto_research.evidence_read import EvidenceCache
from auto_research.evidence_conclusion import decision_package
from auto_research.native_store import NativeStore


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def select_refs(store):
    with store._read() as db:
        conclusion = store.conclusion_view(db)
        ref = conclusion.get('final_ref') if conclusion else None
        refs, basis = [], 'latest_claims'
        if ref:
            parsed = frozen_refs.parse(ref)
            if parsed.get('item_id'):
                row = db.execute('SELECT knowledge_refs FROM publication_items WHERE publication_id=? AND item_id=?',
                                 (parsed['publication_id'],parsed['item_id'])).fetchone()
                refs = json.loads(row[0]) if row else []
            else:
                refs = [f'knowledge/{row[0]}@{row[1]}' for row in db.execute(
                    'SELECT knowledge_id,revision FROM publication_knowledge WHERE publication_id=?', (parsed.get('publication_id'),))]
            if refs:
                basis = 'final_report_refs'
        if not refs:
            row = db.execute("SELECT knowledge_refs FROM publication_items WHERE kind='report' AND knowledge_refs <> '[]' ORDER BY rowid DESC LIMIT 1").fetchone()
            refs = json.loads(row[0]) if row else []
            if refs:
                basis = 'latest_report_refs'
        if not refs:
            refs = [f'knowledge/{row[0]}@{row[1]}' for row in db.execute(
                "SELECT k.knowledge_id,k.revision FROM knowledge_revisions k JOIN "
                "(SELECT knowledge_id,MAX(revision) revision FROM knowledge_revisions GROUP BY knowledge_id) x "
                "USING(knowledge_id,revision) JOIN knowledge_entries e USING(knowledge_id) WHERE e.kind='claim'")]
        return refs, basis


def measure(source, label):
    dbfile = source / '.research' / 'state.sqlite3'
    files = [dbfile,Path(str(dbfile)+'-wal')]
    before = {path.name:digest(path) for path in files}
    with tempfile.TemporaryDirectory(prefix='ari-e1e2-sizing-') as directory:
        root = Path(directory)
        metadata = root / '.research'
        metadata.mkdir()
        for path in files:
            if path.exists():
                shutil.copyfile(path,metadata/path.name)
        if before != {path.name:digest(path) for path in files}:
            raise RuntimeError('Source changed while copying')
        objects = source / '.research' / 'objects'
        if objects.exists():
            shutil.copytree(objects,metadata/'objects')
        store = NativeStore(root)
        refs, basis = select_refs(store)
        cache = EvidenceCache()
        start = time.perf_counter()
        ident = cache.open(store,refs)
        snapshot = cache.get(store,ident)
        view = snapshot['view']
        sizes = [item['material_identity']['total_bytes'] for item in view['materials'] if item.get('material_identity')]
        placeholder = {'core_claim_refs':refs,'outcome':'unresolved','gaps':[],
                       'evidence_closure':{'status':'unknown','risk_judgments':[]},
                       'review_coverage':{'coverage':[],'status':'unreviewed'},'exploration_stop':{}}
        package = decision_package(view,placeholder)
        package_bytes = len(json.dumps(package,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8'))
        sections = {key:len(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8'))
                    for key,value in package.items()}
        result = {'project':label,'root_selection':basis,'roots':len(refs),
                  'records':len(view['records']),'materials':len(view['materials']),
                  'readable_materials':len(sizes),'largest_material_bytes':max(sizes,default=0),
                  'risks':len(view['risks']),'limitations':len(view['limitations']),
                  'structural_view_utf8_bytes':len(snapshot['body'].encode('utf-8')),
                  'placeholder_decision_package_bytes':package_bytes,
                  'package_section_bytes':sections,
                  'elapsed_ms':round((time.perf_counter()-start)*1000,1)}
    result['source_db_wal_unchanged'] = before == {path.name:digest(path) for path in files}
    if not result['source_db_wal_unchanged']:
        raise RuntimeError('Source changed during measurement')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roots',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    projects = json.loads(args.roots.read_text())['projects']
    result = {'method':'stable database/WAL and object copies; structural view and placeholder package without judgments; not a prepared draft or model token count',
              'projects':[measure(Path(item['root']),f'project-{index:02d}') for index,item in enumerate(projects,1)]}
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(f"measured {len(result['projects'])} projects")


if __name__ == '__main__':
    main()
