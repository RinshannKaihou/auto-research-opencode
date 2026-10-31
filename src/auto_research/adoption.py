"""Typed relation routing and adoption projection over existing immutable facts."""
import hashlib
import json
from datetime import datetime, timezone

from . import frozen_refs
from .errors import ValidationError, ConflictError


class AdoptionStore:
    def relate_typed(self, fields, operation, actor):
        label, source, target = fields['label'], fields['source_ref'], fields['target_ref']
        if not isinstance(fields.get('note'),str) or not fields['note'].strip():
            raise ValidationError('Typed relation requires a rationale')
        if label == 'reviews':
            if not {'obligation_id','result','complete'}.issubset(fields):
                raise ValidationError('reviews requires obligation_id, result and complete')
            review = {key:value for key,value in fields.items() if key not in {'label','contract_version','note','input_refs','revision'}}
            review['reason'] = fields['note']
            return self.review_link(review,operation,actor)
        if source.startswith('knowledge/') and label in {'grounded_in','motivated_by','answers','supersedes','complements','challenges'}:
            revision = fields.get('revision')
            allowed = {'change_kind','affected_scope_mode','affected_scope','execution_refs','preserve_sources','no_new_evidence'}
            if not isinstance(revision,dict) or set(revision)-allowed:
                raise ValidationError('Typed knowledge relation requires explicit revision fields')
            value = dict(ref=source,changes={},reason=fields['note'],preset='custom',**revision,
                         _contract_version='r2-v1')
            if label=='motivated_by':
                value['_append_motivation'] = target
            else:
                value['relations'] = [{'type':label,'target':target}]
            return self.revise_knowledge(value,operation,asserted_at=actor)
        if label not in {'depends_on','branches_from','revises'}:
            return self.relate(source,target,label,fields['note'],operation,asserted_at=actor,contract_version='r2-v1')
        def work(db):
            self._require_ref(db,source); self._require_ref(db,target)
            if not db.execute('SELECT 1 FROM nodes WHERE node_id=?',(source,)).fetchone():
                raise ValidationError('Typed predecessor source must be a successor node')
            predecessor = self._ref_node_id(db,target)
            if not predecessor or predecessor==source:
                raise ValidationError('Predecessor owner must be unambiguous and distinct')
            if db.execute('SELECT 1 FROM exploration_tasks WHERE node_id=?',(source,)).fetchone() or db.execute('SELECT 1 FROM attempts WHERE node_id=?',(source,)).fetchone():
                raise ConflictError('Cannot change predecessors after node dispatch/work starts')
            if db.execute("SELECT 1 FROM nodes WHERE node_id=? AND status='closed'",(source,)).fetchone():
                raise ConflictError('Cannot change a closed node')
            refs = fields.get('input_refs', [])
            if not isinstance(refs,list) or not all(isinstance(r,str) for r in refs):
                raise ValidationError('input_refs must be fixed material references')
            refs = sorted(set([*refs, *([target] if frozen_refs.parse(target).get('publication_id') else [])]))
            if label=='depends_on' and not refs:
                raise ValidationError('depends_on requires material inputs')
            for ref in refs:
                self._require_ref(db,ref)
                if self._ref_node_id(db,ref) != predecessor:
                    raise ValidationError('Input material is not owned by predecessor')
            visited,queue = set(),[source]
            while queue:
                item=queue.pop()
                if item==predecessor:
                    raise ConflictError('Typed predecessor would create a cycle')
                if item not in visited:
                    visited.add(item)
                    queue.extend(r[0] for r in db.execute('SELECT successor_node_id FROM node_dependencies WHERE predecessor_node_id=?',(item,)))
            existing = db.execute('SELECT * FROM node_dependencies WHERE predecessor_node_id=? AND successor_node_id=? AND relation_type=?',(predecessor,source,label)).fetchone()
            if existing:
                if json.loads(existing['input_refs'])!=refs or existing['rationale']!=fields['note']:
                    raise ConflictError('Existing predecessor declaration differs')
                return {**dict(existing),'input_refs':refs,'deduplicated':True}
            value = dict(dependency_id=self._next(db,'dependency','D'),predecessor_node_id=predecessor,
                successor_node_id=source,relation_type=label,rationale=fields['note'],input_refs=refs,
                scheduling=label=='depends_on',created_at=datetime.now(timezone.utc).isoformat())
            db.execute('INSERT INTO node_dependencies VALUES(?,?,?,?,?,?,?,?)',
                (*[value[k] for k in ('dependency_id','predecessor_node_id','successor_node_id','relation_type','rationale')],json.dumps(refs),int(value['scheduling']),value['created_at']))
            self._event(db,'dependency.created',{**value,'asserted_at':actor})
            return value
        return self._mutate('relate.typed',fields,operation,work)

    def adoptions(self, *, node_id=None, cursor=None, limit=50):
        with self._read() as db:
            project = self._project(db)['project_id']
            if cursor:
                if cursor['project_id']!=project or (node_id is not None and node_id!=cursor['node_id']):
                    raise ValidationError('read_view_changed: adoption cursor scope differs')
                node_id = cursor['node_id']
            values = {}
            def add(node,attempt,ref,relation,purpose,origin):
                if node_id is not None and node!=node_id: return
                key=(node,attempt,ref,relation,purpose)
                values.setdefault(key,dict(node_id=node,attempt_id=attempt,source_ref=ref,relation_type=relation,
                                          purpose=purpose,origins=[]))['origins'].append(origin)
            for row in db.execute('SELECT * FROM nodes ORDER BY rowid'):
                for ref in json.loads(row['inputs']):
                    add(row['node_id'],None,ref,'adopts','proposal_input',{'kind':'proposal','ref':row['node_id']})
            for row in db.execute('SELECT * FROM material_consumptions ORDER BY rowid'):
                add(row['node_id'],row['attempt_id'],row['source_ref'],row['relation_type'],row['use_text'],{'kind':'explicit','ref':row['consumption_id']})
            for row in db.execute('SELECT * FROM node_dependencies ORDER BY rowid'):
                for ref in json.loads(row['input_refs']):
                    add(row['successor_node_id'],None,ref,'adopts','dependency_input',{'kind':'dependency','ref':row['dependency_id']})
            for row in db.execute('SELECT e.node_id,r.* FROM knowledge_revisions r JOIN knowledge_entries e USING(knowledge_id) ORDER BY r.rowid'):
                asserted = json.loads(row['asserted_at'] or '{}') or {}
                attempts = list(db.execute('''SELECT t.attempt_id FROM attempts t JOIN associations a USING(association_id)
                    WHERE a.host_id=? AND a.session_id=? AND t.node_id IS ? AND t.started_at<=?
                    AND (t.ended_at IS NULL OR t.ended_at>=?)''',
                    (asserted.get('host_id'),asserted.get('session_id'),row['node_id'],row['created_at'],row['created_at'])))
                attempt=attempts[0][0] if len(attempts)==1 else None
                for name,purpose in (('evidence_refs','evidence'),('dependencies','grounded_in')):
                    for ref in json.loads(row[name]):
                        add(row['node_id'],attempt,ref,'supports',purpose,{'kind':'derived','ref':f"knowledge/{row['knowledge_id']}@{row['revision']}"})
            items=sorted(values.values(),key=lambda value:json.dumps(value,sort_keys=True))
            fingerprint=hashlib.sha256(json.dumps(items,sort_keys=True).encode()).hexdigest()
            if cursor and cursor['fingerprint']!=fingerprint:
                raise ValidationError('read_view_changed: adoption projection changed')
            offset=cursor['offset'] if cursor else 0
            if type(offset) is not int or offset<0:
                raise ValidationError('Adoption cursor offset must be a nonnegative integer')
            limit=max(1,min(int(limit),200))
            return {'items':items[offset:offset+limit],'total':len(items),'cursor':
                dict(project_id=project,node_id=node_id,offset=offset+limit,fingerprint=fingerprint)
                if offset+limit<len(items) else None}
