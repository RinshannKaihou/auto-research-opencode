"""R1 current-state read model. Callers own the SQLite read transaction."""
from __future__ import annotations

from collections import Counter
import hashlib
import json

from . import epistemic, frozen_refs, hints
from .errors import ValidationError, NotFoundError

POLICIES = ("memory-v2", "memory-r1-v1")
COLLECTIONS = ("critical_now", "findings", "completed_findings")
CHECKPOINT_CONTEXT_CHARS = 4000
# Standing exploration strategy for the main coordinator. Strategy prompts are
# not gates: single-path proposals are not rejected for lacking a second route,
# and execution-control instructions take precedence over these rules.
EXPLORATION_STRATEGY_VERSION = "exploration-strategy-v1"
EXPLORATION_STRATEGY = [
    "large_commit: before committing large effort, name the key unknown that would change that "
    "commitment and test it with a smaller runnable probe; you judge what counts as large from "
    "the task and budget.",
    "alternatives: for a selection or discovery choice, consider a substantively different "
    "alternative or record why one path suffices; with no scope yet, register the choice "
    "question as a scope first; delivery work may run directly.",
    "new_result: when a new result arrives, judge whether it changes the current choice, a "
    "material alternative or applicability; check registered challenges and counter-evidence "
    "first; ordinary results are not automatically counter-evidence.",
    "stop_check: before claiming a plateau or stopping, review the basis, untried options, "
    "residual uncertainty and the information value of more work; stopping with reasons is "
    "allowed.",
]
EXPLORATION_STRATEGY_CODES = [rule.split(":", 1)[0] for rule in EXPLORATION_STRATEGY]
# The brief is the always-deliverable strategy: one actionable sentence per
# trigger that survives without the route samples. Bare codes are observability
# only and are never the sole strategy a model receives.
EXPLORATION_STRATEGY_BRIEF = ("Probe decision-changing unknowns with a smaller step before large effort; "
                              "consider an alternative or state the single-path reason; judge each new "
                              "result against the current choice; before stopping check basis, untried "
                              "options and uncertainty.")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def read_model(store, db, *, node_id=None, decision_refs=None, final_ref=None):
    if decision_refs is None:
        declared = store._project(db)['config'].get('r2_review_scope')
        run = db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()
        if declared is not None and run and declared['generation']==run[0]:
            decision_refs = declared['refs']
    run = db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()
    if (node_id is None and run and db.execute('PRAGMA user_version').fetchone()[0] >= 13
            and store._project(db)['config'].get('exploration_policy') == 'exploration-v1'):
        from .exploration_store import _scopes
        current_scopes = _scopes(db, run['generation'])
        if current_scopes:
            route_refs = []
            for row in db.execute("SELECT r.data FROM exploration_routes r JOIN "
                "(SELECT route_id,MAX(revision) revision FROM exploration_routes WHERE scope_id IN (SELECT scope_id FROM exploration_scopes WHERE generation=?) GROUP BY route_id) x "
                "ON r.route_id=x.route_id AND r.revision=x.revision", (run['generation'],)):
                route = json.loads(row['data'])
                if route.get('disposition') in {'investigating', 'adopted'}:
                    route_refs.extend(route.get('evidence_refs', []))
            decision_refs = list(dict.fromkeys([*(decision_refs or []), *route_refs]))
    if decision_refs is not None and (not isinstance(decision_refs, list) or
            any(not isinstance(ref, str) or not ref.strip() for ref in decision_refs)):
        raise ValidationError("decision_refs must be a list of nonempty references")
    if final_ref is not None and not frozen_refs.parse(final_ref).get('publication_id'):
        raise ValidationError("final_ref must identify a publication or publication item")
    bound = epistemic.read_bound(db)
    records = [store._decode_knowledge(row, db) for row in db.execute(
        "SELECT e.kind,e.node_id,r.* FROM knowledge_entries e "
        "JOIN knowledge_revisions r USING(knowledge_id) ORDER BY r.rowid DESC")]
    by_ref = {row['ref']: row for row in records}
    latest = {}
    for row in records:
        latest.setdefault(row['knowledge_id'], row)
    scope = {}
    pub_scopes = {}

    def add(ref, reason):
        visited = ref in scope
        scope.setdefault(ref, set()).add(reason)
        if visited:
            return
        if ref.startswith('knowledge/'):
            if ref not in by_ref:
                raise NotFoundError(f'Unknown research reference: {ref}')
            return
        input_node = db.execute('SELECT node_id,question_ref FROM nodes WHERE node_id=?', (ref,)).fetchone()
        if input_node:
            if input_node['question_ref']:
                add(input_node['question_ref'], 'declared_node_input')
            for entry in latest.values():
                if entry['node_id'] == ref:
                    add(entry['ref'], 'declared_node_input')
            return
        parsed = frozen_refs.parse(ref)
        if frozen_refs.is_frozen(ref):
            resolution = frozen_refs.resolve(db, store.root, ref)
            if resolution['outcome'] == 'not_found' and resolution['reason']!='object_missing':
                raise NotFoundError(resolution['message'])
            if resolution['outcome'] in ('unsupported', 'ambiguous'):
                raise ValidationError(resolution['message'])
        pid = parsed.get('publication_id')
        if pid:
            if not db.execute('SELECT 1 FROM publications WHERE publication_id=?', (pid,)).fetchone():
                raise NotFoundError(f'Unknown research reference: {ref}')
            if parsed.get('item_id'):
                item = db.execute('SELECT knowledge_refs FROM publication_items WHERE publication_id=? AND item_id=?',
                                  (pid, parsed['item_id'])).fetchone()
                if item is None:
                    raise NotFoundError(f'Unknown research reference: {ref}')
                pinned = json.loads(item['knowledge_refs'])
            else:
                pinned = [f'knowledge/{r[0]}@{r[1]}' for r in db.execute(
                    'SELECT knowledge_id,revision FROM publication_knowledge WHERE publication_id=?', (pid,))]
            pub_scopes[ref] = (pid, pinned)
            for fixed in pinned:
                add(fixed, reason)
            return
        if parsed.get('snapshot_id'):
            return
        store._require_ref(db, ref)

    for ref in decision_refs or []:
        add(ref, 'decision_input')
    if final_ref is None:
        conclusion = store.conclusion_view(db)
        final_ref = conclusion['final_ref'] if conclusion else None
    if final_ref:
        add(final_ref, 'final')
    if node_id:
        node = db.execute('SELECT inputs,question_ref FROM nodes WHERE node_id=?', (node_id,)).fetchone()
        if node is None:
            raise NotFoundError(f'Unknown node: {node_id}')
        for ref in json.loads(node['inputs']) + ([node['question_ref']] if node['question_ref'] else []):
            add(ref, 'node_input')
        for edge in db.execute('SELECT input_refs FROM node_dependencies WHERE successor_node_id=?', (node_id,)):
            for ref in json.loads(edge['input_refs']):
                add(ref, 'dependency')
        for row in latest.values():
            if row['node_id'] == node_id:
                add(row['ref'], 'node_knowledge')
    # Only the materialised typed support graph propagates scientific dependence.
    queue = list(scope)
    while queue:
        ref = queue.pop()
        for edge in db.execute('SELECT used_ref FROM knowledge_support_edges WHERE user_ref=?', (ref,)):
            if edge[0] not in scope:
                add(edge[0], 'support_ancestor')
                queue.append(edge[0])

    finished = list(db.execute("SELECT node_id,MAX(ended_at) completed_at FROM attempts "
                               "WHERE state='finished' AND node_id IS NOT NULL GROUP BY node_id "
                               "ORDER BY completed_at DESC,node_id"))
    ordinary = {}
    for row in latest.values():
        if row['kind'] in ('claim', 'observation'):
            ordinary.setdefault(row['node_id'], []).append(row)
    completed = [{'node_id': row['node_id'], 'finding_count': len(ordinary.get(row['node_id'], [])),
                  'status': 'registered' if ordinary.get(row['node_id']) else '未登记发现',
                  'selected_refs': [v['ref'] for v in ordinary.get(row['node_id'], [])[:2]],
                  'expand': {'collection': 'findings', 'node_id': row['node_id']}}
                 for row in finished]
    selected = {}

    def select(row, reason):
        if row['kind'] == 'open_question':
            return
        value = selected.setdefault(row['ref'], {**row, 'selection_reasons': [],
                                               'scope_origins': sorted(scope.get(row['ref'], []))})
        if reason not in value['selection_reasons']:
            value['selection_reasons'].append(reason)

    for ref in scope:
        if ref in by_ref:
            select(by_ref[ref], 'explicit_input')
            current = latest[by_ref[ref]['knowledge_id']]
            select(current, 'current_version')
    # Round-robin by completed node, so one prolific node cannot occupy the first page.
    for slot in range(max((len(ordinary.get(node["node_id"], [])) for node in finished), default=0)):
        for node in finished:
            rows = ordinary.get(node['node_id'], [])
            if slot < len(rows):
                select(rows[slot], 'completed_node')
    for row in db.execute('SELECT knowledge_id,revision FROM publication_knowledge ORDER BY rowid DESC'):
        select(by_ref[f'knowledge/{row[0]}@{row[1]}'], 'publication_pinned')
        select(latest[row[0]], 'current_version')
    question = db.execute('SELECT question FROM nodes WHERE node_id=?', (node_id,)).fetchone() if node_id else None
    for row in store._knowledge_query(db, query=question[0] if question else store._project(db)['goal'], limit=24)['items']:
        select(row, 'question_terms')
    for row in latest.values():
        if row['status'] == 'disputed' or row['revision'] > 1 or row['kind'] == 'lesson' or row['node_id'] is None:
            select(row, 'disputed' if row['status'] == 'disputed' else 'revised' if row['revision'] > 1 else 'background')
    if node_id:
        for row in ordinary.get(node_id, []):
            select(row, 'node_knowledge')

    risks = []

    def risk(kind, target, details, expand):
        risks.append({'kind': kind, 'target': target, 'risk': details,
                      'relevance': 'related' if target in scope else 'unknown',
                      'relevance_reason': sorted(scope[target]) if target in scope else ['decision_scope_not_declared_for_target'],
                      'expand': expand})

    for impact in epistemic.valid_impacts(db, bound):
        disposition = epistemic.current_disposition(db, impact['change_id'], impact['affected_version'], bound)
        if disposition is None or disposition['kind'] == 'unresolved':
            risk('impact', impact['affected_version'], {
                'impact_id': impact['impact_id'], 'change_id': impact['change_id'],
                'scope_unconfirmed': epistemic.effective_scope(db, impact['change_id'], bound)['mode'] == 'unknown',
                'explanation': epistemic.explain_path(db, impact['affected_version'],
                    epistemic.change_roots(db, impact['change_id'], bound)),
            }, {'ref': impact['impact_id']})
    for edge in db.execute("SELECT e.user_ref,e.used_ref FROM knowledge_support_edges e "
                           "JOIN knowledge_revisions r ON ('knowledge/'||r.knowledge_id||'@'||r.revision)=e.used_ref "
                           "JOIN knowledge_revisions u ON ('knowledge/'||u.knowledge_id||'@'||u.revision)=e.user_ref "
                           "WHERE r.status='retracted' AND u.status<>'retracted' ORDER BY e.user_ref,e.used_ref"):
        risk('retracted_basis', edge[0], {'retracted_ref': edge[1]}, {'ref': edge[0]})
    # Field failures on obsolete, unreferenced revisions remain in history.
    # Explicit inputs, pins and their support ancestors remain current concerns.
    check_refs = epistemic.anchor_set(db) | set(scope)
    queue = list(check_refs)
    while queue:
        for edge in db.execute('SELECT used_ref FROM knowledge_support_edges WHERE user_ref=?', (queue.pop(),)):
            if edge[0] not in check_refs:
                check_refs.add(edge[0])
                queue.append(edge[0])
    for row in records:
        if row['ref'] in scope:
            reasons = epistemic.publication_reference_risk(db, row['ref'], bound)
            if reasons:
                risk('input_reference', row['ref'], {'reasons': reasons}, {'ref': row['ref']})
            uncovered = epistemic.uncovered_refs(db, row['ref'])
            if uncovered:
                risk('coverage', row['ref'], {'uncovered_refs': uncovered}, {'ref': row['ref']})
        residual = [value for value in epistemic.residual_use_risk(db, row['ref'], bound)
                    if value['reason'] == 'disposed_old_ref']
        if residual:
            risk('residual_use', row['ref'], {'reasons': residual}, {'ref': row['ref']})
        for check in row['field_checks'] if row['ref'] in check_refs else []:
            if check['result'] != 'consistent':
                risk('field_check', row['ref'], {key: check[key] for key in ('check_index','result','reason','input_ref')}, {'ref': row['ref']})
    publications = [(f"pub/{r[0]}", r[0], None) for r in db.execute("SELECT publication_id FROM publications WHERE status='complete' ORDER BY publication_id")
                    if f'pub/{r[0]}' not in pub_scopes and not any(v[0] == r[0] for v in pub_scopes.values())]
    publications += [(ref, pid, pinned) for ref, (pid, pinned) in pub_scopes.items()]
    for ref, pid, pinned in publications:
        check = epistemic.publication_check(db, pid, bound, declared_refs=pinned)
        status = check['result']['status']
        if status != 'no impact found' or not check['targets_complete']:
            risk('publication', ref, {'status': status, 'versions': check['result']['versions'],
                                     'targets_complete': check['targets_complete']}, {'ref': ref})
    if db.execute('PRAGMA user_version').fetchone()[0]>=11:
        from .review_lifecycle import unresolved_challenges
        for review in unresolved_challenges(db):
            risk('review_challenge',review['target_ref'],{'review_event':review['event_id'],
                'obligation_id':review['obligation_id'],'review_ref':json.loads(review['evidence_refs'])[0],
                'status':'unresolved'}, {'ref':review['target_ref']})
    for ref in scope:
        if frozen_refs.is_frozen(ref):
            resolution=frozen_refs.resolve(db,store.root,ref)
            if resolution['outcome']!='resolved':
                risk('material_unavailable',ref,{'status':'unavailable','reason':resolution['reason']},{'ref':ref})
    for row in selected.values():
        row['current_ref'] = latest[row['knowledge_id']]['ref']
        row['version_notices'] = epistemic.version_notices(db, row['ref'], bound)
        if row['current_ref'] != row['ref']:
            row['version_notices'].append({'notice':'newer_revision_exists','source':row['ref'],'current_ref':row['current_ref']})
        row['risk'] = [r['kind'] for r in risks if r['target'] == row['ref']]
    risks.sort(key=lambda v: (v['relevance'] != 'related', v['kind'], v['target'], encoded(v['risk'])))
    return {'critical_now': risks, 'findings': list(selected.values()),
            'completed_findings': completed, 'source_sequence': bound,
            'decision_scope': {ref: sorted(reasons) for ref, reasons in scope.items()}}


def page(store, db, collection, *, cursor=None, limit=50, node_id=None, kind=None,
         query=None, decision_refs=None, final_ref=None):
    if collection not in COLLECTIONS:
        raise ValidationError(f'Unsupported derived collection: {collection}')
    model = read_model(store, db, node_id=node_id, decision_refs=decision_refs, final_ref=final_ref)
    items = model[collection]
    if node_id and collection != 'critical_now':
        items = [v for v in items if v['node_id'] == node_id]
        if collection == 'findings':
            items.sort(key=lambda v: v['kind'] not in ('claim', 'observation'))
    kinds = sorted({v['kind'] for v in items if 'kind' in v})
    if kind:
        items = [v for v in items if v.get('kind') == kind]
    if query is not None and collection != 'findings':
        raise ValidationError('query is only supported by the findings collection')
    if query:
        items = [v for v in items if query.casefold() in v['statement'].casefold()]
    filters = dict(node_id=node_id, kind=kind, query=query, decision_refs=decision_refs, final_ref=final_ref)
    signature = digest({'collection': collection, 'project': store._project(db)['project_id'],
                        'filters': filters, 'items': items})
    after = 0
    if cursor is not None:
        if not isinstance(cursor, dict) or not isinstance(cursor.get('fingerprint'), str) or not isinstance(cursor.get('after'), int):
            raise ValidationError('Invalid derived collection cursor')
        if cursor['fingerprint'] != signature:
            raise ValidationError('read_view_changed: refresh the collection')
        after = cursor['after']
        if not isinstance(after, int) or after < 0:
            raise ValidationError('Invalid derived cursor position')
    limit = max(1, min(int(limit), 200))
    values = items[after:after + limit]
    end = after + len(values)
    return {'collection': collection, 'items': values, 'total': len(items), 'kinds': kinds,
            'has_more': end < len(items), 'cursor': {'fingerprint': signature, 'after': end} if end < len(items) else None,
            'source_sequence': model['source_sequence']}


def compact_finding(item):
    value = {key: item[key] for key in ('ref','current_ref','node_id','kind','status','risk','version_notices','selection_reasons','scope_origins')}
    value['expand'] = {'ref': item['ref']}
    value['truncated_fields'] = []
    for key, budget in (('statement', 200), ('scope', 180), ('conditions', 180)):
        source = item[key]
        text = source if isinstance(source, str) else encoded(source)
        if len(text) > budget:
            value[key] = {'preview': text[:budget], 'notice': '未显示完整，必须展开'}
            value['truncated_fields'].append(key)
        else:
            value[key] = {'status': '未记录'} if source is None or source == '' or source == {} or source == [] else source
    return value


def compact_risk(item):
    value = {**item, 'risk': {}, 'truncated_fields': []}
    for key, field in item['risk'].items():
        if len(encoded(field)) > 400:
            value['truncated_fields'].append(key)
        else:
            value['risk'][key] = field
    return value


ROUTE_SAMPLE_CAP = 4
ROUTE_PER_SCOPE_CAP = 2


def exploration_overview(store, db, generation):
    """Bounded multi-scope projection; complete objects remain in paged collections.

    Sample selection is deterministic and coverage-first: pending work and risks on
    invested routes, current choices, untried candidates and deferred/excluded routes
    with registered condition changes, allocated round-robin per scope so one scope
    cannot fill the global sample. It is a display order, not a quality ranking.
    """
    from .exploration_store import _scopes, _decode
    scopes = _scopes(db, generation)
    if not scopes:
        return None
    active = [scope for scope in scopes if scope.get('state', 'active') == 'active']
    ordered_scopes = [*active, *[item for item in scopes if item not in active]]
    routes = [_decode(row, 'route') for row in db.execute(
        "SELECT r.* FROM exploration_routes r JOIN (SELECT route_id,MAX(revision) revision "
        "FROM exploration_routes WHERE scope_id IN (SELECT scope_id FROM exploration_scopes WHERE generation=?) "
        "GROUP BY route_id) x ON r.route_id=x.route_id AND r.revision=x.revision ORDER BY r.route_id", (generation,))]
    pending_rows = list(db.execute(
        "SELECT result_id,scope_id,kind,source_ref,data FROM exploration_results WHERE handled_by_decision IS NULL "
        "AND scope_id IN (SELECT scope_id FROM exploration_scopes WHERE generation=?) ORDER BY result_id",
        (generation,)))
    pending = len(pending_rows)
    intents = list(db.execute("SELECT i.state,i.details,t.error FROM workflow_intents i LEFT JOIN exploration_tasks t "
        "ON t.task_id=json_extract(i.details,'$.task_id') WHERE i.kind='exploration_work' "
        "AND json_extract(i.details,'$.generation')=? AND (i.state IN ('pending','waiting_budget') "
        "OR i.state='materialized' AND (t.state IN ('queued','starting','running','waiting','stopping','unverified') "
        "OR t.task_id IS NULL)) ORDER BY i.rowid", (generation,)))
    live_states = {'queued', 'starting', 'running', 'waiting', 'stopping', 'unverified'}
    live_work = sum(row['state'] == 'materialized' for row in intents)
    decisions = []
    reactivation_refs = set()
    for scope in ordered_scopes:
        row = db.execute('SELECT decision_id,data FROM exploration_decisions WHERE scope_id=? ORDER BY rowid DESC LIMIT 1',
                         (scope['scope_id'],)).fetchone()
        if row:
            data = json.loads(row['data'])
            decisions.append({'scope_id': scope['scope_id'], 'decision_id': row['decision_id'],
                'action': data.get('action'), 'provisional': data.get('provisional', False),
                'expected_results_total': len(data.get('expected_results', [])),
                'missing_results_total': len(data.get('expected_missing', [])),
                'unacknowledged_missing_total': sum(not item.get('acknowledged_by_decision') for item in data.get('expected_missing', [])),
                'declared_missing_results_total': len(data.get('missing_results', [])),
                'revises_decision_id': data.get('revises_decision_id'),
                'considered_route_refs': list(dict.fromkeys(data.get('considered_route_refs', [])))[:6],
                **({'considered_route_refs_omitted': len(set(data.get('considered_route_refs', []))) - 6}
                   if len(set(data.get('considered_route_refs', []))) > 6 else {})})
    for row in db.execute("SELECT data FROM exploration_decisions WHERE generation=?", (generation,)):
        evidence = json.loads(row['data']).get('reactivation_evidence') or {}
        reactivation_refs.update(evidence.keys())
    # Pending results map back to routes through their recorded route ids, then
    # through the node bindings captured when the work was allocated.
    node_routes = {}
    for binding in db.execute("SELECT b.node_id,b.route_id FROM exploration_bindings b "
                              "JOIN exploration_scopes s ON s.scope_id=b.scope_id WHERE s.generation=?",
                              (generation,)):
        node_routes.setdefault(binding['node_id'], set()).add(binding['route_id'])
    pending_by_route = {}
    risk_results = []
    for row in pending_rows:
        data = json.loads(row['data'])
        route_ids = set(data.get('route_ids') or []) | node_routes.get(data.get('node_id'), set())
        for route_id in route_ids:
            pending_by_route.setdefault(route_id, []).append(row['result_id'])
        if row['kind'] in {'review_challenge', 'failed', 'cancelled', 'unverified', 'stopped'}:
            risk_results.append({'result_id': row['result_id'], 'scope_id': row['scope_id'],
                                 'kind': row['kind'], 'source_ref': row['source_ref'],
                                 'selection_reasons': ['registered_risk_pending']})
    scope_pending = {}
    for row in pending_rows:
        scope_pending[row['scope_id']] = scope_pending.get(row['scope_id'], 0) + 1
    latest_by_scope = {item['scope_id']: item for item in decisions}

    def route_reasons(route):
        reasons = []
        route_id = route['route_id']
        if route.get('disposition') in {'investigating', 'adopted'} and pending_by_route.get(route_id):
            reasons.append('pending_results_on_invested_route')
        if route.get('disposition') == 'adopted':
            reasons.append('current_choice')
        choice = latest_by_scope.get(route['scope_id'])
        if (choice and route['ref'] in choice['considered_route_refs']
                and choice.get('unacknowledged_missing_total')):
            reasons.append('missing_expected_result')
        if route.get('disposition') == 'untried':
            reasons.append('untried_candidate')
        if route.get('disposition') in {'deferred', 'excluded'}:
            reasons.append('reactivation_registered' if route['ref'] in reactivation_refs
                           else 'parked_route')
        return reasons or ['scope_coverage']

    priority = {'pending_results_on_invested_route': 0, 'current_choice': 1, 'missing_expected_result': 2,
                'untried_candidate': 3, 'reactivation_registered': 4, 'parked_route': 5, 'scope_coverage': 6}

    def route_rank(route):
        return (min(priority[reason] for reason in route_reasons(route)), route['route_id'])

    # Round-robin per scope: scopes with pending todos first, then other active
    # scopes, so a single scope cannot fill the global sample. A lone scope is
    # the whole sample, so its quota is the global cap.
    sample, sampled_ids = [], set()
    scope_order = sorted(ordered_scopes, key=lambda item: (scope_pending.get(item['scope_id'], 0) == 0,
                                                           item.get('state', 'active') != 'active',
                                                           item['scope_id']))
    scope_quota = ROUTE_SAMPLE_CAP if len(scope_order) == 1 else ROUTE_PER_SCOPE_CAP
    queues = {item['scope_id']: sorted([route for route in routes if route['scope_id'] == item['scope_id']],
                                       key=route_rank) for item in scope_order}
    per_scope = {item['scope_id']: 0 for item in scope_order}
    progressed = True
    while len(sample) < ROUTE_SAMPLE_CAP and progressed:
        progressed = False
        for scope in scope_order:
            if len(sample) >= ROUTE_SAMPLE_CAP:
                break
            queue = queues[scope['scope_id']]
            while queue and per_scope[scope['scope_id']] < scope_quota:
                route = queue.pop(0)
                if route['route_id'] in sampled_ids:
                    continue
                sampled_ids.add(route['route_id'])
                per_scope[scope['scope_id']] += 1
                sample.append(route)
                progressed = True
                break
    route_status_counts = dict(sorted(Counter(route.get('disposition', 'untried') for route in routes).items()))

    def route_entry(route):
        value = {key: route[key] for key in ('ref', 'scope_id', 'title', 'disposition')}
        # Sample titles are bounded; the full title lives in the paged route.
        if len(value['title']) > 100:
            value['title'] = value['title'][:100]
            value['title_truncated'] = True
        value['selection_reasons'] = route_reasons(route)
        if route.get('disposition') == 'untried':
            for key in ('critical_assumption', 'next_probe'):
                if route.get(key):
                    text = route[key] if isinstance(route[key], str) else encoded(route[key])
                    value[key] = text[:120] if len(text) > 120 else text
        if route.get('disposition') in {'deferred', 'excluded'} and route.get('reactivation_condition'):
            condition = route['reactivation_condition'] if isinstance(route['reactivation_condition'], str) \
                else encoded(route['reactivation_condition'])
            value['reactivation_condition'] = condition[:120]
        return value

    def scope_entry(item):
        scope_routes = [route for route in routes if route['scope_id'] == item['scope_id']]
        value = {'ref': item['ref'], 'scope_id': item['scope_id'], 'objective': item['objective'][:100],
                'state': item.get('state', 'active'), 'task_kind': item.get('task_kind'),
                'routes_total': len(scope_routes),
                'pending_results_total': scope_pending.get(item['scope_id'], 0),
                'adopted_route_refs': [route['ref'] for route in scope_routes
                                       if route.get('disposition') == 'adopted'],
                'selection_reasons': (['pending_todo_first'] if scope_pending.get(item['scope_id'])
                                      else ['active_scope'])}
        # Alternatives show as the explicit not-registered note only when no
        # considerable candidate remains; candidates themselves stay in the
        # route sample and the paged collection.
        if not [route for route in scope_routes if route.get('disposition') in {'untried', 'investigating'}]:
            value['notable_alternatives'] = {'status': 'not_registered',
                'note': 'No structured alternative is registered; register one or record the single-path reason.'}
        return value

    selected = active[-1] if active else scopes[-1]
    return {'scope_ref': selected['ref'], 'objective': selected['objective'],
        'scopes_total': len(scopes), 'active_scopes_total': len(active),
        'deferred_scopes_total': sum(item.get('state') == 'deferred' for item in scopes),
        'closed_scopes_total': sum(item.get('state') == 'closed' for item in scopes),
        'routes_total': len(routes), 'active_routes_total': sum(item.get('disposition') == 'investigating' for item in routes),
        'route_status_counts': route_status_counts,
        'untried_total': route_status_counts.get('untried', 0),
        'adopted_total': route_status_counts.get('adopted', 0),
        'parked_routes_total': route_status_counts.get('deferred', 0) + route_status_counts.get('excluded', 0),
        'pending_results_total': pending, 'pending_work_total': len(intents),
        'live_work_total': live_work,
        'live_work_note': 'investigating = registered investment; live work from task state',
        'waiting_budget_total': sum(row['state'] == 'waiting_budget' for row in intents),
        'waiting_resource_total': sum((row['error'] or '').startswith('waiting_resource:') for row in intents),
        'provisional_scopes_total': sum(item['provisional'] for item in decisions),
        'missing_results_total': sum(item['missing_results_total'] for item in decisions),
        'unacknowledged_missing_total': sum(item['unacknowledged_missing_total'] for item in decisions),
        'record_boundary': 'Registered records only; absent entries are unrecorded, not disproven.',
        'index_complete': False,
        'scopes_index': {'collection':'exploration_scopes', 'generation':generation},
        'routes_index': {'collection':'exploration_routes', 'generation':generation},
        'results_index': {'collection':'exploration_results', 'generation':generation},
        'work_index': {'collection':'exploration_intents', 'generation':generation},
        'scopes': [scope_entry(item) for item in ordered_scopes[:3]],
        'scopes_omitted': max(0, len(scopes) - 3),
        'routes': [route_entry(route) for route in sample],
        'routes_omitted': max(0, len(routes) - len(sample)),
        'result_sample': risk_results[:4],
        'results_omitted': max(0, pending - min(4, len(risk_results))),
        'latest_decisions': decisions[:3],
        'decisions_omitted': max(0, len(decisions) - 3),
        'sample_order_note': 'Deterministic coverage order, not a quality ranking.'}


def render_context(store, db, blocks, node_id, max_chars, *, decision_refs=None, final_ref=None):
    max_chars = max(500, min(int(max_chars), 12000))
    model = read_model(store, db, node_id=node_id, decision_refs=decision_refs, final_ref=final_ref)
    block_map = dict(blocks)
    project = dict(block_map['project'])
    if len(project['goal']) > 1000:
        project['goal'] = project['goal'][:1000]
        project['goal_truncated'] = True
        project['expand'] = 'research_query without filters returns the project goal'
    block_map['project'] = project
    expand_scope = {key: value for key, value in (
        ('node_id', node_id), ('decision_refs', decision_refs), ('final_ref', final_ref)
    ) if value is not None}
    views = {'identity': block_map['identity']}
    exploration_details = None
    exploration_meta = None
    exploration_selection = None
    if (block_map['identity']['role'] == 'main' and db.execute('PRAGMA user_version').fetchone()[0] >= 13
            and store._project(db)['config'].get('exploration_policy') == 'exploration-v1'):
        run = db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()
        overview = exploration_overview(store, db, run['generation']) if run else None
        # Tier-2 strategy: the actionable brief and the untried count travel
        # with the core totals independently of the route samples; full rules
        # and meta upgrade later only when the budget allows.
        strategy = {'brief': EXPLORATION_STRATEGY_BRIEF}
        if overview:
            views['exploration'] = {key: overview[key] for key in
                ('scope_ref', 'scopes_total', 'active_scopes_total', 'routes_total', 'pending_results_total',
                 'untried_total', 'provisional_scopes_total', 'missing_results_total',
                 'unacknowledged_missing_total')}
            views['exploration'].update({'strategy': strategy, 'details_omitted': True,
                'scopes': {'collection':'exploration_scopes'}, 'routes': {'collection':'exploration_routes'},
                'results': {'collection':'exploration_results'}, 'intents': {'collection':'exploration_intents'}})
            exploration_details = {'objective':overview['objective'][:120], 'scope_sample':overview['scopes'],
                'route_sample':overview['routes'], 'latest_decisions':overview['latest_decisions'],
                'pending_work_total':overview['pending_work_total'], 'waiting_budget_total':overview['waiting_budget_total'],
                'waiting_resource_total':overview['waiting_resource_total']}
            exploration_meta = {'route_status_counts':overview['route_status_counts'],
                'untried_total':overview['untried_total'], 'live_work_total':overview['live_work_total'],
                'live_work_note':'investigating = registered investment; live work from task state',
                'result_sample':overview['result_sample'],
                'omitted': {'scopes':overview['scopes_omitted'], 'routes':overview['routes_omitted'],
                            'results':overview['results_omitted'], 'decisions':overview['decisions_omitted']},
                'record_boundary':'Registered records only; absent entries are unrecorded, not disproven.',
                'sample_order_note':'Deterministic coverage order, not a quality ranking.'}
            exploration_selection = {'version': 'exploration-context-v2',
                'selection_reasons': {route['ref']: route['selection_reasons'] for route in overview['routes']},
                'omitted': {'scopes':overview['scopes_omitted'], 'routes':overview['routes_omitted'],
                            'results':overview['results_omitted'], 'decisions':overview['decisions_omitted']}}
            from .runtime_budget import exploration_budget_view
            budget = exploration_budget_view(store, db)
            if budget.get('available'):
                exploration_meta['budget'] = {key:budget[key] for key in
                    ('allocated', 'reserved', 'committed', 'limit', 'unit', 'actual_usage_status')}
                exploration_meta['resources'] = {'held_sample':dict(list(budget['resources_held'].items())[:3]),
                    'capacity_sample':dict(list(budget['resource_capacities'].items())[:3]),
                    'expand':{'collection':'exploration_budget'}, 'note':'Unverified external exit keeps resources held'}
            exploration_details['instruction'] = ('Read evidence with scope_id before a local choice. Process late results, '
                'explain provisional choices and missing results, and reassess a deferred route only with changed conditions. '
                'For project conclusion omit scope_id and cover every scope, including deferred/closed scopes. '
                'A shared probe binds multiple routes to one work allocation; failures are not refutations.')
        else:
            views['exploration'] = {'scopes_total': 0, 'active_scopes_total': 0, 'untried_total': 0,
                'strategy': strategy,
                'scopes': {'collection':'exploration_scopes'}, 'routes': {'collection':'exploration_routes'}}
            exploration_details = {'instruction': 'For a choice or discovery with a key unknown, create a scope and save possible routes; direct delivery need not invent alternatives.'}
            exploration_meta = None
            exploration_selection = {'version': 'exploration-context-v2', 'selection_reasons': {}, 'omitted': {}}
    for name in ('critical_now', 'findings'):
        views[name] = {'total': len(model[name]), 'items': [], 'index_complete': False,
                       'details_complete': False, 'expand_required': True,
                       'expand': {'collection': name, **expand_scope}}
    views['critical_now']['risk_index'] = []
    completed = model['completed_findings']
    views['findings']['completed_nodes'] = {
        'total': len(completed), 'items': completed[:8], 'shown': min(8, len(completed)),
        'has_more': len(completed) > 8, 'expand': {'collection': 'completed_findings'}}
    clerical = Counter(v['class'] for v in hints.structure_hints(
        db, limit=None, node_id=node_id, klass='clerical')['items'])
    if db.execute('PRAGMA user_version').fetchone()[0]>=11:
        from .review_lifecycle import summary
        views['review_backlog'] = summary(db,node_id)
    else:
        counts = dict(db.execute("SELECT trigger_kind,COUNT(*) FROM review_todos WHERE state='pending' "
                                 "AND (? IS NULL OR node_id=?) GROUP BY trigger_kind", (node_id, node_id)))
        views['review_backlog'] = {'total': sum(counts.values()), 'by_trigger_kind': {},
                                   'categories_omitted': len(counts),
                                   'expand': {'collection': 'review_todos', 'state': 'pending',
                                              **({'node_id': node_id} if node_id else {})}}
        for kind, count in sorted(counts.items()):
            views['review_backlog']['by_trigger_kind'][kind] = count
            if len('## review_backlog\n' + encoded(views['review_backlog'])) > 600:
                del views['review_backlog']['by_trigger_kind'][kind]
                break
            views['review_backlog']['categories_omitted'] -= 1
    views['clerical_index'] = {'by_class': dict(clerical),
                               'expand': {'collection': 'hints', 'kind': 'clerical',
                                          **({'node_id': node_id} if node_id else {})}}

    def body():
        return '\n\n'.join(f'## {key}\n{encoded(value)}' for key, value in views.items())

    # Shrink optional indexes first; never trim a fixed reference in place.
    if len(body()) > max_chars:
        views['findings']['completed_nodes']['items'] = []
        views['findings']['completed_nodes']['shown'] = 0
        views['findings']['completed_nodes']['has_more'] = bool(completed)
    if len(body()) > max_chars:
        del views['review_backlog'], views['clerical_index']
    if len(body()) > max_chars:
        for name in ('critical_now', 'findings'):
            views[name] = {'total': len(model[name]), 'details_complete': False,
                           'expand_required': True, 'expand': {'collection': name,
                            **expand_scope}}
        views['findings']['completed_nodes'] = {'total': len(completed),
                                               'expand': {'collection': 'completed_findings'}}
    if len(body()) > max_chars:
        del views['identity']
    # The exploration core degrades before the decision scope can fail: the
    # brief strategy, operative totals, untried count and the primary scope
    # and route entries stay; softer counts, the scope pointer and the
    # secondary result/work entries yield first. Only a still-overflowing body
    # falls back to bare trigger codes.
    if len(body()) > max_chars and 'exploration' in views:
        for key in ('scope_ref', 'provisional_scopes_total', 'missing_results_total',
                    'unacknowledged_missing_total', 'results', 'intents'):
            views['exploration'].pop(key, None)
    if len(body()) > max_chars and 'exploration' in views \
            and 'brief' in views['exploration'].get('strategy', {}):
        views['exploration']['strategy'] = {'codes': list(EXPLORATION_STRATEGY_CODES),
                                            'brief_omitted': True}
    if len(body()) > max_chars:
        raise ValidationError(f'Context budget too small for decision scope; requires at least {len(body())} characters')
    # Role/control and current task outrank degradable exploration samples:
    # fill them first so long route prose can never displace them.
    for key in ('control_instructions', 'role_instructions', 'attempt', 'task', 'project'):
        if block_map[key] is not None:
            views[key] = block_map[key]
            if len(body()) > max_chars:
                del views[key]
    if exploration_details is not None:
        views['exploration']['details'] = exploration_details
        views['exploration']['details_omitted'] = False
        if len(body()) > max_chars:
            del views['exploration']['details']
            views['exploration']['details_omitted'] = True
    # The full rules and projection metadata upgrade independently of whether
    # the route samples fitted: dropped details must not silence the strategy.
    if 'exploration' in views and 'brief' in views['exploration'].get('strategy', {}):
        upgraded = {'version': EXPLORATION_STRATEGY_VERSION, 'rules': list(EXPLORATION_STRATEGY),
                    'note': 'Prompts, not gates; execution-control instructions take precedence.'}
        brief = views['exploration']['strategy']
        views['exploration']['strategy'] = upgraded
        if len(body()) > max_chars:
            views['exploration']['strategy'] = brief
    if exploration_meta is not None:
        views['exploration']['meta'] = exploration_meta
        if len(body()) > max_chars:
            del views['exploration']['meta']
    remaining = max_chars - len(body())
    finding_budget = min(4000, remaining * 5 // 8) if model['critical_now'] else min(4000, remaining)
    risk_budget = min(2400, remaining - finding_budget) if model['findings'] else min(2400, remaining)
    # List stable risk identities before spending the remaining risk budget on details.
    if 'risk_index' in views['critical_now']:
        for item in model['critical_now']:
            index = {'kind': item['kind'], 'target': item['target'], 'ref': item['expand']['ref'],
                     'risk': {key: item['risk'][key] for key in
                              ('status', 'result', 'check_index', 'retracted_ref', 'scope_unconfirmed','review_event')
                              if key in item['risk']}}
            entries = views['critical_now']['risk_index']
            entries.append(index)
            if len(encoded(entries)) > risk_budget or len(body()) > max_chars:
                entries.pop()
                break
        risk_budget = max(0, risk_budget - len(encoded(views['critical_now']['risk_index'])))
    # Keep a complete recovery snapshot ahead of optional finding prose.
    for key in ('checkpoint_recovery','checkpoint'):
        if block_map[key] is not None:
            if key=='checkpoint' and len(encoded(block_map[key]))>CHECKPOINT_CONTEXT_CHARS:
                continue
            views[key]=block_map[key]
            if len(body())>max_chars:
                del views[key]
    findings = model['findings']
    groups = [
        [v for v in findings if 'explicit_input' in v['selection_reasons']],
        [v for v in findings if 'completed_node' in v['selection_reasons']],
        [v for v in findings if 'disputed' in v['selection_reasons']][:2],
        [v for v in findings if 'revised' in v['selection_reasons']][:2],
    ]
    # Interleave independent channels; history volume cannot replace ordinary findings.
    ordered, used_refs = [], set()
    for slot in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if slot < len(group) and group[slot]['ref'] not in used_refs:
                ordered.append(group[slot]); used_refs.add(group[slot]['ref'])
    ordered.extend(v for v in findings if v['ref'] not in used_refs)
    for name, candidates, budget in (
        ('critical_now', model['critical_now'], risk_budget), ('findings', ordered, finding_budget)
    ):
        if 'items' not in views[name]:
            continue
        for item in candidates:
            value = compact_finding(item) if name == 'findings' else compact_risk(item)
            if len(encoded(views[name]['items'] + [value])) > budget:
                if name == 'findings' and any(reason in item['selection_reasons'] for reason in ('explicit_input', 'completed_node')):
                    # Keep the first deferred priority ref; do not replace it with cheaper background.
                    views[name]['deferred_refs'] = [item['ref']]
                    if len(body()) > max_chars:
                        del views[name]['deferred_refs']
                    break
                continue
            views[name]['items'].append(value)
            if len(body()) > max_chars:
                views[name]['items'].pop()
                break
        views[name]['index_complete'] = len(views[name].get('risk_index', views[name]['items'])) == len(model[name])
        views[name]['details_complete'] = len(views[name]['items']) == len(model[name]) and not any(
            value['truncated_fields'] for value in views[name]['items'])
        views[name]['expand_required'] = not views[name]['details_complete']
    for key in ('questions', 'conclusion', 'frozen_context', 'specialist_task', 'method_guidance', 'recent_node_notes'):
        if block_map[key] is not None:
            views[key] = block_map[key]
            if len(body()) > max_chars:
                del views[key]
    text = body()
    shown = views['findings'].get('items', [])
    risk_shown = views['critical_now'].get('items', [])
    risk_index = views['critical_now'].get('risk_index', [])
    dependencies = {value['ref'] for value in shown}
    dependencies.update(views['findings'].get('deferred_refs', []))
    dependencies.update(value['target'] for value in risk_shown + risk_index)
    dependencies.update(value['risk']['retracted_ref'] for value in risk_shown if 'retracted_ref' in value['risk'])
    if 'checkpoint' in views:
        value = views['checkpoint']
        dependencies.add(f"checkpoint/{value['checkpoint_id']}@{value['revision']}")
    if 'method_guidance' in views:
        value = views['method_guidance']
        dependencies.add(f"guidance/{value['guidance_id']}@{value['content_hash']}")
    omitted = [name for name, value in blocks if value is not None and value != [] and value != {} and name not in views and name not in
               ('knowledge', 'pending_review', 'affected_knowledge', 'structure_hints', 'retracted_refs', 'index')]
    coverage = {name: {'source_complete': True, 'total': len(model[name]),
                      'shown': len(views[name].get('items', [])),
                      'index_shown': len(views[name].get('risk_index', views[name].get('items', []))),
                      'index_complete': views[name].get('index_complete', False),
                      'details_complete': views[name]['details_complete']}
                for name in ('critical_now', 'findings')}
    coverage['completed_findings'] = {'total': len(completed),
        'shown': views['findings']['completed_nodes']['shown'] if 'shown' in views['findings']['completed_nodes'] else 0,
        'source_complete': True, 'expand': {'collection': 'completed_findings'}}
    return {'text': text, 'policy_version': 'memory-r1-v1', 'context_mode': block_map['specialist_task']['context_mode'] if block_map['specialist_task'] else None,
            **({'exploration_context': 'exploration-context-v2', 'exploration_selection': exploration_selection}
               if exploration_details is not None else {}),
            'source_digest': hashlib.sha256(text.encode()).hexdigest(),
            'source_sequence': model['source_sequence'], 'dependencies': sorted(dependencies),
            'selection': {value['ref']: {'reasons': value['selection_reasons'], 'scope_origins': value['scope_origins']} for value in shown},
            'truncation': {'fields': {value['ref']: value['truncated_fields'] for value in shown if value['truncated_fields']},
                           'scope_params_omitted': False, 'project_goal': bool(project.get('goal_truncated')),
                           'unshown': {name: len(model[name]) - len(views[name].get('items', [])) for name in ('critical_now', 'findings')}},
            'coverage': coverage,
            'block_chars': {name: len(f'## {name}\n{encoded(value)}') for name, value in views.items()},
            'truncated': any(not coverage[name]['details_complete'] for name in ('critical_now', 'findings')) or bool(omitted) or bool(project.get('goal_truncated')),
            'omitted': omitted}


def context_metrics(view):
    """Count rendered text, separating clerical hints from publication risk."""
    import re
    matches = list(re.finditer(r'(?m)^## ([^\n]+)\n', view['text']))
    blocks = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(view['text'])
        text = view['text'][match.end():end].rstrip()
        blocks[match[1]] = json.loads(text)
    chars = {match[1]: len(view['text'][match.start():matches[index + 1].start() if index + 1 < len(matches) else len(view['text'])].rstrip()) for index, match in enumerate(matches)}
    if view['policy_version'] == 'memory-r1-v1':
        findings = blocks.get('findings', {}).get('items', [])
        critical = blocks.get('critical_now', {})
        routine = chars.get('review_backlog', 0) + chars.get('clerical_index', 0)
    else:
        knowledge = blocks.get('knowledge', {})
        groups = knowledge.values() if isinstance(knowledge, dict) and 'items' not in knowledge else [knowledge]
        findings = [row for group in groups if isinstance(group, dict) for row in group.get('items', [])]
        critical = {}
        clerical = [item for item in blocks.get('structure_hints', {}).get('items', [])
                    if item.get('class') in hints.CLASSES[:3]]
        routine = chars.get('pending_review', 0)
        if clerical:
            routine += len('## clerical_index\n' + encoded({'items': clerical}))
    return {'characters': len(view['text']), 'routine_characters': routine,
            'visible_refs': sorted({row['ref'] for row in findings if isinstance(row.get('ref'), str) and re.fullmatch(r'knowledge/[^@\s]+@\d+', row['ref'])}),
            'critical_total': critical.get('total'), 'critical_shown': len(critical.get('risk_index', critical.get('items', []))),
            'critical_details_shown': len(critical.get('items', [])),
            'critical_details_complete': critical.get('details_complete'), 'block_chars': chars}
