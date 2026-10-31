"""Durable limits for mediated autonomous dispatch; external work stays unmetered."""
import json
import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .errors import ValidationError, BudgetError, ConflictError

LIMITS = {'max_session_turns','max_total_nodes','max_wall_seconds','max_tokens','max_cost'}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def owner(db, host, session):
    row = db.execute('SELECT * FROM workflow_sessions WHERE host_id=? AND session_id=? AND detached=0', (host,session)).fetchone()
    if not row:
        raise ValidationError('Runtime session is not associated')
    attempt = db.execute('''SELECT t.node_id FROM attempts t JOIN associations a USING(association_id)
        WHERE a.host_id=? AND a.session_id=? AND a.ended_at IS NULL AND t.ended_at IS NULL''', (host,session)).fetchone()
    node = attempt[0] if attempt and attempt[0] else row['node_id']
    return 'node:' + node if node else 'project'


def start(store, db, gen, at):
    limits = store._project(db)['config'].get('runtime_budget', {})
    seconds = limits.get('max_wall_seconds')
    deadline = (datetime.fromisoformat(at) + timedelta(seconds=seconds)).isoformat() if seconds else None
    db.execute('INSERT OR IGNORE INTO runtime_budgets VALUES(?,?,?,?,?)', (gen,json.dumps(limits),at,deadline,at))


def view(store, db, host, session):
    row = db.execute('SELECT generation,state FROM workflow_project WHERE id=1').fetchone()
    gen = row['generation'] if row else 0
    stored = db.execute('SELECT * FROM runtime_budgets WHERE generation=?', (gen,)).fetchone()
    limits = json.loads(stored['limits']) if stored else store._project(db)['config'].get('runtime_budget', {})
    scope_owner = owner(db,host,session)
    turns = db.execute("SELECT COUNT(*) FROM runtime_budget_reservations WHERE generation=? AND kind='turn' AND owner=?", (gen,scope_owner)).fetchone()[0]
    unconfirmed = db.execute("SELECT COUNT(*) FROM runtime_budget_reservations WHERE generation=? AND kind='turn' AND owner=? AND state='reserved'", (gen,scope_owner)).fetchone()[0]
    nodes = db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0]
    usage = list(db.execute("SELECT amount,details FROM usage_observations WHERE json_extract(details,'$.generation')=?", (gen,)))
    tokens = [r['amount'] for r in usage if r['amount'] is not None]
    costs = [json.loads(r['details'])['cost'] for r in usage if json.loads(r['details']).get('cost') is not None]
    expired = bool(stored and stored['deadline_at'] and timestamp() >= stored['deadline_at'])
    reason = ('wall_clock' if expired else
              'session_turns' if limits.get('max_session_turns') is not None and turns >= limits['max_session_turns'] else
              'tokens_observed' if tokens and limits.get('max_tokens') is not None and sum(tokens)>=limits['max_tokens'] else
              'cost_observed' if costs and limits.get('max_cost') is not None and sum(costs)>=limits['max_cost'] else None)
    return {'generation':gen,'scope_owner':scope_owner,'limits':limits,'turns':turns,'total_nodes':nodes,
            'turns_accounting':'reserved autonomous requests; uncertain dispatch is not refunded',
            'unconfirmed_reservations':unconfirmed,
            'deadline_at':stored['deadline_at'] if stored else None, 'expired':expired,'reason':reason,
            'tokens_observed':sum(tokens) if tokens else None,'cost_observed':sum(costs) if costs else None,
            'usage_coverage':'observed_subset','scope':'plugin autonomous dispatch; external processes unmetered',
            'run_state':row['state'] if row else None}


def check_node(store, db):
    limit = store._project(db)['config'].get('runtime_budget', {}).get('max_total_nodes')
    if limit is not None and db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0] >= limit:
        raise BudgetError('Project total-node limit reached; explicit budget change required')


def publication_materials(db, publication):
    return [tuple(row) for row in db.execute('''SELECT kind,content,object_version,object_kind,source_path,knowledge_refs
        FROM publication_items WHERE publication_id=? ORDER BY kind,content,object_version,object_kind,source_path,knowledge_refs''',(publication,))]


class RuntimeStore:
    def runtime_budget_summary(self, db):
        if db.execute('PRAGMA user_version').fetchone()[0]<11:
            return {'available':False,'reason':'requires_migration'}
        main = db.execute("SELECT host_id,session_id FROM workflow_sessions WHERE role='main' AND detached=0").fetchone()
        if not main:
            return {'available':False,'reason':'no_runtime_owner'}
        return {'available':True,**view(self,db,main['host_id'],main['session_id'])}

    def invocation_context(self, host, session):
        with self._read() as db:
            return self._invocation_context(db,host,session)

    def _invocation_context(self, db, host, session):
        association = self._association(db,host,session)
        attempt = db.execute('SELECT attempt_id,node_id FROM attempts WHERE association_id=? AND ended_at IS NULL',(association['association_id'],)).fetchone()
        if not attempt:
            attempt = db.execute('SELECT attempt_id,node_id FROM specialist_bindings WHERE session_id=?',(session,)).fetchone()
        row = db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()
        return dict(association_id=association['association_id'],attempt_id=attempt['attempt_id'] if attempt else None,
                    node_id=attempt['node_id'] if attempt else None,generation=row[0] if row else 0,
                    source_sequence=db.execute('SELECT COALESCE(MAX(event_id),0) FROM events').fetchone()[0])

    def runtime_native_event(self, host, session, fields, operation):
        def work(db):
            phase = fields['phase']
            key = fields['source_key']
            if phase not in {'started','completed','failed'}:
                raise ValidationError('Unknown native usage phase')
            row = db.execute('SELECT * FROM usage_observations WHERE source_key=?',(key,)).fetchone()
            if not row and fields.get('request_key'):
                row = db.execute('SELECT * FROM usage_observations WHERE source_key=?',(fields['request_key'],)).fetchone()
                if row:
                    db.execute('UPDATE usage_observations SET source_key=? WHERE observation_id=?',(key,row['observation_id']))
            if row and 'native_phase' not in json.loads(row['details']):
                raise ConflictError('Native source key belongs to an existing non-native observation')
            if not row:
                context = self._invocation_context(db,host,session) if phase=='started' else dict(
                    association_id=None,attempt_id=None,node_id=None,generation=None)
                model = fields.get('model') or {}
                details = {'generation':context['generation'],'variant':model.get('variant'),'native_phase':'started',
                           'binding':'frozen' if phase=='started' else 'unknown','source_kind':fields.get('kind','primary')}
                db.execute('INSERT INTO usage_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (str(uuid4()),key,context['association_id'],context['attempt_id'],context['node_id'],
                            'conversation' if fields.get('kind','primary')=='primary' else fields['kind'],
                            model.get('providerID'),model.get('id'),None,'unknown',json.dumps(details),timestamp()))
                row = db.execute('SELECT * FROM usage_observations WHERE source_key=?',(key,)).fetchone()
            if phase != 'started':
                details = json.loads(row['details'])
                tokens = fields.get('tokens')
                amount = None
                if tokens is not None:
                    values = [tokens['input'],tokens['output'],tokens['reasoning'],tokens['cache']['read'],tokens['cache']['write']]
                    if any(type(n) not in {float,int} or not math.isfinite(n) or n<0 for n in values):
                        raise ValidationError('Invalid native token usage')
                    amount = sum(values)
                cost = fields.get('cost')
                if cost is not None and (type(cost) not in {float,int} or not math.isfinite(cost) or cost<0):
                    raise ValidationError('Invalid native cost')
                details.update(native_phase=phase,tokens=tokens,cost=cost)
                db.execute('INSERT INTO usage_adjustments VALUES(?,?,?,?,?,?,?)',
                    (str(uuid4()),row['observation_id'],row['amount'],amount,'actual' if amount is not None else 'unknown',json.dumps(details),timestamp()))
                db.execute('UPDATE usage_observations SET amount=?,completeness=?,details=? WHERE source_key=?',
                           (amount,'actual' if amount is not None else 'unknown',json.dumps(details),key))
            return {'source_key':key,'binding':json.loads(row['details'])['binding']}
        intent = {key:value for key,value in fields.items() if key!='request_key'}
        return self._mutate('runtime.native_event',dict(host=host,session=session,fields=intent),operation,work)

    def runtime_budget_set(self, host, session, limits, reason, operation):
        if not isinstance(limits, dict) or set(limits)-LIMITS or not isinstance(reason,str) or not reason.strip():
            raise ValidationError('Explicit runtime limits and reason required')
        if any(value is not None and (type(value) is not int or value<=0) for value in limits.values()):
            raise ValidationError('Runtime limits must be positive integers or null (disabled)')
        payload = dict(host=host,session=session,limits=limits,reason=reason)
        def work(db):
            config = self._project(db)['config']
            before = config.get('runtime_budget', {})
            updated = {**before, **limits}
            config['runtime_budget'] = updated
            db.execute('UPDATE project SET config=?', (json.dumps(config),))
            run = db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()
            if run:
                stored = db.execute('SELECT started_at FROM runtime_budgets WHERE generation=?', (run[0],)).fetchone()
                if stored:
                    seconds = updated.get('max_wall_seconds')
                    deadline = (datetime.fromisoformat(stored[0])+timedelta(seconds=seconds)).isoformat() if seconds else None
                    db.execute('UPDATE runtime_budgets SET limits=?,deadline_at=?,updated_at=? WHERE generation=?',
                               (json.dumps(updated),deadline,timestamp(),run[0]))
            self._event(db,'runtime.budget_changed', {**payload,'before':before,'limits':updated})
            return updated
        return self._mutate('runtime.budget_set',payload,operation,work)

    def runtime_budget_status(self, host, session):
        with self._read() as db:
            return view(self,db,host,session)

    def runtime_reserve(self, host, session, operation):
        def work(db):
            run = db.execute('SELECT generation,state FROM workflow_project WHERE id=1').fetchone()
            # Specialists are mediated one-shot requests, including manual node work.
            start(self, db, run['generation'], timestamp())
            state = view(self,db,host,session)
            if state['reason']:
                db.execute("UPDATE workflow_sessions SET pause_reason='budget' WHERE session_id=?", (session,))
                return {'allowed':False, **state}
            rid = 'BR-' + uuid4().hex
            upper = db.execute('SELECT COALESCE(MAX(event_id),0) FROM events').fetchone()[0]
            db.execute('''INSERT INTO runtime_progress VALUES(?,?,?,?,0,NULL)
                ON CONFLICT(generation,host_id,session_id) DO UPDATE SET last_event_sequence=excluded.last_event_sequence''',
                (run['generation'],host,session,upper))
            db.execute('INSERT INTO runtime_budget_reservations VALUES(?,?,?,?,?,?,?,?)',
                       (rid,run['generation'],'turn',session,state['scope_owner'],operation,'reserved',timestamp()))
            return {'allowed':True,'reservation_id':rid,**state}
        return self._mutate('runtime.reserve',dict(host=host,session=session),operation,work)

    def runtime_budget_stop(self, host, session, operation):
        def work(db):
            state = view(self,db,host,session)
            if not state['expired'] or state['run_state'] != 'running':
                return {'stopped':False,**state}
            db.execute("UPDATE workflow_project SET state='paused' WHERE id=1")
            db.execute("UPDATE project SET control='paused'")
            db.execute("UPDATE exploration_tasks SET state='cancelled',error='wall_clock_budget: queued request cancelled',updated_at=? WHERE state='queued'",(timestamp(),))
            db.execute("UPDATE exploration_tasks SET state='unverified',error='wall_clock_budget: exit not yet verified',updated_at=? WHERE state IN ('starting','running','waiting','stopping')",(timestamp(),))
            db.execute("UPDATE specialist_tasks SET state='unverified',exit_verified=0 WHERE state IN ('starting','running')")
            db.execute("UPDATE workflow_sessions SET pause_reason='budget' WHERE detached=0")
            self._event(db,'runtime.budget_exhausted',state)
            return {'stopped':True,**state}
        return self._mutate('runtime.budget_stop',dict(host=host,session=session),operation,work)

    def runtime_turn_finished(self, host, session, turn, operation):
        def work(db):
            from .review_lifecycle import generation
            gen = generation(db)
            previous = db.execute('SELECT * FROM runtime_progress WHERE generation=? AND host_id=? AND session_id=?',(gen,host,session)).fetchone()
            if previous and previous['last_finished_turn']==turn:
                return {'idle_turns':previous['idle_turns'],'meaningful':False,'duplicate':True}
            lower = previous['last_event_sequence'] if previous else 0
            upper = db.execute('SELECT COALESCE(MAX(event_id),0) FROM events').fetchone()[0]
            associated = db.execute('SELECT association_id FROM associations WHERE host_id=? AND session_id=? AND ended_at IS NULL',(host,session)).fetchone()
            mine = owner(db,host,session)
            meaningful = False
            for row in db.execute('SELECT kind,data FROM events WHERE event_id>? AND event_id<=?',(lower,upper)):
                data = json.loads(row['data'])
                kind = row['kind']
                if kind == 'knowledge.recorded' and (data.get('node_id') == (mine[5:] if mine.startswith('node:') else None)):
                    ref = data['ref']
                    k = db.execute('SELECT statement,scope,conditions,evidence_refs,dependencies FROM knowledge_revisions WHERE knowledge_id=? AND revision=?',
                                   (ref.split('/')[1].split('@')[0],int(ref.split('@')[1]))).fetchone()
                    duplicates = db.execute('''SELECT COUNT(*) FROM knowledge_revisions r JOIN knowledge_entries e USING(knowledge_id)
                        WHERE e.node_id IS ? AND r.statement=? AND r.scope=? AND r.conditions=? AND r.evidence_refs=? AND r.dependencies=?''',
                        ((mine[5:] if mine.startswith('node:') else None),*tuple(k))).fetchone()[0]
                    meaningful |= bool(json.loads(k['evidence_refs'])) and duplicates == 1
                elif kind == 'knowledge.revised':
                    ref = data['ref']; kid,revision=ref.split('/')[1].split('@'); revision=int(revision)
                    entry = db.execute('SELECT node_id FROM knowledge_entries WHERE knowledge_id=?',(kid,)).fetchone()
                    if entry[0] == (mine[5:] if mine.startswith('node:') else None):
                        values = list(db.execute('SELECT evidence_refs,scope,conditions,status FROM knowledge_revisions WHERE knowledge_id=? AND revision IN (?,?) ORDER BY revision',(kid,revision-1,revision)))
                        meaningful |= len(values)==2 and tuple(values[0])!=tuple(values[1])
                        change = db.execute('SELECT kind,affected_scope_mode FROM knowledge_changes WHERE change_id=?',(data['change_id'],)).fetchone()
                        meaningful |= change['kind'] != 'reword' and change['affected_scope_mode'] != 'none'
                elif kind == 'publication.created':
                    pub = db.execute('SELECT p.rowid,p.node_id,a.association_id FROM publications p JOIN attempts a USING(attempt_id) WHERE p.publication_id=?',(data['publication_id'],)).fetchone()
                    if pub and associated and pub['association_id']==associated[0]:
                        materials=publication_materials(db,data['publication_id'])
                        prior=db.execute('SELECT publication_id FROM publications WHERE node_id IS ? AND rowid<?',(pub['node_id'],pub['rowid']))
                        meaningful |= bool(materials) and not any(publication_materials(db,row[0])==materials for row in prior)
                elif mine=='project' and kind=='node.proposed':
                    duplicates = db.execute('SELECT COUNT(*) FROM nodes WHERE question=? AND plan=? AND inputs=?',
                        (data['question'],data['plan'],json.dumps(data['inputs'],ensure_ascii=False,sort_keys=True))).fetchone()[0]
                    meaningful |= duplicates==1
                elif mine=='project' and kind=='project.concluded':
                    meaningful = True
                elif kind=='review.scope_changed':
                    meaningful |= data['actor'].get('host_id')==host and data['actor'].get('session_id')==session and bool(data['added'])
                elif kind == 'review.completed':
                    meaningful |= data.get('node_id') == (mine[5:] if mine.startswith('node:') else None)
                elif kind == 'knowledge.disposed':
                    kid = data['affected_version'].split('/')[1].split('@')[0]
                    target_owner = db.execute('SELECT node_id FROM knowledge_entries WHERE knowledge_id=?',(kid,)).fetchone()[0]
                    current = db.execute('SELECT kind,evidence_refs,replacement_ref,source_sequence FROM knowledge_dispositions WHERE disposition_id=?',(data['disposition_id'],)).fetchone()
                    prior = db.execute('''SELECT kind,evidence_refs,replacement_ref FROM knowledge_dispositions
                        WHERE change_id=? AND affected_version=? AND source_sequence<? ORDER BY source_sequence DESC LIMIT 1''',
                        (data['change_id'],data['affected_version'],current['source_sequence'])).fetchone()
                    meaningful |= target_owner == (mine[5:] if mine.startswith('node:') else None) and (
                        prior is None or tuple(current)[:3] != tuple(prior))
            idle = 0 if meaningful else (previous['idle_turns'] if previous else 0)+1
            db.execute('INSERT INTO runtime_progress VALUES(?,?,?,?,?,?) ON CONFLICT(generation,host_id,session_id) DO UPDATE SET last_event_sequence=excluded.last_event_sequence,idle_turns=excluded.idle_turns,last_finished_turn=excluded.last_finished_turn',
                       (gen,host,session,upper,idle,turn))
            return {'idle_turns':idle,'meaningful':bool(meaningful)}
        return self._mutate('runtime.turn_finished',dict(host=host,session=session,turn=turn),operation,work)

    def runtime_execution_end(self, host, session, turn, outcome, operation):
        def work(db):
            db.execute("UPDATE exploration_tasks SET state='waiting',error='wall_clock_budget: session exit verified; paused work',updated_at=? WHERE session_id=? AND state='unverified' AND error LIKE 'wall_clock_budget:%'",(timestamp(),session))
            return {'turn':turn}
        return self._mutate('runtime.execution_end',dict(host=host,session=session,turn=turn,outcome=outcome),
                            operation,work)

    def runtime_submitted(self, host, session, reservation, operation):
        def work(db):
            row=db.execute('SELECT session_id FROM runtime_budget_reservations WHERE reservation_id=?',(reservation,)).fetchone()
            if not row or row[0]!=session:
                raise ValidationError('Reservation belongs to another session')
            db.execute("UPDATE runtime_budget_reservations SET state='submitted' WHERE reservation_id=?",(reservation,))
            return {'submitted':True}
        return self._mutate('runtime.submitted',dict(host=host,session=session,reservation=reservation),operation,work)
