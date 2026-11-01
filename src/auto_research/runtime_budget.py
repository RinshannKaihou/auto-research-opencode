"""Durable limits for mediated autonomous dispatch; external work stays unmetered."""
import json
import math
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .errors import ValidationError, BudgetError, ConflictError, ResearchControlError, ResearchExecutionUnverified

LIMITS = {'max_session_turns','max_total_nodes','max_wall_seconds','max_tokens','max_cost',
          'shared_work_budget', 'resource_capacities'}
SOURCE_ALGORITHM = 'source-worker-v1'


def controller_phase(db):
    row = db.execute("SELECT state FROM workflow_intents WHERE intent_id='controller-owner:v1' AND kind='controller_owner'").fetchone()
    return row['state'] if row else 'unrecorded'


def require_new_work(db, host, session, *, manual=False, waiting=False, role=None):
    """Check authorization in the same transaction that creates new work."""
    run = db.execute('SELECT state,control_revision FROM workflow_project WHERE id=1').fetchone()
    actor = db.execute('SELECT role,pause_reason,detached FROM workflow_sessions WHERE host_id=? AND session_id=?',
                       (host, session)).fetchone()
    if not actor or actor['detached'] or (role and actor['role'] != role):
        raise ValidationError('The current session is not authorized for this operation')
    allowed_states = {'running', 'manual'} if manual else {'running'}
    allowed_pauses = {None, 'wait'} if waiting else {None}
    if not run or run['state'] not in allowed_states or actor['pause_reason'] not in allowed_pauses:
        raise ResearchControlError(run['state'] if run else None, actor['pause_reason'],
                                   run['control_revision'] if run else None, controller_phase(db))
    return actor


def require_model_drain(db, host, session, execution_token):
    """Only an authenticated in-flight node turn may write during soft pause."""
    actor = db.execute('SELECT role,pause_reason,detached FROM workflow_sessions WHERE host_id=? AND session_id=?',
                       (host, session)).fetchone()
    if not actor or actor['detached'] or actor['role'] != 'node_core':
        return
    run = db.execute('SELECT state,generation,control_revision FROM workflow_project WHERE id=1').fetchone()
    unknown = db.execute("SELECT task_id FROM exploration_tasks WHERE session_id=? AND state='unverified' LIMIT 1", (session,)).fetchone()
    if unknown:
        raise ResearchExecutionUnverified(run['state'], run['control_revision'], unknown['task_id'],
                                          actor['pause_reason'], controller_phase(db))
    if run['state'] == 'manual' or (run['state'] == 'running' and actor['pause_reason'] in {None, 'finished', 'segment_complete'}):
        return
    if run['state'] == 'paused' and actor['pause_reason'] in {None, 'project'} and execution_token:
        row = db.execute("SELECT * FROM workflow_intents WHERE intent_id=? AND kind='execution_start' AND session_id=?",
                         ('execution-start:' + execution_token, session)).fetchone()
        details = json.loads(row['details']) if row else {}
        task = db.execute("SELECT 1 FROM exploration_tasks WHERE session_id=? AND generation=? AND state IN ('starting','running','waiting')", (session, run['generation'])).fetchone()
        if (row and row['state'] == 'open' and details.get('host') == host and details.get('generation') == run['generation']
                and not details.get('ambiguous') and details.get('reservation_id') and details.get('message_id') and task):
            return
    raise ResearchControlError(run['state'], actor['pause_reason'], run['control_revision'], controller_phase(db))


def source_owner(db,host,session):
    row=db.execute("""SELECT w.cwd,w.node_id,t.attempt_id,p.generation FROM workflow_sessions w
        JOIN workflow_project p ON p.id=1
        LEFT JOIN associations a ON a.host_id=w.host_id AND a.session_id=w.session_id AND a.ended_at IS NULL
        LEFT JOIN attempts t ON t.association_id=a.association_id AND t.ended_at IS NULL
        WHERE w.host_id=? AND w.session_id=? AND w.detached=0 AND w.role='node_core'""",(host,session)).fetchone()
    return dict(row) if row else None


def source_digest(db, host, session, snapshot):
    if snapshot is None:
        return None
    if not isinstance(snapshot,dict) or not isinstance(snapshot.get('owner'),dict):
        raise ValidationError('Invalid host source snapshot')
    digest=snapshot.get('digest')
    if digest is not None and (not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)):
        raise ValidationError('Invalid host source digest')
    return digest if snapshot.get('algorithm')==SOURCE_ALGORITHM and source_owner(db,host,session)==snapshot['owner'] else None


def source_identity(snapshot):
    return hashlib.sha256(json.dumps(snapshot['owner'],sort_keys=True).encode()).hexdigest()


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
    turns = db.execute("SELECT COUNT(*) FROM runtime_budget_reservations WHERE generation=? AND kind='turn' AND owner=? AND state!='not_sent'", (gen,scope_owner)).fetchone()[0]
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
            'exploration': exploration_budget_view(store, db),
            'run_state':row['state'] if row else None}


def check_node(store, db):
    limit = store._project(db)['config'].get('runtime_budget', {}).get('max_total_nodes')
    if limit is not None and db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0] >= limit:
        raise BudgetError('Project total-node limit reached; explicit budget change required')


def publication_materials(db, publication):
    return [tuple(row) for row in db.execute('''SELECT kind,content,object_version,object_kind,source_path,knowledge_refs
        FROM publication_items WHERE publication_id=? ORDER BY kind,content,object_version,object_kind,source_path,knowledge_refs''',(publication,))]


def _resource_requirements(value):
    if not isinstance(value, dict) or any(
            not isinstance(name, str) or not name.strip() or len(name) > 200
            or type(amount) is not int or amount <= 0 for name, amount in value.items()):
        raise ValidationError('Resources must map nonempty names to positive integer units')
    return value


def _allocations_available(db):
    return db.execute('PRAGMA user_version').fetchone()[0] >= 16


def exploration_budget_view(store, db):
    """Planned work units and resource leases, never inferred actual expenditure."""
    if not _allocations_available(db):
        return {'available': False, 'semantics': 'legacy'}
    generation = db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()[0]
    limits = store._project(db)['config'].get('runtime_budget', {})
    rows = list(db.execute('SELECT a.*,r.state,r.generation FROM exploration_allocations a '
                          'JOIN runtime_budget_reservations r USING(reservation_id) '
                          'WHERE r.generation=? ORDER BY a.rowid', (generation,)))
    allocations = []
    for row in rows:
        item = dict(row)
        item['resources'] = json.loads(item['resources'])
        item['actual_usage_status'] = 'unknown' if item['actual_usage'] is None else 'observed'
        allocations.append(item)
    by_scope = {}
    for item in allocations:
        totals = by_scope.setdefault(item['scope_id'], {'allocated': 0, 'reserved': 0, 'committed': 0,
                                                       'released': 0, 'work_items': 0, 'actual_usage': None})
        totals['work_items'] += 1
        if item['state'] != 'released':
            totals['allocated'] += item['amount']
        if item['state'] in {'reserved', 'committed', 'released'}:
            totals[item['state']] += item['amount']
    held = list(db.execute("SELECT a.intent_id,a.task_id,a.resources,a.resource_state,r.generation,t.state task_state "
        "FROM exploration_allocations a JOIN runtime_budget_reservations r USING(reservation_id) "
        "LEFT JOIN exploration_tasks t ON t.task_id=a.task_id WHERE a.resource_state IN ('held','uncertain') "
        "ORDER BY a.rowid"))
    resource_usage = {}
    leases = []
    for row in held:
        item = dict(row)
        item['resources'] = json.loads(item['resources'])
        leases.append(item)
        for name, amount in item['resources'].items():
            resource_usage[name] = resource_usage.get(name, 0) + amount
    return {'available': True, 'unit': 'work_units', 'limit': limits.get('shared_work_budget'),
            'allocated': sum(item['amount'] for item in allocations if item['state'] != 'released'),
            'reserved': sum(item['amount'] for item in allocations if item['state'] == 'reserved'),
            'committed': sum(item['amount'] for item in allocations if item['state'] == 'committed'),
            'released': sum(item['amount'] for item in allocations if item['state'] == 'released'),
            'actual_usage': None, 'actual_usage_status': 'unknown',
            'accounting': 'planned work allocations; actual tokens/cost remain separate observations',
            'allocations': allocations, 'by_scope': by_scope,
            'resource_capacities': limits.get('resource_capacities') or {},
            'resources_held': resource_usage, 'resource_leases': leases,
            'resource_coverage': 'declared managed work; external exit requires operator verification'}


def reserve_exploration_work(store, db, intent_id, details):
    """Reserve one shared work item in the caller's decision transaction."""
    if not _allocations_available(db):
        return {'available': False, 'semantics': 'legacy'}
    work = details['next_work']
    amount = work.get('budget_amount', 1)
    if type(amount) not in {int, float} or not math.isfinite(amount) or amount <= 0:
        raise ValidationError('budget_amount must be a finite positive number of work units')
    resources = _resource_requirements(work.get('resources', {}))
    limits = store._project(db)['config'].get('runtime_budget', {})
    capacities = limits.get('resource_capacities') or {}
    for name, requested in resources.items():
        if name not in capacities or requested > capacities[name]:
            raise ValidationError(f'Resource {name} needs an explicit sufficient capacity')
    existing = db.execute('SELECT a.*,r.state,r.generation FROM exploration_allocations a '
                          'JOIN runtime_budget_reservations r USING(reservation_id) WHERE intent_id=?',
                          (intent_id,)).fetchone()
    if existing:
        if (existing['amount'] != amount or json.loads(existing['resources']) != resources
                or existing['generation'] != details['generation'] or existing['scope_id'] != details['scope_id']):
            raise ConflictError('Exploration work allocation identity changed')
        return dict(existing)
    used = db.execute("SELECT COALESCE(SUM(a.amount),0) FROM exploration_allocations a "
                      "JOIN runtime_budget_reservations r USING(reservation_id) "
                      "WHERE r.generation=? AND r.state!='released'", (details['generation'],)).fetchone()[0]
    limit = limits.get('shared_work_budget')
    if limit is not None and used + amount > limit:
        raise BudgetError(f'Shared exploration work budget exceeded: allocated={used}, requested={amount}, limit={limit}')
    reservation = 'exploration-budget:' + hashlib.sha256(intent_id.encode()).hexdigest()[:32]
    db.execute('INSERT INTO runtime_budget_reservations VALUES(?,?,?,?,?,?,?,?)',
               (reservation, details['generation'], 'exploration_work', None, 'project', intent_id,
                'reserved', timestamp()))
    db.execute('INSERT INTO exploration_allocations VALUES(?,?,?,?,?,?,?,?,?,?)',
               (reservation, intent_id, details['scope_id'], None, amount, 'work_units',
                json.dumps(resources, sort_keys=True), 'unclaimed', None, timestamp()))
    return {'reservation_id': reservation, 'intent_id': intent_id, 'amount': amount,
            'unit': 'work_units', 'state': 'reserved', 'actual_usage': None}


def materialize_exploration_work(store, db, intent_id, task_id):
    if not _allocations_available(db):
        return
    row = db.execute('SELECT reservation_id,task_id FROM exploration_allocations WHERE intent_id=?',
                     (intent_id,)).fetchone()
    if not row:
        return
    if row['task_id'] and row['task_id'] != task_id:
        raise ConflictError('Exploration allocation already belongs to another task')
    db.execute('UPDATE exploration_allocations SET task_id=?,updated_at=? WHERE intent_id=?',
               (task_id, timestamp(), intent_id))
    db.execute("UPDATE runtime_budget_reservations SET state='committed' WHERE reservation_id=? AND state='reserved'",
               (row['reservation_id'],))


def release_exploration_work(store, db, intent_id):
    """Release only work with no host submission, not completed/uncertain usage."""
    if not _allocations_available(db):
        return {'released': False, 'reason': 'legacy'}
    row = db.execute('SELECT * FROM exploration_allocations WHERE intent_id=?', (intent_id,)).fetchone()
    if not row:
        return {'released': False, 'reason': 'no_allocation'}
    if row['task_id']:
        task = db.execute('SELECT state FROM exploration_tasks WHERE task_id=?', (row['task_id'],)).fetchone()
        submitted = db.execute("SELECT 1 FROM workflow_intents WHERE kind='node_launch' "
            "AND json_extract(details,'$.task_id')=? AND state!='not_sent' LIMIT 1", (row['task_id'],)).fetchone()
        if not task or task['state'] not in {'queued', 'cancelled'} or submitted:
            return {'released': False, 'reason': 'work_may_have_started'}
    db.execute("UPDATE runtime_budget_reservations SET state='released' WHERE reservation_id=?", (row['reservation_id'],))
    db.execute("UPDATE exploration_allocations SET resource_state='released',updated_at=? WHERE intent_id=?",
               (timestamp(), intent_id))
    return {'released': True, 'reservation_id': row['reservation_id']}


def _release_task_resources(db, task_id):
    if _allocations_available(db):
        db.execute("UPDATE exploration_allocations SET resource_state='released',updated_at=? WHERE task_id=?",
                   (timestamp(), task_id))


def claim_exploration_resources(store, db, task_id):
    if not _allocations_available(db):
        return {'allowed': True, 'exploration': False}
    row = db.execute('SELECT * FROM exploration_allocations WHERE task_id=?', (task_id,)).fetchone()
    if not row:
        return {'allowed': True, 'exploration': False}
    if row['resource_state'] in {'held', 'uncertain'}:
        return {'allowed': True, 'exploration': True, 'resources': json.loads(row['resources'])}
    requested = json.loads(row['resources'])
    capacities = store._project(db)['config'].get('runtime_budget', {}).get('resource_capacities') or {}
    occupied = {}
    for lease in db.execute("SELECT resources FROM exploration_allocations WHERE resource_state IN ('held','uncertain')"):
        for name, amount in json.loads(lease['resources']).items():
            occupied[name] = occupied.get(name, 0) + amount
    missing = {name: {'requested': amount, 'held': occupied.get(name, 0), 'capacity': capacities.get(name)}
               for name, amount in requested.items()
               if name not in capacities or occupied.get(name, 0) + amount > capacities[name]}
    if missing:
        return {'allowed': False, 'reason': 'waiting_resource', 'state': 'queued', 'resources': missing}
    db.execute("UPDATE exploration_allocations SET resource_state='held',updated_at=? WHERE reservation_id=?",
               (timestamp(), row['reservation_id']))
    return {'allowed': True, 'exploration': True, 'resources': requested}


class RuntimeStore:
    def runtime_turn_not_sent(self, host, session, message_id, operation):
        """Revoke a standalone nudge only with the host's pre-API not-sent proof."""
        if not isinstance(message_id, str) or not message_id.startswith('msg_') or len(message_id) > 256:
            raise ValidationError('A bounded host message ID is required')

        def work(db):
            self._association(db, host, session)
            row = db.execute("SELECT * FROM workflow_intents WHERE kind='turn_authorization' AND session_id=? "
                             "AND json_extract(details,'$.host')=? AND json_extract(details,'$.message_id')=? ORDER BY rowid DESC LIMIT 1",
                             (session, host, message_id)).fetchone()
            if not row:
                return {'refunded': False, 'reason': 'no_reservation'}
            details = json.loads(row['details'])
            rid = details['reservation_id']
            reserved = db.execute('SELECT state FROM runtime_budget_reservations WHERE reservation_id=?', (rid,)).fetchone()
            if reserved and reserved['state'] == 'not_sent' and row['state'] == 'revoked':
                return {'refunded': True}
            if not reserved or reserved['state'] != 'reserved' or row['state'] != 'issued':
                raise ConflictError('Turn may have started; cannot declare it unsent')
            db.execute("UPDATE workflow_intents SET state='revoked',updated_at=? WHERE intent_id=?", (timestamp(), row['intent_id']))
            db.execute("UPDATE runtime_budget_reservations SET state='not_sent' WHERE reservation_id=?", (rid,))
            self._event(db, 'runtime.turn_not_sent', {'session': session, 'message_id': message_id, 'reservation_id': rid})
            return {'refunded': True}
        return self._mutate('runtime.turn_not_sent', dict(host=host, session=session, message_id=message_id), operation, work)

    def runtime_outbound_not_sent(self, host, session, intent_id, stage, reason, operation):
        """Host-only proof that its API was never invoked; never infer from an RPC error."""
        if stage not in {'create', 'synthetic'} or not isinstance(reason, str) or not reason.strip():
            raise ValidationError('A pre-submission stage and reason are required')
        payload = dict(host=host, session=session, intent_id=intent_id, stage=stage, reason=reason[:2000])

        def work(db):
            self._association(db, host, session)
            row = db.execute('SELECT * FROM workflow_intents WHERE intent_id=? AND session_id=?', (intent_id, session)).fetchone()
            if not row or row['kind'] not in {'node_launch', 'specialist_launch', 'notification_delivery', 'notification_wake'}:
                raise ConflictError('Outbound intent does not belong to this session')
            details = json.loads(row['details'])
            if (row['kind'] != 'specialist_launch' and details.get('host') != host
                    or row['kind'] == 'specialist_launch' and details.get('parent') != session):
                raise ConflictError('Outbound host does not match')
            allowed = {'claimed', 'creating', 'prepared'} if stage == 'create' else {'claimed', 'created', 'ready', 'bound', 'sending', 'submitting'}
            if row['state'] not in allowed:
                raise ConflictError('Outbound stage changed; cannot declare it unsent')
            if stage == 'synthetic' and row['state'] == 'claimed' and not details.get('reuse'):
                raise ConflictError('A new session has not been confirmed')
            if row['kind'] in {'notification_delivery', 'notification_wake'} and stage != 'synthetic':
                raise ConflictError('Notifications do not create sessions')
            rid = details.get('reservation_id')
            if row['kind'] == 'notification_wake':
                auth = db.execute("SELECT details FROM workflow_intents WHERE kind='turn_authorization' AND json_extract(details,'$.message_id')=? ORDER BY rowid DESC LIMIT 1",
                                  (details['message_id'],)).fetchone()
                rid = json.loads(auth['details'])['reservation_id'] if auth else None
            if rid:
                auth = db.execute('SELECT state FROM workflow_intents WHERE intent_id=?', ('turn-authorization:' + rid,)).fetchone()
                reservation = db.execute('SELECT state FROM runtime_budget_reservations WHERE reservation_id=?', (rid,)).fetchone()
                if not auth or auth['state'] != 'issued' or not reservation or reservation['state'] != 'reserved':
                    raise ConflictError('Execution may have started; cannot refund this submission')
            paused = False
            if row['kind'] == 'node_launch':
                task = db.execute('SELECT * FROM exploration_tasks WHERE task_id=?', (details['task_id'],)).fetchone()
                latest = db.execute("SELECT intent_id FROM workflow_intents WHERE kind='node_launch' AND json_extract(details,'$.task_id')=? ORDER BY rowid DESC LIMIT 1", (details['task_id'],)).fetchone()
                if not task or task['state'] != 'starting' or task['session_id'] != details['session_id'] or latest['intent_id'] != intent_id:
                    raise ConflictError('Task changed; preserve the current recovery state')
                db.execute("UPDATE exploration_tasks SET state='queued',error=?,updated_at=? WHERE task_id=?", (reason[:2000], timestamp(), details['task_id']))
                _release_task_resources(db, details['task_id'])
                recent = list(db.execute("SELECT state FROM workflow_intents WHERE kind='node_launch' AND json_extract(details,'$.task_id')=? AND intent_id!=? ORDER BY rowid DESC LIMIT 2",
                                         (details['task_id'], intent_id)))
                paused = len(recent) == 2 and all(item['state'] == 'not_sent' for item in recent)
                if paused:
                    db.execute("UPDATE workflow_sessions SET pause_reason='fault' WHERE host_id=? AND session_id=?", (host, session))
            elif row['kind'] == 'specialist_launch':
                task = db.execute('SELECT state FROM specialist_tasks WHERE task_id=?', (details['task_id'],)).fetchone()
                if not task or task['state'] not in {'starting', 'running'}:
                    raise ConflictError('Specialist changed; preserve its recovery state')
                db.execute("UPDATE specialist_tasks SET state='incomplete',exit_verified=1,error=?,updated_at=? WHERE task_id=?", (reason[:2000], timestamp(), details['task_id']))
            if rid:
                db.execute("UPDATE workflow_intents SET state='revoked',updated_at=? WHERE intent_id=?", (timestamp(), 'turn-authorization:' + rid))
                db.execute("UPDATE runtime_budget_reservations SET state='not_sent' WHERE reservation_id=?", (rid,))
            state = 'retryable' if row['kind'] in {'notification_delivery', 'notification_wake'} else 'not_sent'
            db.execute('UPDATE workflow_intents SET state=?,updated_at=? WHERE intent_id=?', (state, timestamp(), intent_id))
            self._event(db, 'runtime.outbound_not_sent', {**payload, 'reservation_id': rid})
            return {'rolled_back': True, 'state': state, 'paused': paused}
        return self._mutate('runtime.outbound_not_sent', payload, operation, work)

    def runtime_unverified(self, host, session):
        with self._read() as db:
            self._association(db, host, session)
            return {'tasks': db.execute("SELECT COUNT(*) FROM exploration_tasks WHERE state='unverified'").fetchone()[0],
                    'specialists': db.execute("SELECT COUNT(*) FROM specialist_tasks WHERE state='unverified'").fetchone()[0]}

    def runtime_execution_start(self, host, session, execution_token, operation, event_seq=None, ambiguous=False):
        if not isinstance(execution_token, str) or not execution_token:
            raise ValidationError('Execution token is required')
        if event_seq is not None and (not isinstance(event_seq, int) or isinstance(event_seq, bool) or event_seq < 0):
            raise ValidationError('Invalid native event sequence')
        payload = dict(host=host, session=session, execution_token=execution_token, event_seq=event_seq, ambiguous=bool(ambiguous))

        def work(db):
            self._association(db, host, session)
            run = db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()
            db.execute("UPDATE workflow_intents SET state='superseded',updated_at=? WHERE kind='execution_start' AND session_id=? AND state='open'", (timestamp(), session))
            details = {**payload, 'generation': run['generation']}
            db.execute('INSERT INTO workflow_intents VALUES(?,?,?,?,?,?)',
                       ('execution-start:' + execution_token, 'execution_start', session, 'open', json.dumps(details, sort_keys=True), timestamp()))
            return {'execution_token': execution_token, 'state': 'open'}
        return self._mutate('runtime.execution_start', payload, operation, work)

    def runtime_execution_bind(self, host, session, execution_token, message_id, operation, event_seq=None):
        if event_seq is not None and (not isinstance(event_seq, int) or isinstance(event_seq, bool) or event_seq < 0):
            raise ValidationError('Invalid native event sequence')
        payload = dict(host=host, session=session, execution_token=execution_token, message_id=message_id, event_seq=event_seq)

        def work(db):
            start = db.execute("SELECT * FROM workflow_intents WHERE intent_id=? AND kind='execution_start' AND session_id=? AND state='open'",
                               ('execution-start:' + execution_token, session)).fetchone()
            if not start:
                return {'matched': False}
            details = json.loads(start['details'])
            if details['host'] != host:
                return {'matched': False}
            if details.get('message_id') == message_id:
                return {'matched': True, 'execution_token': execution_token}
            previous_end = db.execute("SELECT MAX(CAST(json_extract(details,'$.event_seq') AS INTEGER)) FROM workflow_intents WHERE kind='execution_terminal' AND session_id=? AND json_extract(details,'$.host')=? AND json_extract(details,'$.event_seq') IS NOT NULL", (session, host)).fetchone()[0]
            if (details.get('ambiguous') or details.get('event_seq') is None or event_seq is None
                    or previous_end is not None and event_seq <= previous_end):
                run = db.execute('SELECT state FROM workflow_project WHERE id=1').fetchone()
                if run and run['state'] != 'running' and event_seq is None:
                    db.execute("UPDATE workflow_intents SET state='revoked',updated_at=? WHERE intent_id=?", (timestamp(), start['intent_id']))
                return {'matched': False}
            auth = db.execute("SELECT * FROM workflow_intents WHERE kind='turn_authorization' AND session_id=? AND state='issued' AND json_extract(details,'$.message_id')=? ORDER BY updated_at LIMIT 1",
                              (session, message_id)).fetchone()
            if not auth:
                run = db.execute('SELECT state FROM workflow_project WHERE id=1').fetchone()
                if run and run['state'] != 'running':
                    db.execute("UPDATE workflow_intents SET state='revoked',updated_at=? WHERE intent_id=?", (timestamp(), start['intent_id']))
                return {'matched': False}
            authorization = json.loads(auth['details'])
            if authorization['host'] != host or authorization['generation'] != details['generation']:
                return {'matched': False}
            details.update(reservation_id=authorization['reservation_id'], message_id=message_id)
            db.execute('UPDATE workflow_intents SET details=?,updated_at=? WHERE intent_id=?',
                       (json.dumps(details, sort_keys=True), timestamp(), start['intent_id']))
            db.execute("UPDATE workflow_intents SET state='bound',updated_at=? WHERE intent_id=?", (timestamp(), auth['intent_id']))
            return {'matched': True, 'execution_token': execution_token}
        return self._mutate('runtime.execution_bind', payload, operation, work)

    def runtime_drain_check(self, host, session, execution_token):
        with self._read() as db:
            require_model_drain(db, host, session, execution_token)
            return {'allowed': True}

    def runtime_execution_view(self, host, session, execution_token=None):
        with self._read() as db:
            self._association(db, host, session)
            row = db.execute("SELECT rowid AS sequence,state,details FROM workflow_intents WHERE kind='execution_start' AND session_id=? "
                             "AND (? IS NULL OR intent_id=?) ORDER BY rowid DESC LIMIT 1",
                             (session, execution_token, 'execution-start:' + execution_token if execution_token else None)).fetchone()
            if not row:
                return None
            details = json.loads(row['details'])
            if details.get('host') != host:
                return None
            pending = db.execute("SELECT 1 FROM workflow_intents WHERE kind='turn_authorization' AND session_id=? AND state='issued' "
                                 "AND rowid>? AND json_extract(details,'$.host')=? AND json_extract(details,'$.generation')=? LIMIT 1",
                                 (session, row['sequence'], host, details['generation'])).fetchone()
            terminal = db.execute("SELECT details FROM workflow_intents WHERE kind='execution_terminal' AND session_id=? "
                                  "AND json_extract(details,'$.execution_token')=? LIMIT 1", (session, details['execution_token'])).fetchone()
            return {'state': row['state'], 'has_unbound_turn': bool(pending),
                    'terminal': json.loads(terminal['details']) if terminal else None, **details}

    def node_launch_get(self, host, session, task_id):
        with self._read() as db:
            self._association(db, host, session)
            row = db.execute("SELECT * FROM workflow_intents WHERE kind='node_launch' AND session_id=? AND json_extract(details,'$.task_id')=? ORDER BY CAST(json_extract(details,'$.launch_no') AS INTEGER) DESC LIMIT 1", (session, task_id)).fetchone()
            return {'intent_id': row['intent_id'], 'state': row['state'], **json.loads(row['details'])} if row else None

    def specialist_launch_get(self, host, session, task_id):
        with self._read() as db:
            self._association(db, host, session)
            row = db.execute("SELECT * FROM workflow_intents WHERE intent_id=? AND kind='specialist_launch' AND session_id=?",
                             ('specialist-launch:' + task_id, session)).fetchone()
            return {'intent_id': row['intent_id'], 'state': row['state'], **json.loads(row['details'])} if row else None

    def specialist_launch_phase(self, host, session, intent_id, phase, operation):
        transitions = {'creating': {'prepared'}, 'created': {'creating'}, 'bound': {'created'}, 'submitted': {'sending'}}
        if phase not in transitions:
            raise ValidationError('Unknown specialist launch phase')
        payload = dict(host=host, session=session, intent_id=intent_id, phase=phase)

        def work(db):
            row = db.execute("SELECT * FROM workflow_intents WHERE intent_id=? AND kind='specialist_launch' AND session_id=?",
                             (intent_id, session)).fetchone()
            if not row:
                raise ConflictError('Specialist launch does not belong to this parent')
            details = json.loads(row['details'])
            if row['state'] not in transitions[phase]:
                if phase == 'creating':
                    return {'claimed': False, 'state': row['state'], 'intent_id': intent_id, **details}
                raise ConflictError('Invalid specialist launch phase transition')
            task = db.execute('SELECT state FROM specialist_tasks WHERE task_id=? AND parent_session_id=?',
                              (details['task_id'], session)).fetchone()
            if not task or task['state'] not in {'starting', 'running'}:
                raise ConflictError('Specialist is no longer active')
            db.execute('UPDATE workflow_intents SET state=?,updated_at=? WHERE intent_id=?', (phase, timestamp(), intent_id))
            self._event(db, 'runtime.specialist_launch_phase', {'intent_id': intent_id, 'phase': phase})
            return {'claimed': True, 'state': phase, 'intent_id': intent_id, **details}
        return self._mutate('runtime.specialist_launch_phase', payload, operation, work)

    def specialist_cancel_unstarted(self, host, session, task_id, operation):
        """Cancel only a reservation that never acquired the create claim."""
        payload = dict(host=host, session=session, task_id=task_id)

        def work(db):
            self._association(db, host, session)
            task = db.execute('SELECT state FROM specialist_tasks WHERE task_id=? AND parent_session_id=?',
                              (task_id, session)).fetchone()
            intent = db.execute("SELECT state FROM workflow_intents WHERE intent_id=? AND kind='specialist_launch' AND session_id=?",
                                ('specialist-launch:' + task_id, session)).fetchone()
            if not task or not intent:
                raise ConflictError('Specialist reservation does not belong to this parent')
            if task['state'] != 'starting' or intent['state'] != 'prepared':
                return {'cancelled': False, 'state': task['state'], 'launch_state': intent['state']}
            db.execute("UPDATE specialist_tasks SET state='cancelled',result='{}',exit_verified=1,error=?,updated_at=? WHERE task_id=?",
                       ('batch reservation failed before launch', timestamp(), task_id))
            db.execute("UPDATE workflow_intents SET state='cancelled',updated_at=? WHERE intent_id=?",
                       (timestamp(), 'specialist-launch:' + task_id))
            self._event(db, 'runtime.specialist_reservation_cancelled', {'task_id': task_id})
            return {'cancelled': True, 'state': 'cancelled'}
        return self._mutate('runtime.specialist_cancel_unstarted', payload, operation, work)

    def recovery_confirm_exit(self, host, session, task_id, reason, operation):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValidationError('A reason for the operator exit check is required')
        payload = dict(host=host, session=session, task_id=task_id, reason=reason.strip())

        def work(db):
            actor = db.execute("SELECT role,detached FROM workflow_sessions WHERE host_id=? AND session_id=?", (host, session)).fetchone()
            task = db.execute('SELECT * FROM exploration_tasks WHERE task_id=? AND parent_session_id=?', (task_id, session)).fetchone()
            if not actor or actor['role'] != 'main' or actor['detached'] or not task or task['state'] != 'unverified':
                raise ConflictError('Only the main may verify an unverified task it owns')
            association = db.execute('SELECT * FROM associations WHERE host_id=? AND session_id=? AND ended_at IS NULL',
                                     (host, task['session_id'])).fetchone()
            if association:
                attempt = db.execute('SELECT * FROM attempts WHERE association_id=? AND ended_at IS NULL', (association['association_id'],)).fetchone()
                if attempt:
                    if attempt['node_id'] != task['node_id'] or db.execute('SELECT 1 FROM focus_queue WHERE association_id=?', (association['association_id'],)).fetchone():
                        raise ConflictError('The current work segment needs separate reconciliation')
                    self._finish_in_tx(db, host, task['session_id'], 'stopped',
                                       {'reason': 'operator verified exit', 'verification_reason': reason.strip()})
            launch = db.execute("SELECT intent_id FROM workflow_intents WHERE kind='node_launch' AND json_extract(details,'$.task_id')=? ORDER BY CAST(json_extract(details,'$.launch_no') AS INTEGER) DESC LIMIT 1", (task_id,)).fetchone()
            db.execute("UPDATE exploration_tasks SET state='failed',error=?,updated_at=? WHERE task_id=?",
                       ('operator verified exit: ' + reason.strip(), timestamp(), task_id))
            db.execute("UPDATE workflow_intents SET state='revoked',updated_at=? WHERE session_id=? AND kind IN ('execution_start','turn_authorization') AND state IN ('open','issued')",
                       (timestamp(), task['session_id']))
            details = {'task_id': task_id, 'launch_id': launch['intent_id'] if launch else None,
                       'session_id': task['session_id'], 'reason': reason.strip(), 'verified_by': 'user'}
            self._event(db, 'runtime.task_exit_confirmed', details)
            _release_task_resources(db, task_id)
            return {'task_id': task_id, 'state': 'failed', 'exit_verified_by': 'user'}
        return self._mutate('runtime.recovery_confirm_exit', payload, operation, work)

    def runtime_resources_release(self, host, session, task_id, reason, operation):
        """Host/operator attestation of external exit; never a model assertion."""
        from .controller import CURRENT_MODEL_CALL
        if CURRENT_MODEL_CALL.get():
            raise ValidationError('Resource exit verification is a host/operator-only action')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValidationError('An operator verification reason is required')
        payload = dict(host=host, session=session, task_id=task_id, reason=reason.strip())

        def work(db):
            actor = db.execute("SELECT role,detached FROM workflow_sessions WHERE host_id=? AND session_id=?", (host, session)).fetchone()
            task = db.execute('SELECT * FROM exploration_tasks WHERE task_id=? AND parent_session_id=?', (task_id, session)).fetchone()
            run = db.execute('SELECT main_session_id FROM workflow_project WHERE id=1').fetchone()
            if (not actor or actor['role'] != 'main' or actor['detached'] or run['main_session_id'] != session
                    or not task or task['state'] not in {'finished', 'failed', 'cancelled'}):
                raise ConflictError('Only the current main may verify external exit of a terminal owned task; reconcile unverified execution first')
            _release_task_resources(db, task_id)
            self._event(db, 'runtime.resources_exit_verified', payload)
            return {'task_id': task_id, 'resources_released': True, 'exit_verified_by': 'user',
                    'work_budget_released': False, 'actual_usage_status': 'unknown'}
        return self._mutate('runtime.resources_release', payload, operation, work)

    def recovery_retry_task(self, host, session, task_id, reason, operation):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValidationError('A retry reason is required')
        payload = dict(host=host, session=session, task_id=task_id, reason=reason.strip())

        def work(db):
            require_new_work(db, host, session, waiting=True, role='main')
            task = db.execute('SELECT * FROM exploration_tasks WHERE task_id=? AND parent_session_id=?', (task_id, session)).fetchone()
            if not task or task['state'] != 'failed':
                raise ConflictError('Only a verified failed task can be retried')
            launch = db.execute("SELECT intent_id FROM workflow_intents WHERE kind='node_launch' AND json_extract(details,'$.task_id')=? ORDER BY CAST(json_extract(details,'$.launch_no') AS INTEGER) DESC LIMIT 1", (task_id,)).fetchone()
            launch_id = launch['intent_id'] if launch else None
            verified = db.execute("SELECT 1 FROM events WHERE kind='runtime.task_exit_confirmed' AND json_extract(data,'$.task_id')=? AND json_extract(data,'$.launch_id') IS ? LIMIT 1", (task_id, launch_id)).fetchone()
            if not verified:
                raise ConflictError('The last launch exit has not been verified by the user')
            run = db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()
            db.execute("UPDATE exploration_tasks SET state='queued',generation=?,error=NULL,updated_at=? WHERE task_id=?",
                       (run['generation'], timestamp(), task_id))
            self._event(db, 'runtime.task_retry_requested', {'task_id': task_id, 'reason': reason.strip(), 'verified_launch': launch_id})
            return {'task_id': task_id, 'state': 'queued', 'accepted': True}
        return self._mutate('runtime.recovery_retry_task', payload, operation, work)

    def recovery_confirm_specialist(self, host, session, task_id, reason, operation):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValidationError('A reason for the operator exit check is required')
        payload = dict(host=host, session=session, task_id=task_id, reason=reason.strip())

        def work(db):
            actor = db.execute('SELECT role,detached FROM workflow_sessions WHERE host_id=? AND session_id=?', (host, session)).fetchone()
            task = db.execute('SELECT * FROM specialist_tasks WHERE task_id=?', (task_id,)).fetchone()
            if not actor or actor['role'] != 'main' or actor['detached'] or not task or task['state'] != 'unverified':
                raise ConflictError('Only the main may verify an unverified specialist')
            db.execute("UPDATE specialist_tasks SET state='incomplete',exit_verified=1,error=?,updated_at=? WHERE task_id=?",
                       ('operator verified exit: ' + reason.strip(), timestamp(), task_id))
            if task['child_session_id']:
                db.execute("UPDATE workflow_intents SET state='revoked',updated_at=? WHERE session_id=? AND kind IN ('execution_start','turn_authorization') AND state IN ('open','issued')",
                           (timestamp(), task['child_session_id']))
            self._event(db, 'runtime.specialist_exit_confirmed', {'task_id': task_id, 'reason': reason.strip(), 'verified_by': 'user'})
            return {'task_id': task_id, 'state': 'incomplete', 'exit_verified_by': 'user'}
        return self._mutate('runtime.recovery_confirm_specialist', payload, operation, work)

    def node_launch_claim(self, host, session, task_id, concurrency, operation):
        if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
            raise ValidationError('Concurrency must be a positive integer')
        payload = dict(host=host, session=session, task_id=task_id, concurrency=concurrency)

        def work(db):
            require_new_work(db, host, session, waiting=True, role='main')
            task = db.execute('SELECT * FROM exploration_tasks WHERE task_id=? AND parent_session_id=?',
                              (task_id, session)).fetchone()
            if not task:
                raise ValidationError('The task does not belong to this coordinator')
            if task['state'] != 'queued':
                return {'claimed': False, 'reason': 'already_claimed', 'state': task['state']}
            from .exploration_store import check_exploration_task
            check = check_exploration_task(self, db, task_id)
            if not check['allowed']:
                return {'claimed': False, 'reason': check['reason'], 'state': check['state']}
            active = db.execute("SELECT COUNT(*) FROM exploration_tasks WHERE state IN ('starting','running','stopping','unverified')").fetchone()[0]
            if active >= concurrency:
                return {'claimed': False, 'reason': 'capacity', 'state': 'queued'}
            resources = claim_exploration_resources(self, db, task_id)
            if not resources['allowed']:
                db.execute('UPDATE exploration_tasks SET error=?,updated_at=? WHERE task_id=?',
                           ('waiting_resource: ' + json.dumps(resources.get('resources', {}), sort_keys=True), timestamp(), task_id))
                return {'claimed': False, **resources}
            run = db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()
            if task['generation'] != run['generation']:
                raise ConflictError('The task belongs to an earlier research generation')
            launch_no = db.execute("SELECT COUNT(*) FROM workflow_intents WHERE kind='node_launch' AND json_extract(details,'$.task_id')=?",
                                   (task_id,)).fetchone()[0] + 1
            reuse = bool(db.execute("SELECT 1 FROM workflow_sessions WHERE session_id=? AND detached=0 AND role='node_core'",
                                    (task['session_id'],)).fetchone())
            child = task['session_id'] if reuse else 'ses_research_' + uuid4().hex
            intent_id = f'node-launch:{task_id}:{launch_no}'
            details = dict(host=host, parent=session, task_id=task_id, node_id=task['node_id'],
                           generation=task['generation'], launch_no=launch_no, session_id=child,
                           message_id='msg_research_' + uuid4().hex, reuse=reuse)
            db.execute("INSERT INTO workflow_intents VALUES(?,?,?,?,?,?)",
                       (intent_id, 'node_launch', session, 'claimed', json.dumps(details, sort_keys=True), timestamp()))
            db.execute("UPDATE exploration_tasks SET state='starting',session_id=?,error=NULL,updated_at=? WHERE task_id=?",
                       (child, timestamp(), task_id))
            self._event(db, 'runtime.node_launch_claimed', {'intent_id': intent_id, 'task_id': task_id})
            return {'claimed': True, 'intent_id': intent_id, 'state': 'claimed', **details}
        return self._mutate('runtime.node_launch_claim', payload, operation, work)

    def node_launch_phase(self, host, session, intent_id, phase, operation):
        transitions = {'creating': {'claimed'}, 'created': {'claimed', 'creating'}, 'ready': {'created'}}
        if phase not in transitions:
            raise ValidationError('Unknown node launch phase')
        payload = dict(host=host, session=session, intent_id=intent_id, phase=phase)

        def work(db):
            row = db.execute("SELECT * FROM workflow_intents WHERE intent_id=? AND kind='node_launch' AND session_id=?",
                             (intent_id, session)).fetchone()
            if not row:
                raise ConflictError('Node launch does not belong to this coordinator')
            details = json.loads(row['details'])
            if details['host'] != host or row['state'] not in transitions[phase]:
                raise ConflictError('Invalid node launch phase transition')
            if db.execute("SELECT state FROM exploration_tasks WHERE task_id=?", (details['task_id'],)).fetchone()[0] != 'starting':
                raise ConflictError('Node launch is no longer starting')
            db.execute('UPDATE workflow_intents SET state=?,updated_at=? WHERE intent_id=?', (phase, timestamp(), intent_id))
            self._event(db, 'runtime.node_launch_phase', {'intent_id': intent_id, 'phase': phase})
            return {'intent_id': intent_id, 'state': phase, **details}
        return self._mutate('runtime.node_launch_phase', payload, operation, work)

    def node_launch_submitted(self, host, session, intent_id, message_id, cwd, operation):
        payload = dict(host=host, session=session, intent_id=intent_id, message_id=message_id, cwd=cwd)

        def work(db):
            row = db.execute("SELECT * FROM workflow_intents WHERE intent_id=? AND kind='node_launch' AND session_id=?",
                             (intent_id, session)).fetchone()
            if not row or row['state'] != 'sending':
                raise ConflictError('Node launch is not awaiting host confirmation')
            details = json.loads(row['details'])
            if details['host'] != host or details['message_id'] != message_id:
                raise ConflictError('Node launch confirmation does not match')
            task = db.execute("SELECT state,session_id FROM exploration_tasks WHERE task_id=?", (details['task_id'],)).fetchone()
            if not task or task['state'] != 'starting' or task['session_id'] != details['session_id']:
                raise ConflictError('Node launch no longer owns the starting task')
            db.execute("UPDATE exploration_tasks SET state='running',cwd=?,error=NULL,updated_at=? WHERE task_id=?",
                       (cwd, timestamp(), details['task_id']))
            db.execute("UPDATE workflow_intents SET state='submitted',updated_at=? WHERE intent_id=?", (timestamp(), intent_id))
            self._event(db, 'runtime.node_launch_submitted', {'intent_id': intent_id, 'task_id': details['task_id']})
            return {'intent_id': intent_id, 'state': 'submitted', **details}
        return self._mutate('runtime.node_launch_submitted', payload, operation, work)

    def notification_wake_begin(self, host, session, notification_id, operation):
        payload = dict(host=host, session=session, notification_id=notification_id)

        def work(db):
            actor = require_new_work(db, host, session, waiting=True, role='main')
            intent_id = 'notification-wake:' + notification_id
            previous = db.execute('SELECT * FROM workflow_intents WHERE intent_id=?', (intent_id,)).fetchone()
            if previous and previous['state'] != 'retryable':
                return {'intent_id': intent_id, 'state': previous['state'], 'send': False,
                        **json.loads(previous['details'])}
            current = db.execute('SELECT waiting FROM workflow_sessions WHERE host_id=? AND session_id=?',
                                 (host, session)).fetchone()
            notice = db.execute('SELECT * FROM workflow_notifications WHERE notification_id=? AND recipient=?',
                                (notification_id, session)).fetchone()
            exploration = notice and notice['kind'] == 'exploration_result'
            matched = notice and (notice['task_id'] in json.loads(current['waiting']) or exploration and notice['task_id'] is None)
            if (actor['pause_reason'] != 'wait' or not notice or notice['state'] != 'delivered'
                    or notice['kind'] not in {'finished', 'stopped', 'failed', 'unknown', 'exploration_result'}
                    or not matched or exploration and not self._exploration_notice_pending(db, notice)):
                raise ConflictError('The coordinator is not waiting for this delivered result')
            if previous:
                details = json.loads(previous['details'])
                db.execute("UPDATE workflow_intents SET state='submitting',updated_at=? WHERE intent_id=?", (timestamp(), intent_id))
                return {'intent_id': intent_id, 'state': 'submitting', 'send': True, **details}
            details = {**payload, 'message_id': 'msg_research_wake_' + hashlib.sha256(intent_id.encode()).hexdigest()[:24]}
            db.execute('INSERT INTO workflow_intents VALUES(?,?,?,?,?,?)',
                       (intent_id, 'notification_wake', session, 'submitting', json.dumps(details, sort_keys=True), timestamp()))
            self._event(db, 'runtime.wake_submitting', {'intent_id': intent_id, 'notification_id': notification_id})
            return {'intent_id': intent_id, 'state': 'submitting', 'send': True, **details}
        return self._mutate('runtime.wake_begin', payload, operation, work)

    def notification_wake_confirm(self, host, session, intent_id, message_id, operation):
        payload = dict(host=host, session=session, intent_id=intent_id, message_id=message_id)

        def work(db):
            row = db.execute("SELECT * FROM workflow_intents WHERE intent_id=? AND kind='notification_wake' AND session_id=?",
                             (intent_id, session)).fetchone()
            if not row or row['state'] != 'submitting':
                raise ConflictError('Coordinator wake is not awaiting confirmation')
            details = json.loads(row['details'])
            if details['host'] != host or details['message_id'] != message_id:
                raise ConflictError('Coordinator wake confirmation does not match')
            notice = db.execute('SELECT * FROM workflow_notifications WHERE notification_id=?', (details['notification_id'],)).fetchone()
            if notice and notice['kind'] == 'exploration_result' and not self._exploration_notice_pending(db, notice):
                raise ConflictError('Exploration result is no longer pending')
            db.execute("UPDATE workflow_sessions SET waiting='[]',pause_reason=NULL WHERE host_id=? AND session_id=? AND pause_reason='wait'",
                       (host, session))
            db.execute("UPDATE workflow_intents SET state='delivered',updated_at=? WHERE intent_id=?", (timestamp(), intent_id))
            self._event(db, 'runtime.wake_delivered', {'intent_id': intent_id})
            return {'intent_id': intent_id, 'state': 'delivered'}
        return self._mutate('runtime.wake_confirm', payload, operation, work)

    @staticmethod
    def _exploration_notice_pending(db, notice):
        return bool(db.execute('SELECT 1 FROM exploration_results r JOIN exploration_scopes s ON s.scope_id=r.scope_id '
            'JOIN workflow_project p ON p.generation=s.generation WHERE p.id=1 AND r.result_id=? '
            'AND r.handled_by_decision IS NULL AND p.main_session_id=?', (notice['reference'], notice['recipient'])).fetchone())

    def notification_begin(self, host, session, notification_id, operation, text=None):
        """Reserve a stable host message before attempting delivery once."""
        if not isinstance(text, str) or not text or len(text) > 32000:
            raise ValidationError('A bounded notification message is required')
        payload = dict(host=host, session=session, notification_id=notification_id, text=text)

        def work(db):
            role = db.execute("SELECT role,detached FROM workflow_sessions WHERE host_id=? AND session_id=?",
                              (host, session)).fetchone()
            notice = db.execute("SELECT * FROM workflow_notifications WHERE notification_id=? AND recipient=?",
                                (notification_id, session)).fetchone()
            if not role or role['role'] != 'main' or role['detached'] or not notice:
                raise ValidationError('Notification does not belong to the current coordinator')
            if notice['kind'] == 'exploration_result' and not self._exploration_notice_pending(db, notice):
                db.execute("UPDATE workflow_notifications SET state='claimed' WHERE notification_id=?", (notification_id,))
                return {'notification_id': notification_id, 'state': 'claimed', 'send': False}
            intent_id = 'notification:' + notification_id
            previous = db.execute('SELECT * FROM workflow_intents WHERE intent_id=?', (intent_id,)).fetchone()
            if previous:
                if previous['state'] == 'retryable' and notice['state'] == 'pending':
                    db.execute("UPDATE workflow_intents SET state='submitting',updated_at=? WHERE intent_id=?", (timestamp(), intent_id))
                    return {'intent_id': intent_id, 'state': 'submitting', 'send': True, **json.loads(previous['details'])}
                return {'intent_id': intent_id, 'state': previous['state'], 'send': False,
                        **json.loads(previous['details'])}
            if notice['state'] != 'pending':
                return {'intent_id': intent_id, 'state': notice['state'], 'send': False}
            message_id = 'msg_research_' + hashlib.sha256(intent_id.encode()).hexdigest()[:32]
            details = {**payload, 'message_id': message_id, 'text': text}
            db.execute('INSERT INTO workflow_intents VALUES(?,?,?,?,?,?)',
                       (intent_id, 'notification_delivery', session, 'submitting', json.dumps(details, sort_keys=True), timestamp()))
            self._event(db, 'runtime.notification_submitting', {'intent_id': intent_id, 'notification_id': notification_id})
            return {'intent_id': intent_id, 'state': 'submitting', 'send': True, **details}
        return self._mutate('runtime.notification_begin', payload, operation, work)

    def notification_confirm(self, host, session, intent_id, message_id, operation):
        payload = dict(host=host, session=session, intent_id=intent_id, message_id=message_id)

        def work(db):
            row = db.execute("SELECT * FROM workflow_intents WHERE intent_id=? AND kind='notification_delivery' AND session_id=?",
                             (intent_id, session)).fetchone()
            if not row or row['state'] != 'submitting':
                raise ConflictError('Notification delivery is not awaiting confirmation')
            details = json.loads(row['details'])
            if details['host'] != host or details['message_id'] != message_id:
                raise ConflictError('Notification confirmation does not match the reserved message')
            db.execute("UPDATE workflow_notifications SET state='delivered' WHERE notification_id=? AND recipient=? AND state='pending'",
                       (details['notification_id'], session))
            db.execute("UPDATE workflow_intents SET state='delivered',updated_at=? WHERE intent_id=?", (timestamp(), intent_id))
            self._event(db, 'runtime.notification_delivered', {'intent_id': intent_id, 'notification_id': details['notification_id']})
            return {'intent_id': intent_id, 'state': 'delivered', 'notification_id': details['notification_id']}
        return self._mutate('runtime.notification_confirm', payload, operation, work)

    def recovery_resolve_delivery(self, host, session, notification_id, kind, disposition, reason, operation):
        if kind not in {'report', 'wake'} or disposition not in {'delivered', 'not_sent'}:
            raise ValidationError('Choose report or wake, and delivered or not_sent')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValidationError('A reason for the delivery check is required')
        payload = dict(host=host, session=session, notification_id=notification_id, kind=kind, disposition=disposition, reason=reason.strip())

        def work(db):
            actor = db.execute('SELECT role,detached FROM workflow_sessions WHERE host_id=? AND session_id=?', (host, session)).fetchone()
            intent_id = ('notification:' if kind == 'report' else 'notification-wake:') + notification_id
            row = db.execute('SELECT * FROM workflow_intents WHERE intent_id=? AND session_id=?', (intent_id, session)).fetchone()
            if not actor or actor['role'] != 'main' or actor['detached'] or not row or row['state'] != 'submitting':
                raise ConflictError('No unconfirmed delivery belongs to this coordinator')
            details = json.loads(row['details'])
            if details.get('host') != host or details.get('notification_id') != notification_id:
                raise ConflictError('Delivery identity does not match')
            if disposition == 'delivered':
                if kind == 'report':
                    db.execute("UPDATE workflow_notifications SET state='delivered' WHERE notification_id=? AND recipient=? AND state='pending'", (notification_id, session))
                else:
                    db.execute("UPDATE workflow_sessions SET waiting='[]',pause_reason=NULL WHERE host_id=? AND session_id=? AND pause_reason='wait'", (host, session))
            state = 'delivered' if disposition == 'delivered' else 'retryable'
            db.execute('UPDATE workflow_intents SET state=?,updated_at=? WHERE intent_id=?', (state, timestamp(), intent_id))
            self._event(db, 'runtime.delivery_reconciled', {**payload, 'verified_by': 'user', 'message_id': details['message_id']})
            return {'notification_id': notification_id, 'kind': kind, 'state': state, 'verified_by': 'user'}
        return self._mutate('runtime.recovery_resolve_delivery', payload, operation, work)

    def node_finish_request(self, host, session, state, summary, turn_token, operation):
        if state not in {'finished', 'stopped'} or not isinstance(summary, str) or not summary.strip():
            raise ValidationError('A terminal node state and summary are required')
        if not isinstance(turn_token, str) or not turn_token:
            raise ValidationError('The current node execution has not been verified')
        payload = dict(host=host, session=session, state=state, summary=summary, turn_token=turn_token)

        def work(db):
            role = db.execute("SELECT role,detached FROM workflow_sessions WHERE host_id=? AND session_id=?", (host, session)).fetchone()
            if not role or role['role'] != 'node_core' or role['detached']:
                raise ValidationError('Only the current node executor can finish a node')
            require_model_drain(db, host, session, turn_token)
            association = self._association(db, host, session)
            attempt = self._attempt(db, association['association_id'])
            task = db.execute("SELECT task_id,generation FROM exploration_tasks WHERE session_id=? AND state IN ('starting','running','waiting')", (session,)).fetchone()
            if not task or attempt['node_id'] is None:
                raise ConflictError('No active node task belongs to this execution')
            intent_id = 'node-finish:' + attempt['attempt_id'] + ':' + hashlib.sha256(turn_token.encode()).hexdigest()[:24]
            details = {**payload, 'attempt_id': attempt['attempt_id'], 'node_id': attempt['node_id'],
                       'task_id': task['task_id'], 'generation': task['generation']}
            previous = db.execute("SELECT * FROM workflow_intents WHERE kind='node_finish' AND session_id=? "
                                  "AND state='pending' AND json_extract(details,'$.attempt_id')=? LIMIT 1",
                                  (session, attempt['attempt_id'])).fetchone()
            if previous:
                old = json.loads(previous['details'])
                failed = db.execute("SELECT 1 FROM workflow_intents WHERE kind='execution_terminal' AND session_id=? "
                                    "AND json_extract(details,'$.execution_token')=? AND json_extract(details,'$.outcome') IN ('failed','interrupted')",
                                    (session, old.get('turn_token'))).fetchone()
                if failed:
                    db.execute("UPDATE workflow_intents SET state='cancelled',updated_at=? WHERE intent_id=?", (timestamp(), previous['intent_id']))
                    previous = None
            if previous:
                if previous['kind'] != 'node_finish' or json.loads(previous['details']) != details:
                    raise ConflictError('This node already has a different pending finish request')
                return {'intent_id': previous['intent_id'], 'state': previous['state'], **details}
            if db.execute("SELECT 1 FROM workflow_intents WHERE kind='execution_terminal' AND session_id=? "
                          "AND json_extract(details,'$.execution_token')=?", (session, turn_token)).fetchone():
                raise ConflictError('The finish execution has already ended')
            db.execute('INSERT INTO workflow_intents VALUES(?,?,?,?,?,?)',
                       (intent_id, 'node_finish', session, 'pending', json.dumps(details, sort_keys=True), timestamp()))
            self._event(db, 'runtime.node_finish_requested', {'intent_id': intent_id, 'task_id': task['task_id']})
            return {'intent_id': intent_id, 'state': 'pending', **details}
        return self._mutate('runtime.node_finish_request', payload, operation, work)

    def node_finish_pending(self, host, session):
        with self._read() as db:
            self._association(db, host, session)
            row = db.execute("SELECT intent_id,state,details FROM workflow_intents WHERE session_id=? AND kind='node_finish' AND state='pending' ORDER BY updated_at DESC LIMIT 1", (session,)).fetchone()
            return {'intent_id': row['intent_id'], 'state': row['state'], **json.loads(row['details'])} if row else None

    def node_finish_commit(self, host, session, intent_id, turn_token, terminal, operation):
        payload = dict(host=host, session=session, intent_id=intent_id, turn_token=turn_token, terminal=terminal)

        def work(db):
            row = db.execute("SELECT * FROM workflow_intents WHERE intent_id=? AND kind='node_finish' AND session_id=?", (intent_id, session)).fetchone()
            if not row or row['state'] != 'pending':
                raise ConflictError('Node finish is not pending')
            details = json.loads(row['details'])
            if details['host'] != host or details['turn_token'] != turn_token:
                raise ConflictError('Node finish belongs to another execution')
            run = db.execute('SELECT state,generation FROM workflow_project WHERE id=1').fetchone()
            role = db.execute('SELECT pause_reason FROM workflow_sessions WHERE host_id=? AND session_id=? AND detached=0', (host, session)).fetchone()
            if not role or run['state'] not in {'running', 'paused'} or role['pause_reason'] in {'budget', 'native_stop', 'detached'}:
                raise ConflictError('Node completion cannot be confirmed in this control state')
            if run['generation'] != details['generation']:
                raise ConflictError('Node finish belongs to an earlier generation')
            event = db.execute("SELECT payload FROM requests WHERE request_id=? AND operation='runtime.execution_end'", (f'end:{session}:{terminal}',)).fetchone()
            if not event:
                raise ConflictError('A native terminal event is required')
            witness = json.loads(event['payload'])
            if (witness.get('host') != host or witness.get('session') != session or witness.get('outcome') != 'succeeded'
                    or witness.get('execution_token') != turn_token):
                raise ConflictError('The native terminal event does not confirm success')
            association = self._association(db, host, session)
            attempt = self._attempt(db, association['association_id'])
            if attempt['attempt_id'] != details['attempt_id']:
                raise ConflictError('Node finish belongs to a different work segment')
            result = self._finish_in_tx(db, host, session, details['state'], {'reason': details['summary']})
            db.execute("UPDATE exploration_tasks SET state='finished',updated_at=? WHERE task_id=? AND session_id=?",
                       (timestamp(), details['task_id'], session))
            if _allocations_available(db):
                db.execute("UPDATE exploration_allocations SET resource_state='uncertain',updated_at=? "
                           "WHERE task_id=? AND resource_state='held' AND resources!='{}'",
                           (timestamp(), details['task_id']))
            db.execute("UPDATE workflow_sessions SET pause_reason='segment_complete' WHERE session_id=?", (session,))
            db.execute("UPDATE workflow_intents SET state='completed',updated_at=? WHERE intent_id=?", (timestamp(), intent_id))
            self._event(db, 'runtime.node_finish_committed', {'intent_id': intent_id, 'task_id': details['task_id']})
            return {**result, 'task_id': details['task_id'], 'summary': details['summary']}
        return self._mutate('runtime.node_finish_commit', payload, operation, work)

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
        return self._association_context(db,host,session,association)

    def _association_context(self, db, host, session, association):
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

    def runtime_tool_observed(self, host, session, tool, call_id, operation):
        """Record only host-observed executable tool completion, without its input or output."""
        if tool not in {'bash', 'shell', 'python', 'terminal'} or not isinstance(call_id, str) or not call_id:
            raise ValidationError('An observed executable tool and call identity are required')
        payload = {'host':host, 'session':session, 'tool':tool, 'call_id':call_id}
        def work(db):
            role = db.execute('SELECT role FROM workflow_sessions WHERE host_id=? AND session_id=? AND detached=0',
                              (host, session)).fetchone()
            context = self._invocation_context(db, host, session)
            if not role or role['role'] != 'node_core' or not context['attempt_id']:
                return {'recorded':False}
            key = f'tool:{host}:{session}:{call_id}'
            details = {'tool':tool, 'status':'completed', 'generation':context['generation'], 'binding':'frozen'}
            db.execute('INSERT OR IGNORE INTO usage_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (str(uuid4()), key, context['association_id'], context['attempt_id'], context['node_id'],
                        'tool_execution', None, None, None, 'unknown', json.dumps(details), timestamp()))
            return {'recorded':True, 'source_key':key}
        return self._mutate('runtime.tool_observed', payload, operation, work)

    def runtime_budget_set(self, host, session, limits, reason, operation):
        if not isinstance(limits, dict) or set(limits)-LIMITS or not isinstance(reason,str) or not reason.strip():
            raise ValidationError('Explicit runtime limits and reason required')
        if any(value is not None and (type(value) not in ({int,float} if key in {'max_cost','shared_work_budget'} else {int})
                or not math.isfinite(value) or value<=0) for key,value in limits.items() if key != 'resource_capacities'):
            raise ValidationError('Runtime limits must be positive integers (cost/shared work may be fractional) or null (disabled)')
        if 'resource_capacities' in limits and limits['resource_capacities'] is not None:
            _resource_requirements(limits['resource_capacities'])
        payload = dict(host=host,session=session,limits=limits,reason=reason)
        def work(db):
            config = self._project(db)['config']
            before = config.get('runtime_budget', {})
            updated = {**before, **limits}
            config['runtime_budget'] = updated
            self._set_project_config(db, config)
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

    def runtime_source_owner(self, host, session):
        with self._read() as db:
            return source_owner(db,host,session)

    def runtime_deadline(self):
        with self._read() as db:
            row=db.execute('''SELECT p.state,b.deadline_at FROM workflow_project p
                LEFT JOIN runtime_budgets b ON b.generation=p.generation WHERE p.id=1''').fetchone()
            return dict(row) if row else {'state':None,'deadline_at':None}

    def runtime_reserve(self, host, session, operation, source_snapshot=None, wake_intent_id=None, launch_intent_id=None, specialist_launch_id=None, message_id=None):
        if message_id is not None and (not isinstance(message_id, str) or not message_id.startswith('msg_')):
            raise ValidationError('A valid host message ID is required')
        def work(db):
            # This operation is host-only. A specialist may be delegated by a
            # manually controlled node; ordinary autonomous turns require running.
            actor = db.execute('SELECT role,pause_reason FROM workflow_sessions WHERE host_id=? AND session_id=? AND detached=0',
                               (host, session)).fetchone()
            if actor and actor['role'] == 'node_core':
                unknown = db.execute("SELECT task_id FROM exploration_tasks WHERE session_id=? AND state='unverified' LIMIT 1", (session,)).fetchone()
                if unknown:
                    run = db.execute('SELECT state,control_revision FROM workflow_project WHERE id=1').fetchone()
                    raise ResearchExecutionUnverified(run['state'], run['control_revision'], unknown['task_id'],
                                                      actor['pause_reason'], controller_phase(db))
            if wake_intent_id:
                intent = db.execute("SELECT * FROM workflow_intents WHERE intent_id=? AND kind='notification_wake' AND session_id=?",
                                    (wake_intent_id, session)).fetchone()
                if not intent or intent['state'] != 'submitting' or json.loads(intent['details'])['host'] != host:
                    raise ConflictError('Coordinator wake is not authorized')
                notice = db.execute('SELECT * FROM workflow_notifications WHERE notification_id=?',
                                    (json.loads(intent['details'])['notification_id'],)).fetchone()
                if notice and notice['kind'] == 'exploration_result' and not self._exploration_notice_pending(db, notice):
                    raise ConflictError('Exploration result is no longer pending')
            if launch_intent_id:
                launch = db.execute("SELECT * FROM workflow_intents WHERE intent_id=? AND kind='node_launch'",
                                    (launch_intent_id,)).fetchone()
                details = json.loads(launch['details']) if launch else {}
                if (not launch or launch['state'] != 'ready' or details.get('host') != host
                        or details.get('session_id') != session):
                    raise ConflictError('Node launch is not authorized for this session')
            if specialist_launch_id:
                specialist = db.execute("SELECT * FROM workflow_intents WHERE intent_id=? AND kind='specialist_launch'", (specialist_launch_id,)).fetchone()
                specialist_details = json.loads(specialist['details']) if specialist else {}
                if not specialist or specialist['state'] != 'bound' or specialist_details.get('session_id') != session:
                    raise ConflictError('Specialist launch is not authorized for this session')
                require_new_work(db, host, specialist_details['parent'], manual=True)
            require_new_work(db, host, session, manual=bool(actor and actor['role'] == 'specialist'),
                             waiting=bool(wake_intent_id))
            run = db.execute('SELECT generation,state,control_revision FROM workflow_project WHERE id=1').fetchone()
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
            if message_id:
                authorization = dict(host=host, session=session, reservation_id=rid, message_id=message_id,
                                     generation=run['generation'], control_revision=run['control_revision'])
                db.execute('INSERT INTO workflow_intents VALUES(?,?,?,?,?,?)',
                           ('turn-authorization:' + rid, 'turn_authorization', session, 'issued', json.dumps(authorization, sort_keys=True), timestamp()))
            if launch_intent_id:
                details['reservation_id'] = rid
                db.execute("UPDATE workflow_intents SET state='sending',details=?,updated_at=? WHERE intent_id=?",
                           (json.dumps(details, sort_keys=True), timestamp(), launch_intent_id))
            if specialist_launch_id:
                specialist_details['reservation_id'] = rid
                db.execute("UPDATE workflow_intents SET state='sending',details=?,updated_at=? WHERE intent_id=?",
                           (json.dumps(specialist_details, sort_keys=True), timestamp(), specialist_launch_id))
            if state['scope_owner'].startswith('node:'):
                digest=source_digest(db,host,session,source_snapshot)
                self._event(db,'runtime.workspace_baseline',{'host_id':host,'session_id':session,'algorithm':SOURCE_ALGORITHM,
                    'digest':digest,'source_identity':source_identity(source_snapshot) if digest is not None else None})
            return {'allowed':True,'reservation_id':rid,**state}
        payload = dict(host=host, session=session)
        if wake_intent_id:
            payload['wake_intent_id'] = wake_intent_id
        if launch_intent_id:
            payload['launch_intent_id'] = launch_intent_id
        if specialist_launch_id:
            payload['specialist_launch_id'] = specialist_launch_id
        if message_id:
            payload['message_id'] = message_id
        return self._mutate('runtime.reserve',payload,operation,work)

    def runtime_budget_stop(self, host, session, operation):
        def work(db):
            state = view(self,db,host,session)
            if not state['expired'] or state['run_state'] != 'running':
                return {'stopped':False,**state}
            affected_tasks = list(db.execute("SELECT task_id,node_id,state FROM exploration_tasks "
                "WHERE state IN ('queued','starting','running','waiting','stopping')"))
            from .workflow_store import cancel_unstarted_exploration
            settled_tasks = cancel_unstarted_exploration(self, db, state['generation'], 'budget_exhausted')
            db.execute("UPDATE workflow_project SET state='paused' WHERE id=1")
            db.execute("UPDATE project SET control='paused'")
            db.execute("UPDATE exploration_tasks SET state='cancelled',error='wall_clock_budget: queued request cancelled',updated_at=? WHERE state='queued'",(timestamp(),))
            db.execute("UPDATE exploration_tasks SET state='unverified',error='wall_clock_budget: exit not yet verified',updated_at=? WHERE state IN ('starting','running','waiting','stopping')",(timestamp(),))
            db.execute("UPDATE specialist_tasks SET state='unverified',exit_verified=0 WHERE state IN ('starting','running')")
            db.execute("UPDATE workflow_intents SET state='revoked',updated_at=? WHERE kind IN ('execution_start','turn_authorization') AND state IN ('open','issued')", (timestamp(),))
            db.execute("UPDATE workflow_sessions SET pause_reason='budget' WHERE detached=0")
            for task in affected_tasks:
                if task['task_id'] in settled_tasks:
                    continue
                outcome = 'cancelled' if task['state'] == 'queued' else 'unverified'
                self.exploration_record_result(db, task['node_id'], f"task:{task['task_id']}:{outcome}",
                    outcome, None, {'task_id': task['task_id'], 'reason': 'budget_exhausted'})
            self._event(db,'runtime.budget_exhausted',state)
            return {'stopped':True,**state}
        return self._mutate('runtime.budget_stop',dict(host=host,session=session),operation,work)

    def runtime_turn_finished(self, host, session, turn, operation, source_snapshot=None):
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
            baseline = None
            baseline_identity = None
            for row in db.execute('SELECT kind,data FROM events WHERE event_id>? AND event_id<=?',(lower,upper)):
                data = json.loads(row['data'])
                kind = row['kind']
                if kind=='runtime.workspace_baseline' and data['host_id']==host and data['session_id']==session:
                    baseline=data['digest'] if data.get('algorithm')==SOURCE_ALGORITHM else None
                    baseline_identity=data.get('source_identity')
                elif kind == 'knowledge.recorded' and (data.get('node_id') == (mine[5:] if mine.startswith('node:') else None)):
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
                elif mine=='project' and kind=='exploration.decided':
                    meaningful |= data.get('actor')=={'host_id':host,'session_id':session} and bool(data.get('result_ids'))
                elif kind=='review.scope_changed':
                    meaningful |= data['actor'].get('host_id')==host and data['actor'].get('session_id')==session and bool(data['added'])
                elif kind == 'review.completed':
                    meaningful |= data.get('node_id') == (mine[5:] if mine.startswith('node:') else None)
                elif kind=='review.challenge_disposed' and data['actor']=={'host_id':host,'session_id':session} and data['kind']!='unresolved':
                    duplicates=db.execute('SELECT COUNT(*) FROM events WHERE kind=? AND data=?',(kind,row['data'])).fetchone()[0]
                    meaningful |= duplicates==1
                elif kind == 'knowledge.disposed':
                    kid = data['affected_version'].split('/')[1].split('@')[0]
                    target_owner = db.execute('SELECT node_id FROM knowledge_entries WHERE knowledge_id=?',(kid,)).fetchone()[0]
                    current = db.execute('SELECT kind,evidence_refs,replacement_ref,source_sequence FROM knowledge_dispositions WHERE disposition_id=?',(data['disposition_id'],)).fetchone()
                    prior = db.execute('''SELECT kind,evidence_refs,replacement_ref FROM knowledge_dispositions
                        WHERE change_id=? AND affected_version=? AND source_sequence<? ORDER BY source_sequence DESC LIMIT 1''',
                        (data['change_id'],data['affected_version'],current['source_sequence'])).fetchone()
                    meaningful |= target_owner == (mine[5:] if mine.startswith('node:') else None) and (
                        prior is None or tuple(current)[:3] != tuple(prior))
            workspace=source_digest(db,host,session,source_snapshot)
            source_changed=baseline!=workspace if baseline is not None and workspace is not None and baseline_identity==source_identity(source_snapshot) else None
            meaningful |= source_changed is True
            idle = 0 if meaningful else (previous['idle_turns'] if previous else 0)+1
            db.execute('INSERT INTO runtime_progress VALUES(?,?,?,?,?,?) ON CONFLICT(generation,host_id,session_id) DO UPDATE SET last_event_sequence=excluded.last_event_sequence,idle_turns=excluded.idle_turns,last_finished_turn=excluded.last_finished_turn',
                       (gen,host,session,upper,idle,turn))
            return {'idle_turns':idle,'meaningful':bool(meaningful),'source_workspace_changed':source_changed}
        return self._mutate('runtime.turn_finished',dict(host=host,session=session,turn=turn),operation,work)

    def runtime_execution_end(self, host, session, turn, outcome, operation, execution_token=None, event_seq=None):
        def work(db):
            if execution_token:
                intent_id = 'execution-terminal:' + hashlib.sha256((host + ':' + session + ':' + execution_token).encode()).hexdigest()
                if db.execute('SELECT 1 FROM workflow_intents WHERE intent_id=?', (intent_id,)).fetchone():
                    return {'turn': turn, 'claimed': False}
                db.execute('INSERT INTO workflow_intents VALUES(?,?,?,?,?,?)',
                           (intent_id, 'execution_terminal', session, 'settled', json.dumps({'host': host, 'session': session,
                            'execution_token': execution_token, 'turn': turn, 'outcome': outcome, 'event_seq': event_seq}, sort_keys=True), timestamp()))
                db.execute("UPDATE workflow_intents SET state='ended',updated_at=? WHERE intent_id=? AND kind='execution_start' AND state='open'",
                           (timestamp(), 'execution-start:' + execution_token))
                if outcome != 'succeeded':
                    db.execute("UPDATE workflow_intents SET state='cancelled',updated_at=? WHERE kind='node_finish' "
                               "AND session_id=? AND state='pending' AND json_extract(details,'$.turn_token')=?",
                               (timestamp(), session, execution_token))
            return {'turn':turn, 'claimed':True}
        payload = dict(host=host,session=session,turn=turn,outcome=outcome)
        if execution_token:
            payload['execution_token'] = execution_token
        if event_seq is not None:
            payload['event_seq'] = event_seq
        return self._mutate('runtime.execution_end',payload,operation,work)

    def runtime_submitted(self, host, session, reservation, operation):
        def work(db):
            row=db.execute('SELECT session_id,state FROM runtime_budget_reservations WHERE reservation_id=?',(reservation,)).fetchone()
            if not row or row[0]!=session:
                raise ValidationError('Reservation belongs to another session')
            if row['state'] == 'not_sent':
                raise ConflictError('Reservation was proven unsent and revoked')
            db.execute("UPDATE runtime_budget_reservations SET state='submitted' WHERE reservation_id=?",(reservation,))
            return {'submitted':True}
        return self._mutate('runtime.submitted',dict(host=host,session=session,reservation=reservation),operation,work)
