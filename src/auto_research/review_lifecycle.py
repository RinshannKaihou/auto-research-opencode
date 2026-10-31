"""Review work is distinct from scientific risk. Reads never reconcile state."""
import hashlib
import json
from datetime import datetime, timezone

from . import epistemic, frozen_refs
from .errors import ValidationError, NotFoundError


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def now():
    return datetime.now(timezone.utc).isoformat()


def generation(db):
    row = db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()
    return row[0] if row else 0


def event(db, oid, action, reason, refs, scope, operation, actor):
    db.execute('''INSERT INTO review_lifecycle_events
        (obligation_id,candidate_id,action,reason,evidence_refs,scope_snapshot,source_sequence,operation_id,actor,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)''', (oid, scope.get('candidate_id'),action, reason, encoded(refs), encoded(scope),
        epistemic.read_bound(db), operation, encoded(actor), now()))


def candidate(db, todo_id, kind, ref, node_id, at):
    pinned = []
    targets = [ref] if kind == 'knowledge-revision' else []
    if kind == 'publication':
        pid = frozen_refs.parse(ref).get('publication_id')
        pinned = [f'knowledge/{r[0]}@{r[1]}' for r in db.execute(
            'SELECT knowledge_id,revision FROM publication_knowledge WHERE publication_id=?', (pid,))]
        targets = [ref,*[f'pub/{pid}#{r[0]}' for r in db.execute('SELECT item_id FROM publication_items WHERE publication_id=? ORDER BY item_id',(pid,))],*pinned]
    db.execute('INSERT OR IGNORE INTO review_candidates VALUES(?,?,?,?,?,?,?,?)',
               (todo_id, todo_id, kind, ref, node_id, encoded(targets),
                'exact' if kind == 'knowledge-revision' or pinned else 'unknown', at))


def closure(store, db, refs, *, validate=False):
    result, queue = set(), list(refs)
    while queue:
        ref = queue.pop()
        if ref in result:
            continue
        if validate:
            store._require_ref(db, ref)
        result.add(ref)
        parsed = frozen_refs.parse(ref)
        if parsed.get('publication_id'):
            pid = parsed['publication_id']
            if parsed.get('item_id'):
                row = db.execute('SELECT knowledge_refs FROM publication_items WHERE publication_id=? AND item_id=?',
                                 (pid, parsed['item_id'])).fetchone()
                if row:
                    queue.extend(json.loads(row[0]))
            else:
                queue.extend(f'knowledge/{r[0]}@{r[1]}' for r in db.execute(
                    'SELECT knowledge_id,revision FROM publication_knowledge WHERE publication_id=?', (pid,)))
        queue.extend(r[0] for r in db.execute('SELECT used_ref FROM knowledge_support_edges WHERE user_ref=?', (ref,)))
    return result


def set_scope(store, db, refs, reason, operation, actor):
    if not isinstance(refs, list) or not all(isinstance(r, str) and
            (r.startswith('knowledge/') or frozen_refs.is_frozen(r)) for r in refs):
        raise ValidationError('decision_refs must be fixed knowledge/publication/item references')
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError('scope_reason is required when changing decision_refs')
    closure(store, db, refs, validate=True)
    project = store._project(db)
    config = project['config']
    before = config.get('r2_review_scope')
    scope = {'generation': generation(db), 'refs': sorted(set(refs))}
    config['r2_review_scope'] = scope
    db.execute('UPDATE project SET config=?', (encoded(config),))
    event(db, None, 'scope_set', reason, refs, {'before': before, 'after': scope}, operation, actor)
    old_refs = before['refs'] if before and before['generation']==scope['generation'] else []
    added = sorted(set(refs)-set(old_refs))
    if added:
        store._event(db,'review.scope_changed',{'added':added,'actor':actor})
    return scope


def promote(db, owner, target, change, reason, operation, actor, scope):
    kind = 'knowledge' if target.startswith('knowledge/') else 'material'
    key = [generation(db), owner, kind, target, change]
    oid = 'RO-' + hashlib.sha256(encoded(key).encode()).hexdigest()[:24]
    row = db.execute('SELECT * FROM review_obligations WHERE obligation_id=?', (oid,)).fetchone()
    if row and row['state'] != 'out_of_scope':
        return oid
    state = 'pending'
    if row:
        reopen = db.execute("SELECT COALESCE(MAX(event_id),0) FROM review_lifecycle_events WHERE obligation_id=? AND action='reopen'", (oid,)).fetchone()[0]
        completed = db.execute("SELECT 1 FROM review_lifecycle_events WHERE obligation_id=? AND action IN ('completed','accept_previous') AND event_id>?", (oid, reopen)).fetchone()
        state = 'completed' if completed else 'pending'
        db.execute('UPDATE review_obligations SET state=?,updated_at=? WHERE obligation_id=?', (state, now(), oid))
    else:
        db.execute('INSERT INTO review_obligations VALUES(?,?,?,?,?,?,?,?,?)', (*[oid, *key], state, now(), now()))
    event(db, oid, 'reentered' if row else 'promoted', reason, [], scope, operation, actor)
    return oid


def reconcile(store, db, operation):
    gen = generation(db)
    scopes = {}
    for row in db.execute('''SELECT DISTINCT a.node_id,a.attempt_id FROM attempts a
        JOIN associations s USING(association_id) JOIN workflow_sessions w ON w.session_id=s.session_id AND w.host_id=s.host_id
        WHERE a.ended_at IS NULL AND a.node_id IS NOT NULL AND w.detached=0'''):
        nid = row['node_id']
        node = db.execute('SELECT inputs FROM nodes WHERE node_id=?', (nid,)).fetchone()
        refs = json.loads(node[0])
        for dep in db.execute('SELECT input_refs FROM node_dependencies WHERE successor_node_id=?', (nid,)):
            refs.extend(json.loads(dep[0]))
        refs.extend(r[0] for r in db.execute('SELECT source_ref FROM material_consumptions WHERE attempt_id=?', (row['attempt_id'],)))
        refs.extend(f'knowledge/{r[0]}@{r[1]}' for r in db.execute('''SELECT e.knowledge_id,MAX(r.revision)
            FROM knowledge_entries e JOIN knowledge_revisions r USING(knowledge_id)
            WHERE e.node_id=? AND e.kind!='open_question' GROUP BY e.knowledge_id''', (nid,)))
        for manual in db.execute('''SELECT o.target_ref,e.scope_snapshot FROM review_lifecycle_events e
            JOIN review_obligations o USING(obligation_id) WHERE o.generation=? AND o.scope_owner=?
            AND e.action='promotion_requested' ''',(gen,'node:'+nid)):
            if json.loads(manual['scope_snapshot']).get('attempt_id')==row['attempt_id']:
                refs.append(manual['target_ref'])
        scopes.setdefault('node:' + nid, set()).update(closure(store, db, refs))
    explicit = store._project(db)['config'].get('r2_review_scope')
    known_project = explicit is not None and explicit['generation'] == gen
    if known_project:
        scopes['project'] = closure(store, db, explicit['refs'])
    run = db.execute('SELECT state FROM workflow_project WHERE id=1').fetchone()
    if run and run[0] in {'complete', 'stopped'}:
        scopes = {}
        known_project = True
    actor = {'kind': 'reconciliation'}
    valid_pairs = {(item['change_id'],item['affected_version']) for item in epistemic.valid_impacts(db,epistemic.read_bound(db))}
    for owner, refs in scopes.items():
        for ref in sorted(refs):
            if ref.startswith('knowledge/') or frozen_refs.is_frozen(ref):
                promote(db, owner, ref, None, 'current declared support', operation, actor, {'refs': sorted(refs)})
        for change,target in sorted(valid_pairs):
            if target in refs:
                promote(db, owner, target, change, 'current affected support',
                        operation, actor, {'refs': sorted(refs)})
    for row in list(db.execute('SELECT * FROM review_obligations')):
        owner = row['scope_owner']
        if row['generation'] == gen and owner == 'project' and not known_project:
            continue
        inside = row['generation'] == gen and row['target_ref'] in scopes.get(owner, set()) and (
            row['change_id'] is None or (row['change_id'],row['target_ref']) in valid_pairs)
        if not inside and row['state'] != 'out_of_scope':
            db.execute("UPDATE review_obligations SET state='out_of_scope',updated_at=? WHERE obligation_id=?", (now(), row['obligation_id']))
            event(db, row['obligation_id'], 'out_of_scope', 'left declared scope or work segment', [],
                  {'previous_state': row['state']}, operation, actor)


def read_row(db, row):
    value = dict(row)
    value['coverage'] = 'not_assessed'
    value['events'] = []
    for raw in db.execute('SELECT * FROM review_lifecycle_events WHERE obligation_id=? ORDER BY event_id', (row['obligation_id'],)):
        item = dict(raw)
        for key in ('evidence_refs', 'scope_snapshot', 'actor'):
            item[key] = json.loads(item[key])
        value['events'].append(item)
    if row['change_id']:
        disposition = epistemic.current_disposition(db, row['change_id'], row['target_ref'])
        value['risk_disposition'] = disposition['kind'] if disposition else 'unresolved'
    elif row['target_ref'].startswith('knowledge/'):
        ref = row['target_ref']
        value['risk'] = {'needs_action': epistemic.needs_action(db, ref),
                         'residual_use_risk': epistemic.residual_use_risk(db, ref),
                         'version_notices': epistemic.version_notices(db, ref)}
    return value


def unresolved_challenges(db):
    return list(db.execute('''SELECT o.target_ref,o.obligation_id,o.generation,e.event_id,e.evidence_refs
        FROM review_obligations o JOIN review_lifecycle_events e USING(obligation_id)
        WHERE e.action IN ('completed','report_partial')
        AND json_extract(e.scope_snapshot,'$.result')='challenges'
        AND COALESCE((SELECT json_extract(d.scope_snapshot,'$.kind')
            FROM review_lifecycle_events d WHERE d.action='challenge_disposed'
            AND json_extract(d.scope_snapshot,'$.review_event')=e.event_id
            ORDER BY d.event_id DESC LIMIT 1),'unresolved')='unresolved'
        ORDER BY (o.generation=(SELECT generation FROM workflow_project WHERE id=1)) DESC,e.event_id DESC'''))


def summary(db, node_id=None):
    owner = 'node:' + node_id if node_id else 'project'
    if db.execute('PRAGMA user_version').fetchone()[0]<11:
        return {'available':False,'semantics':'legacy_candidates_only','pending_total':0,
                'expand':{'collection':'review_todos'}}
    count = db.execute("SELECT COUNT(*) FROM review_obligations WHERE generation=? AND scope_owner=? AND state IN ('pending','running')",(generation(db),owner)).fetchone()[0]
    candidates = db.execute('SELECT COUNT(*) FROM review_candidates WHERE (? IS NULL OR node_id=?)',(node_id,node_id)).fetchone()[0]
    config=json.loads(db.execute('SELECT config FROM project LIMIT 1').fetchone()[0])
    declared=config.get('r2_review_scope')
    return {'available':True,'pending_total':count,'candidate_total':candidates,
            'scope_status':'derived' if node_id else 'declared' if declared and declared['generation']==generation(db) else 'unknown',
            'semantics':'task_completion_is_not_risk_resolution',
            'expand':{'collection':'review_obligations','scope_owner':owner}}


class ReviewStore:
    def dispose_review_challenge(self, fields, request_id, actor):
        from .epistemic import DISPOSITION_KINDS
        def work(db):
            if type(fields['review_event']) is not int:
                raise ValidationError('review_event must be an integer')
            review = db.execute('''SELECT o.*,e.scope_snapshot FROM review_lifecycle_events e
                JOIN review_obligations o USING(obligation_id) WHERE e.event_id=?
                AND e.action IN ('completed','report_partial')''',(fields['review_event'],)).fetchone()
            if not review or json.loads(review['scope_snapshot']).get('result')!='challenges':
                raise ValidationError('review_event must identify a challenge report')
            if fields['affected_version']!=review['target_ref'] or fields.get('change_id'):
                raise ValidationError('Challenge disposition must match the exact review target')
            role=db.execute('SELECT role,node_id FROM workflow_sessions WHERE host_id=? AND session_id=? AND detached=0',
                            (actor['host_id'],actor['session_id'])).fetchone()
            if not role or role['role'] not in {'main','node_core'} or (
                    role['role']=='node_core' and (not role['node_id'] or review['scope_owner']!='node:'+role['node_id'])):
                raise ValidationError('Challenge belongs to another work owner')
            kind=fields['disposition_kind']
            if kind not in DISPOSITION_KINDS or not isinstance(fields.get('reason'),str) or not fields['reason'].strip():
                raise ValidationError('Explicit challenge disposition kind and reason required')
            refs=fields.get('evidence_refs',[])
            if not isinstance(refs,list) or not all(isinstance(ref,str) for ref in refs):
                raise ValidationError('evidence_refs must be references')
            if kind!='unresolved' and not refs:
                raise ValidationError('Handling a challenge requires evidence_refs')
            for ref in refs:
                self._require_ref(db,ref)
            replacement=fields.get('replacement_ref')
            if kind=='revised':
                if not replacement or replacement==review['target_ref']:
                    raise ValidationError('Revised challenge requires a different replacement_ref')
                self._require_ref(db,replacement)
            scope=dict(review_event=fields['review_event'],kind=kind,replacement_ref=replacement)
            event(db,review['obligation_id'],'challenge_disposed',fields['reason'],refs,scope,request_id,actor)
            db.execute('UPDATE review_obligations SET updated_at=? WHERE obligation_id=?',(now(),review['obligation_id']))
            self._event(db,'review.challenge_disposed',{**scope,'target_ref':review['target_ref'],'actor':actor,'evidence_refs':refs})
            return {**scope,'target_ref':review['target_ref'],'risk_status':'unresolved' if kind=='unresolved' else 'handled'}
        return self._mutate('review.challenge_dispose',fields,request_id,work)

    def review_scope_set(self, host_id, session_id, refs, reason, request_id):
        payload = dict(host_id=host_id, session_id=session_id, refs=refs, reason=reason)
        return self._mutate('review.scope', payload, request_id, lambda db:
            set_scope(self, db, refs, reason, request_id, {'host_id': host_id, 'session_id': session_id}))

    def review_promote(self, fields, request_id, actor):
        def work(db):
            target, owner = fields['target_ref'], fields['scope_owner']
            closure(self, db, [target], validate=True)
            refs = fields.get('evidence_refs',[])
            for ref in refs:
                self._require_ref(db,ref)
            cid = fields.get('candidate_id')
            if cid:
                candidate = db.execute('SELECT * FROM review_candidates WHERE candidate_id=?',(cid,)).fetchone()
                if not candidate:
                    raise NotFoundError('Unknown review candidate')
                if candidate['mapping_status']=='exact' and target not in json.loads(candidate['target_refs']):
                    raise ValidationError('Target does not match this candidate')
            change = fields.get('change_id')
            if change and not any(item['change_id']==change and item['affected_version']==target for item in epistemic.valid_impacts(db,epistemic.read_bound(db))):
                raise ValidationError('change_id does not affect target_ref')
            if owner == 'project':
                current = self._project(db)['config'].get('r2_review_scope', {})
                current_refs = current.get('refs', []) if current.get('generation') == generation(db) else []
                scope = set_scope(self, db, [*current_refs, target], fields['reason'], request_id, actor)
            else:
                if not owner.startswith('node:'):
                    raise ValidationError('scope_owner must be project or node:<id>')
                attempt = db.execute('SELECT attempt_id FROM attempts WHERE node_id=? AND ended_at IS NULL',(owner[5:],)).fetchone()
                if not attempt:
                    raise ValidationError('Manual node review promotion requires an active work segment')
                scope = {'attempt_id':attempt[0]}
            scope['candidate_id'] = cid
            oid = promote(db, owner, target, change, fields['reason'], request_id, actor, scope)
            event(db,oid,'promotion_requested',fields['reason'],refs,scope,request_id,actor)
            reconcile(self,db,request_id)
            return read_row(db, db.execute('SELECT * FROM review_obligations WHERE obligation_id=?', (oid,)).fetchone())
        return self._mutate('review.promote', fields, request_id, work)

    def review_link(self, fields, request_id, actor):
        def work(db):
            row = db.execute('SELECT * FROM review_obligations WHERE obligation_id=?', (fields['obligation_id'],)).fetchone()
            if not row:
                raise NotFoundError('Unknown review obligation')
            if row['generation'] != generation(db) or fields['target_ref'] != row['target_ref'] or (
                    'change_id' in fields and fields['change_id'] != row['change_id']):
                raise ValidationError('Review must match the exact current obligation key')
            role = db.execute('SELECT role,node_id FROM workflow_sessions WHERE host_id=? AND session_id=? AND detached=0',
                              (actor['host_id'], actor['session_id'])).fetchone()
            if not role or role['role'] not in {'main','node_core'} or (role['role']=='node_core' and (not role['node_id'] or row['scope_owner'] != 'node:' + role['node_id'])):
                raise ValidationError('Review belongs to another work owner')
            report = fields['source_ref']
            if report == row['target_ref'] or not frozen_refs.is_frozen(report):
                raise ValidationError('Review requires a separate frozen report')
            if frozen_refs.is_frozen(row['target_ref']):
                target = frozen_refs.parse(row['target_ref'])
                source = frozen_refs.parse(report)
                same_publication = target.get('publication_id') and target['publication_id']==source.get('publication_id')
                same_snapshot = target.get('snapshot_id') and target['snapshot_id']==source.get('snapshot_id')
                inside = bool(same_publication and (not target.get('item_id') or (
                    target['item_id']==source.get('item_id') and (not target.get('subpath') or
                    source.get('subpath','')==target['subpath'] or source.get('subpath','').startswith(target['subpath']+'/')))))
                inside |= bool(same_snapshot and (not target.get('path') or source.get('path','')==target['path'] or
                                                 source.get('path','').startswith(target['path']+'/')))
                if inside:
                    raise ValidationError('Review requires a separate frozen report outside the reviewed material')
            resolved = frozen_refs.resolve(db, self.root, report)
            if resolved['outcome'] != 'resolved':
                raise ValidationError('Review report must resolve')
            if resolved['object']:
                opened, path = frozen_refs.open(db, self.root, report)
                if opened['outcome'] != 'resolved' or not path or not path.is_file() or not path.read_bytes().strip():
                    raise ValidationError('Review report is empty')
            else:
                parsed = frozen_refs.parse(report)
                if not parsed.get('item_id'):
                    raise ValidationError('Review report must identify an item or frozen file')
                item = db.execute('SELECT content FROM publication_items WHERE publication_id=? AND item_id=?',
                                  (parsed['publication_id'], parsed['item_id'])).fetchone()
                if not json.loads(item[0]):
                    raise ValidationError('Review report is empty')
            result = fields.get('result')
            complete = fields.get('complete')
            if result not in {'supports','challenges','inconclusive'} or type(complete) is not bool:
                raise ValidationError('Review result and complete must be explicit')
            parsed = frozen_refs.parse(report)
            table,column,key = ('publications','publication_id',parsed['publication_id']) if parsed.get('publication_id') else ('snapshots','snapshot_id',parsed.get('snapshot_id'))
            created = db.execute(f'SELECT created_at FROM {table} WHERE {column}=?',(key,)).fetchone()[0]
            reopened = db.execute("SELECT created_at FROM review_lifecycle_events WHERE obligation_id=? AND action='reopen' ORDER BY event_id DESC LIMIT 1",(row['obligation_id'],)).fetchone()
            if complete and reopened and created <= reopened[0]:
                raise ValidationError('Review predates reopen; use a new report or explicit accept_previous')
            if not isinstance(fields.get('reason'), str) or not fields['reason'].strip():
                raise ValidationError('Review reason is required')
            refs = [report, *fields.get('evidence_refs', [])]
            for ref in refs:
                self._require_ref(db, ref)
            if complete and row['state']=='completed':
                prior = db.execute("SELECT evidence_refs,scope_snapshot FROM review_lifecycle_events WHERE obligation_id=? AND action='completed' ORDER BY event_id DESC LIMIT 1",(row['obligation_id'],)).fetchone()
                if prior and json.loads(prior[0])==refs and json.loads(prior[1])['result']==result:
                    return {**read_row(db,row),'deduplicated':True}
            event(db, row['obligation_id'], 'completed' if complete else 'report_partial', fields['reason'], refs,
                  {'result': result, 'complete': complete}, request_id, actor)
            db.execute('UPDATE review_obligations SET updated_at=? WHERE obligation_id=?',(now(),row['obligation_id']))
            if complete:
                db.execute("UPDATE review_obligations SET state='completed',updated_at=? WHERE obligation_id=?", (now(), row['obligation_id']))
                self._event(db,'review.completed',{'obligation_id':row['obligation_id'],
                    'node_id':row['scope_owner'][5:] if row['scope_owner'].startswith('node:') else None,'actor':actor})
            reconcile(self,db,request_id)
            return read_row(db, db.execute('SELECT * FROM review_obligations WHERE obligation_id=?', (row['obligation_id'],)).fetchone())
        return self._mutate('review.link', fields, request_id, work)

    def review_exception(self, fields, request_id, actor):
        def work(db):
            row = db.execute('SELECT * FROM review_obligations WHERE obligation_id=?', (fields['obligation_id'],)).fetchone()
            if not row:
                raise NotFoundError('Unknown review obligation')
            if row['generation'] != generation(db):
                raise ValidationError('Review exceptions apply to the current generation')
            action = fields['action']
            if action not in {'waive','reopen','accept_previous','start'} or not fields.get('reason'):
                raise ValidationError('Explicit review exception action and reason required')
            if action == 'accept_previous':
                report = db.execute("SELECT 1 FROM review_lifecycle_events WHERE event_id=? AND obligation_id=? AND action='completed'",
                                    (fields.get('report_link'), row['obligation_id'])).fetchone()
                if not report:
                    raise ValidationError('accept_previous requires a previous complete report link')
            refs = fields.get('evidence_refs', [])
            for ref in refs:
                self._require_ref(db, ref)
            event(db, row['obligation_id'], action, fields['reason'], refs, {'report_link': fields.get('report_link')}, request_id, actor)
            state = {'waive':'waived','reopen':'pending','accept_previous':'completed','start':'running'}[action]
            db.execute('UPDATE review_obligations SET state=?,updated_at=? WHERE obligation_id=?', (state, now(), row['obligation_id']))
            reconcile(self,db,request_id)
            return read_row(db, db.execute('SELECT * FROM review_obligations WHERE obligation_id=?', (row['obligation_id'],)).fetchone())
        return self._mutate('review.exception', fields, request_id, work)

    def review_page(self, collection, *, cursor=None, limit=50, **filters):
        tables = {'review_candidates':'review_candidates', 'review_obligations':'review_obligations', 'review_events':'review_lifecycle_events'}
        if collection not in tables:
            raise ValidationError('Unknown review collection')
        with self._read() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] < 11:
                return {'available': False, 'reason':'requires_migration', 'items':[], 'cursor':None, 'total':None}
            project = self._project(db)['project_id']
            if cursor:
                if cursor['project_id'] != project or cursor['collection'] != collection:
                    raise ValidationError('read_view_changed: review cursor belongs to another project/collection')
                if any(key in filters and filters[key] != value for key,value in cursor['filters'].items()):
                    raise ValidationError('read_view_changed: review filters changed')
                filters = cursor['filters']
            allowed = {'node_id','trigger_kind'} if collection=='review_candidates' else {'generation','scope_owner','state','target_ref','change_id'} if collection=='review_obligations' else {'obligation_id'}
            if set(filters) - allowed:
                raise ValidationError('Unsupported review filters')
            clause = ' AND '.join(f'{key}=?' for key in filters) or '1'
            rows = [dict(r) for r in db.execute(f'SELECT rowid AS _row,* FROM {tables[collection]} WHERE {clause} ORDER BY rowid', tuple(filters.values()))]
            fingerprint = hashlib.sha256(encoded(rows).encode()).hexdigest()
            if cursor and cursor['fingerprint'] != fingerprint:
                raise ValidationError('read_view_changed: review collection changed; refresh')
            offset = cursor['offset'] if cursor else 0
            if type(offset) is not int or offset < 0:
                raise ValidationError('Review cursor offset must be a nonnegative integer')
            limit = max(1,min(int(limit),200))
            selected = rows[offset:offset+limit]
            items = []
            for row in selected:
                if collection=='review_obligations':
                    value = read_row(db, row)
                else:
                    value = dict(row)
                    for key in ('target_refs','evidence_refs','scope_snapshot','actor'):
                        if key in value:
                            value[key] = json.loads(value[key])
                items.append(value)
            return {'available':True, 'items':items, 'total':len(rows), 'cursor':
                {'project_id':project,'collection':collection,'filters':filters,'fingerprint':fingerprint,'offset':offset+limit}
                if offset+limit<len(rows) else None}
