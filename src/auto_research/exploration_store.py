"""Versioned exploration scopes and routes, separate from execution state."""

import hashlib
import json
import re

from .controller import require_controller
from .errors import BudgetError, ConflictError, EvidenceBasisChanged, NotFoundError, ValidationError
from .runtime_budget import check_node, require_new_work
from .workflow_store import encoded, now


_REF = re.compile(r"explore/(scope|route)/(ES|ER)-([0-9]+)@([1-9][0-9]*)\Z")
_SCOPE_FIELDS = {"objective", "task_kind", "question_refs", "constraints", "comparison_basis",
                 "baseline_refs", "budget_policy", "parent_scope_ref", "state", "reactivation_condition"}
_ROUTE_FIELDS = {"title", "rationale", "critical_assumption", "hypothesis_refs", "baseline_refs",
                 "evidence_refs", "next_probe", "cost_estimate", "reactivation_condition", "relations", "component_refs", "candidate_key"}
_REF_FIELDS = {"question_refs", "baseline_refs", "hypothesis_refs", "evidence_refs"}


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be nonempty text")
    return value.strip()


def _parse_ref(ref, kind):
    match = _REF.fullmatch(ref) if isinstance(ref, str) else None
    if not match or match.group(1) != kind or match.group(2) != {"scope": "ES", "route": "ER"}[kind]:
        raise ValidationError(f"Expected a versioned exploration {kind} reference")
    return f"{match.group(2)}-{match.group(3)}", int(match.group(4))


def _decode(row, kind):
    value = dict(row)
    data = json.loads(value.pop("data"))
    value["actor"] = json.loads(value["actor"])
    value.update(data)
    value["ref"] = f"explore/{kind}/{value[kind + '_id']}@{value['revision']}"
    return value


def _main(db, host, session):
    row = db.execute("SELECT w.role,w.detached,p.main_session_id,p.generation,p.state FROM workflow_sessions w "
                     "CROSS JOIN workflow_project p WHERE w.host_id=? AND w.session_id=? AND p.id=1",
                     (host, session)).fetchone()
    if not row or row["detached"] or row["role"] != "main" or row["main_session_id"] != session:
        raise ValidationError("Only the current project main may manage exploration")
    return row


def _latest(db, kind, ident):
    table = {"scope": "exploration_scopes", "route": "exploration_routes"}[kind]
    key = kind + "_id"
    row = db.execute(f"SELECT * FROM {table} WHERE {key}=? ORDER BY revision DESC LIMIT 1", (ident,)).fetchone()
    if not row:
        raise NotFoundError(f"Unknown exploration {kind}: {ident}")
    return _decode(row, kind)


def _current_scope(db, generation):
    row = db.execute("SELECT * FROM exploration_scopes WHERE generation=? ORDER BY rowid DESC LIMIT 1",
                     (generation,)).fetchone()
    return _decode(row, "scope") if row else None


def _scopes(db, generation):
    """Latest revision of every scope, including closed and deferred scopes."""
    return [_decode(row, 'scope') for row in db.execute(
        'SELECT s.* FROM exploration_scopes s JOIN (SELECT scope_id,MAX(revision) revision '
        'FROM exploration_scopes WHERE generation=? GROUP BY scope_id) x '
        'ON s.scope_id=x.scope_id AND s.revision=x.revision ORDER BY s.scope_id', (generation,))]


def _work_basis(store, db, details):
    # An older fingerprint cannot retrospectively prove the state of an input
    # that was absent from its roots when the work was authorized.
    from .evidence_read import scope_work_basis
    mode = details.get('basis_mode')
    roots = details['roots']
    if mode == 'scope-work-v2':
        roots = sorted(set(roots) | set(details['work_input_refs']))
        return scope_work_basis(store, db, roots, details['scope_id'], details['route_refs'],
                                ignore_review_scope_events=True)
    if set(_work_input_references(details.get('next_work') or {})) - set(roots):
        return None
    if mode == 'scope-work-v1':
        # Development-only E3 round-1 format; schema <=15 never shipped this.
        return scope_work_basis(store, db, roots, details['scope_id'], details['route_refs'])
    return None


def _work_invalid_reason(store, db, details):
    if details.get('basis_mode') not in {'scope-work-v1', 'scope-work-v2'}:
        # Pre-E3 digests cover a different view layout. Do not claim the
        # evidence changed, or manufacture authorization with a new digest.
        return 'exploration_basis_format_upgraded'
    if _work_basis(store, db, details) != details['basis_digest']:
        return 'exploration_basis_changed'
    return None


def _work_input_references(work):
    """Return explicit fixed inputs, including inputs declared via predecessors."""
    refs = list(work.get('inputs') or [])
    for predecessor in work.get('predecessors') or []:
        refs.extend(predecessor.get('input_refs') or [])
    refs.extend(work[key] for key in ('anchor_ref', 'question_ref') if work.get(key))
    return sorted(set(refs))


def contract_versions(store, db, host, session):
    from . import __version__
    config = store._project(db)['config']
    row = db.execute('SELECT r.context_id,p.policy_version,r.selection FROM context_requests r '
                     'JOIN context_packs p USING(pack_id) WHERE r.host_id=? AND r.session_id=? '
                     'ORDER BY r.context_id DESC LIMIT 1', (host, session)).fetchone()
    last = None
    if row:
        selection = json.loads(row['selection'])
        last = {'context_id':row['context_id'], 'policy_version':row['policy_version'],
                'exploration_context':selection.get('exploration_context'), 'plugin_version':selection.get('plugin_version')}
    return {'context_policy':config.get('context_policy', 'memory-v2'),
            'exploration_policy':config.get('exploration_policy', 'off'),
            'conclusion_contract':config.get('conclusion_contract', 'legacy-v1'),
            'backend_version':__version__, 'last_recorded_context':last}


def _changes(store, db, fields, allowed):
    if not isinstance(fields, dict) or set(fields) - allowed:
        raise ValidationError("Unknown exploration fields")
    value = dict(fields)
    for key in _REF_FIELDS & value.keys():
        refs = value[key]
        if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
            raise ValidationError(f"{key} must be a list of fixed references")
        for ref in refs:
            store._require_ref(db, ref)
    for key in {"objective", "title", "rationale", "critical_assumption"} & value.keys():
        value[key] = _text(value[key], key)
    if "task_kind" in value and value["task_kind"] not in {"delivery", "selection", "discovery"}:
        raise ValidationError("task_kind must be delivery, selection or discovery")
    if 'state' in value and value['state'] not in {'active', 'deferred', 'closed'}:
        raise ValidationError('Scope state must be active, deferred or closed')
    if value.get('candidate_key') is not None:
        value['candidate_key'] = _text(value['candidate_key'], 'candidate_key')
    if 'component_refs' in value:
        if not isinstance(value['component_refs'], list) or len(set(value['component_refs'])) != len(value['component_refs']):
            raise ValidationError('component_refs must be distinct fixed route references')
        for ref in value['component_refs']:
            ident, revision = _parse_ref(ref, 'route')
            if not db.execute('SELECT 1 FROM exploration_routes WHERE route_id=? AND revision=?', (ident, revision)).fetchone():
                raise NotFoundError(f'Unknown component route: {ref}')
    if 'relations' in value:
        if not isinstance(value['relations'], list):
            raise ValidationError('relations must be a list')
        seen = set()
        for relation in value['relations']:
            if not isinstance(relation, dict) or relation.get('kind') not in {'competes_with', 'depends_on', 'complements', 'combines'}:
                raise ValidationError('Unknown route relationship')
            ident, revision = _parse_ref(relation.get('route_ref'), 'route')
            key = (relation['kind'], ident)
            if key in seen:
                raise ValidationError('Duplicate route relationship')
            seen.add(key)
            if not db.execute('SELECT 1 FROM exploration_routes WHERE route_id=? AND revision=?', (ident, revision)).fetchone():
                raise NotFoundError('Unknown related route revision')
    # Serialize here so invalid values fail before inserting a revision.
    encoded(value)
    return value


def _comparison_unknown(value):
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in {
            '', 'unknown', 'unspecified', 'not specified', 'unavailable', 'tbd', 'n/a',
            '未知', '未记录', '未提供', '未指定', '待定',
        }
    return isinstance(value, (list, dict)) and not value


def _comparisons(store, db, comparisons):
    """Check declared measurement compatibility, without choosing a winner."""
    if not isinstance(comparisons, list):
        raise ValidationError('comparisons must be a list')
    values = []
    from .frozen_refs import is_frozen
    from .memory_store import is_knowledge_ref
    keys = ('baseline_ref', 'workload', 'metric', 'unit', 'environment')
    for comparison in comparisons:
        if not isinstance(comparison, dict):
            raise ValidationError('Each comparison must be an object')
        refs, conditions = comparison.get('result_refs'), comparison.get('conditions')
        if not isinstance(refs, list) or len(refs) < 2 or len(set(refs)) != len(refs):
            raise ValidationError('Comparison needs at least two distinct fixed result references')
        if not isinstance(conditions, list) or len(conditions) != len(refs) or any(not isinstance(item, dict) for item in conditions):
            raise ValidationError('Comparison conditions must correspond to each result reference')
        for ref in refs:
            if not (is_frozen(ref) or is_knowledge_ref(ref)):
                raise ValidationError('Comparison results must be fixed publication, snapshot or knowledge revisions')
            store._require_ref(db, ref)
        differences = [key for key in keys if any(_comparison_unknown(item.get(key)) for item in conditions)
                       or any(item.get(key) != conditions[0].get(key) for item in conditions[1:])]
        for item in conditions:
            if not _comparison_unknown(item.get('baseline_ref')):
                if not (is_frozen(item['baseline_ref']) or is_knowledge_ref(item['baseline_ref'])):
                    raise ValidationError('Comparison baseline must be a fixed reference')
                store._require_ref(db, item['baseline_ref'])
            repeats = item.get('repeats')
            if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1 or _comparison_unknown(item.get('uncertainty')):
                differences.append('repeats_or_uncertainty')
        _text(comparison.get('reason'), 'comparison reason')
        ranking = comparison.get('ranking', False)
        if not isinstance(ranking, bool):
            raise ValidationError('Comparison ranking must be a boolean')
        if ranking and differences:
            raise ConflictError('incomparable_results: ' + ', '.join(sorted(set(differences))))
        values.append({**comparison, 'status':'incomparable' if differences else 'comparable',
                       'differences':sorted(set(differences))})
    return values


def settle_exploration_work(store, db, row, state, reason):
    """Settle unstarted work and create its durable coordinator result."""
    details = json.loads(row['details'])
    task_id = details.get('task_id')
    if task_id:
        task = db.execute('SELECT state FROM exploration_tasks WHERE task_id=?', (task_id,)).fetchone()
        if not task or task['state'] != 'queued':
            return None
        db.execute("UPDATE exploration_tasks SET state='cancelled',error=?,updated_at=? WHERE task_id=? AND state='queued'",
                   (reason, now(), task_id))
    details['settlement'] = {'state': state, 'reason': reason}
    db.execute('UPDATE workflow_intents SET state=?,details=?,updated_at=? WHERE intent_id=?',
               (state, encoded(details), now(), row['intent_id']))
    if db.execute('PRAGMA user_version').fetchone()[0] >= 16:
        from .runtime_budget import release_exploration_work
        release_exploration_work(store, db, row['intent_id'])
    route_ids = [_parse_ref(ref, 'route')[0] for ref in details['route_refs']]
    result_id = store._exploration_result_in_scope(db, details['scope_id'], f"intent:{row['intent_id']}:{state}",
        state, None, {'intent_id': row['intent_id'], 'task_id': task_id, 'node_id': details.get('node_id'),
                      'reason': reason, 'route_ids': route_ids})
    if db.execute('PRAGMA user_version').fetchone()[0] >= 15:
        recipient = db.execute('SELECT main_session_id FROM workflow_project WHERE id=1').fetchone()[0]
        notification_id = 'notice-' + hashlib.sha256(('exploration:' + result_id).encode()).hexdigest()[:24]
        body = {'kind':'exploration_result', 'result_id':result_id, 'intent_id':row['intent_id'],
                'scope_id':details['scope_id'], 'generation':details['generation'], 'reason':reason,
                'task_id':task_id, 'node_id':details.get('node_id')}
        db.execute('INSERT OR IGNORE INTO workflow_notifications VALUES(?,?,?,?,?,?,?,?)',
                   (notification_id, task_id, recipient, 'exploration_result', result_id, encoded(body), 'pending', now()))
    store._event(db, 'exploration.work_settled', {'intent_id':row['intent_id'], 'task_id':task_id,
                                                  'result_id':result_id, 'state':state, 'reason':reason})
    return {'intent_id':row['intent_id'], 'task_id':task_id, 'state':state, 'reason':reason, 'result_id':result_id}


def check_exploration_task(store, db, task_id):
    """Check and settle a queued exploration task under the launch transaction."""
    from .evidence_read import build_evidence_view
    if db.execute("PRAGMA user_version").fetchone()[0] < 13:
        return {"allowed": True, "exploration": False}
    row = db.execute("SELECT * FROM workflow_intents WHERE kind='exploration_work' AND state='materialized' "
                     "AND json_extract(details,'$.task_id')=?", (task_id,)).fetchone()
    if not row:
        return {"allowed": True, "exploration": False}
    details = json.loads(row["details"])
    if store._project(db)["config"].get("exploration_policy") != "exploration-v1":
        settle_exploration_work(store, db, row, 'cancelled', 'exploration_disabled')
        return {"allowed": False, "reason": "exploration_disabled", 'state':'cancelled'}
    reason = _work_invalid_reason(store, db, details)
    if reason:
        settle_exploration_work(store, db, row, 'stale', reason)
        return {"allowed": False, "reason": reason, 'state':'cancelled'}
    return {"allowed": True, "exploration": True}


class ExplorationStore:
    def exploration_policy(self, host, session, mode, operation):
        if mode is None:
            with self._read() as db:
                run = _main(db, host, session)
                config = self._project(db)["config"]
                return {"generation": run["generation"], "exploration_policy": config.get("exploration_policy", "off"),
                        "context_policy": config.get("context_policy", "memory-v2"),
                        "conclusion_contract": config.get("conclusion_contract", "legacy-v1")}
        if mode not in {"on", "off"}:
            raise ValidationError("exploration policy must be on or off")
        payload = {"host": host, "session": session, "mode": mode}

        def work(db):
            require_controller(db)
            run = _main(db, host, session)
            if run["state"] == "complete" and mode != 'off':
                raise ConflictError("Start a new generation before changing exploration")
            config = self._project(db)["config"]
            before = {"exploration_policy": config.get("exploration_policy", "off"),
                      "context_policy": config.get("context_policy", "memory-v2"),
                      "conclusion_contract": config.get("conclusion_contract", "legacy-v1")}
            config["exploration_policy"] = "exploration-v1" if mode == "on" else "off"
            if mode == "on":
                config["context_policy"] = "memory-r1-v1"
                if db.execute('PRAGMA user_version').fetchone()[0] >= 14:
                    config["conclusion_contract"] = "evidence-v1"
                    if db.execute('PRAGMA user_version').fetchone()[0] >= 15:
                        from .conclusion_policy import set_contract
                        set_contract(db, run['generation'], 'evidence-v1', 'exploration')
            self._set_project_config(db, config)
            if mode == 'off' and run['state'] != 'complete':
                rows = list(db.execute("SELECT * FROM workflow_intents WHERE kind='exploration_work' "
                    "AND state IN ('pending','waiting_budget','materialized') AND json_extract(details,'$.generation')=?",
                    (run['generation'],)))
                for row in rows:
                    settle_exploration_work(self, db, row, 'cancelled', 'exploration_disabled')
            after = {"exploration_policy": config["exploration_policy"],
                     "context_policy": config.get("context_policy", "memory-v2"),
                     "conclusion_contract": config.get("conclusion_contract", "legacy-v1")}
            if before != after:
                self._event(db, "exploration.policy_changed", {"generation": run["generation"], "before": before, "after": after})
            return {"generation": run["generation"], **after,
                    **({'effective_for': 'next_generation'} if run['state'] == 'complete' else {})}
        return self._mutate("exploration.policy", payload, operation, work)

    def exploration_scope(self, host, session, fields, operation):
        payload = {"host": host, "session": session, "fields": fields}

        def work(db):
            require_controller(db)
            run = _main(db, host, session)
            require_new_work(db, host, session, manual=True, waiting=True, role="main")
            if self._project(db)["config"].get("exploration_policy", "off") != "exploration-v1":
                raise ConflictError("Enable exploration before creating a scope")
            changes = _changes(self, db, fields.get("changes", {}), _SCOPE_FIELDS)
            ref = fields.get("scope_ref")
            old = None
            if ref:
                ident, expected = _parse_ref(ref, "scope")
                old = _latest(db, "scope", ident)
                if old["generation"] != run["generation"] or old["revision"] != expected:
                    raise ConflictError("exploration_scope_changed: refresh the current scope")
                _text(fields.get("reason"), "reason")
                data = {key: old.get(key) for key in _SCOPE_FIELDS}
                data.update(changes)
                revision = expected + 1
                goal_version = old["goal_version"] + int(any(old.get(k) != data.get(k) for k in
                    ("objective", "constraints", "comparison_basis")))
            else:
                ident = self._next(db, "exploration_scope", "ES")
                revision, goal_version = 1, 1
                data = {"question_refs": [], "constraints": None, "comparison_basis": None,
                        "baseline_refs": [], "budget_policy": None, "state": "active",
                        "parent_scope_ref": None, "reactivation_condition": None, **changes}
            data['state'] = data.get('state') or 'active'
            if data['state'] in {'deferred', 'closed'}:
                _text(fields.get('reason'), 'scope state reason')
                _text(data.get('reactivation_condition'), 'scope reactivation_condition')
            if old and old.get('state', 'active') != 'active' and data['state'] == 'active':
                _text(fields.get('reason'), 'scope reactivation reason')
                _text(fields.get('changed_conditions'), 'changed_conditions')
                data['changed_conditions'] = fields['changed_conditions']
            parent = data.get('parent_scope_ref')
            visited = {ident}
            while parent:
                parent_id, parent_rev = _parse_ref(parent, 'scope')
                if parent_id in visited:
                    raise ValidationError('Scope parent relationship would form a cycle')
                visited.add(parent_id)
                ancestor = _latest(db, 'scope', parent_id)
                if ancestor['generation'] != run['generation']:
                    raise ValidationError('Parent scope must belong to this generation')
                if not db.execute('SELECT 1 FROM exploration_scopes WHERE scope_id=? AND revision=?', (parent_id, parent_rev)).fetchone():
                    raise ValidationError('Unknown parent scope revision')
                parent = ancestor.get('parent_scope_ref')
            for key in ("objective", "task_kind"):
                if key not in data:
                    raise ValidationError(f"{key} is required")
            data["reason"] = fields.get("reason")
            actor = {"host_id": host, "session_id": session}
            db.execute("INSERT INTO exploration_scopes VALUES(?,?,?,?,?,?,?)",
                       (ident, revision, run["generation"], goal_version, encoded(data), encoded(actor), now()))
            value = _latest(db, "scope", ident)
            self._event(db, "exploration.scope_saved", {"ref": value["ref"], "actor": actor})
            if old and value['goal_version'] != old['goal_version']:
                self._exploration_result_in_scope(db, ident, 'scope:' + value['ref'], 'scope_changed',
                                                  value['ref'], {'before': old['ref'], 'after': value['ref'], 'route_ids': []})
            if old and value['state'] != old.get('state', 'active'):
                self._exploration_result_in_scope(db, ident, 'state:' + value['ref'], 'scope_state_changed',
                    value['ref'], {'before': old['ref'], 'after': value['ref'], 'route_ids': []})
            return value
        return self._mutate("exploration.scope", payload, operation, work)

    def exploration_route(self, host, session, fields, operation):
        payload = {"host": host, "session": session, "fields": fields}

        def work(db):
            require_controller(db)
            run = _main(db, host, session)
            require_new_work(db, host, session, manual=True, waiting=True, role="main")
            if self._project(db)["config"].get("exploration_policy", "off") != "exploration-v1":
                raise ConflictError("Enable exploration before editing a route")
            scope_id, revision = _parse_ref(fields.get("scope_ref"), "scope")
            scope = _latest(db, "scope", scope_id)
            if scope["generation"] != run["generation"] or scope["revision"] != revision:
                raise ConflictError("exploration_scope_changed: refresh the current scope")
            changes = _changes(self, db, fields.get("changes", {}), _ROUTE_FIELDS)
            if fields.get("route_ref"):
                ident, expected = _parse_ref(fields["route_ref"], "route")
                old = _latest(db, "route", ident)
                if old["scope_id"] != scope_id or old["revision"] != expected:
                    raise ConflictError("exploration_route_changed: refresh the current route")
                _text(fields.get("reason"), "reason")
                data = {key: old.get(key) for key in _ROUTE_FIELDS}
                data.update(changes)
                data["disposition"] = old["disposition"]
                data["disposition_reason"] = old.get("disposition_reason")
                revision = expected + 1
            else:
                ident, revision = self._next(db, "exploration_route", "ER"), 1
                data = {"hypothesis_refs": [], "baseline_refs": [], "evidence_refs": [],
                        "next_probe": None, "cost_estimate": None, "reactivation_condition": None,
                        "relations": [], "component_refs": [],
                        "disposition": "untried", "disposition_reason": None, **changes}
            related = [relation['route_ref'] for relation in (data.get('relations') or [])] + (data.get('component_refs') or [])
            if data.get('candidate_key'):
                duplicate = db.execute('SELECT r.route_id,r.revision FROM exploration_routes r JOIN '
                    '(SELECT route_id,MAX(revision) revision FROM exploration_routes WHERE scope_id=? GROUP BY route_id) x '
                    "ON r.route_id=x.route_id AND r.revision=x.revision WHERE r.route_id!=? AND json_extract(r.data,'$.candidate_key')=?",
                    (scope_id, ident, data['candidate_key'])).fetchone()
                if duplicate:
                    raise ConflictError(f"Candidate already registered as explore/route/{duplicate['route_id']}@{duplicate['revision']}; revise or reuse it")
            for related_ref in related:
                related_id, _ = _parse_ref(related_ref, 'route')
                if related_id == ident:
                    raise ValidationError('A route cannot reference itself as a component or relationship')
                related_scope = _latest(db, 'scope', _latest(db, 'route', related_id)['scope_id'])
                if related_scope['generation'] != run['generation']:
                    raise ValidationError('Route relationships must belong to this generation')
            for key in ("title", "rationale", "critical_assumption"):
                if key not in data:
                    raise ValidationError(f"{key} is required")
            data["reason"] = fields.get("reason")
            actor = {"host_id": host, "session_id": session}
            db.execute("INSERT INTO exploration_routes VALUES(?,?,?,?,?,?,?)",
                       (ident, revision, scope_id, scope["goal_version"], encoded(data), encoded(actor), now()))
            value = _latest(db, "route", ident)
            self._event(db, "exploration.route_saved", {"ref": value["ref"], "scope_ref": scope["ref"], "actor": actor})
            return value
        return self._mutate("exploration.route", payload, operation, work)

    def exploration_decide(self, host, session, fields, snapshot, operation):
        payload = {"host": host, "session": session, "fields": fields}

        def work(db):
            from .evidence_read import basis_changes, build_evidence_view, scope_work_basis
            require_controller(db)
            run = _main(db, host, session)
            require_new_work(db, host, session, manual=True, waiting=True, role="main")
            enabled = self._project(db)["config"].get("exploration_policy", "off") == "exploration-v1"
            if not enabled and (fields.get('next_work') is not None or fields.get('work_batch') or fields.get('action') not in {'defer', 'propose_stop'}):
                raise ConflictError('Exploration is disabled; only closing decisions are allowed')
            if not snapshot or snapshot["generation"] != run["generation"]:
                raise ConflictError("evidence_snapshot_expired: read the evidence again")
            scope_id, revision = _parse_ref(fields.get("scope_ref"), "scope")
            scope = _latest(db, "scope", scope_id)
            if scope["generation"] != run["generation"] or scope["revision"] != revision:
                raise ConflictError("exploration_scope_changed: refresh the current scope")
            selected_scope = snapshot['view'].get('scope_id')
            if selected_scope not in {None, scope_id} or (selected_scope is None and len(_scopes(db, run['generation'])) > 1):
                raise ConflictError('Read evidence with this scope_id before choosing work in a multi-scope project')
            current = build_evidence_view(self, db, snapshot["view"]["roots"], scope_id=selected_scope)
            if current["basis_digest"] != snapshot["basis_digest"]:
                raise EvidenceBasisChanged('exploration', basis_changes(snapshot['view'], current['view']))
            route_refs = fields.get("route_refs")
            if not isinstance(route_refs, list) or len(route_refs) != len(set(route_refs)):
                raise ValidationError("route_refs must be a list of distinct current route references")
            routes = {}
            for ref in route_refs:
                route_id, route_rev = _parse_ref(ref, "route")
                route = _latest(db, "route", route_id)
                if route["scope_id"] != scope_id or route["revision"] != route_rev:
                    raise ConflictError("exploration_route_changed: refresh the current route")
                if route["goal_version"] != scope["goal_version"]:
                    raise ConflictError("exploration_goal_changed: revalidate the route for the current objective")
                routes[ref] = route
            result_ids = fields.get("result_ids", [])
            if not isinstance(result_ids, list) or len(result_ids) != len(set(result_ids)):
                raise ValidationError("result_ids must be a list of distinct pending results")
            for result_id in result_ids:
                row = db.execute("SELECT * FROM exploration_results WHERE result_id=? AND scope_id=?", (result_id, scope_id)).fetchone()
                if not row or row["handled_by_decision"]:
                    raise ConflictError("exploration_result_changed: refresh pending results")
            action = fields.get("action")
            if action not in {"probe", "deepen", "redirect", "defer", "recommend_combine", "combine", "restore", "propose_stop"}:
                raise ValidationError("Unknown exploration action")
            reason = _text(fields.get("reason"), "reason")
            next_work = fields.get("next_work")
            batch = fields.get('work_batch')
            if batch is not None and (next_work is not None or not isinstance(batch, list) or not batch or len(batch) > 100):
                raise ValidationError('Use next_work or a nonempty work_batch of at most 100 work items')
            works = batch if batch is not None else ([next_work] if next_work is not None else [])
            if works and scope.get('state', 'active') != 'active':
                raise ConflictError('Reactivate this scope before allocating new work')
            keys = set()
            work_input_refs = []
            for next_work in works:
                if any(item['reason'] == 'evidence_traversal_limit' for item in snapshot['view']['limitations']):
                    raise ConflictError('Evidence traversal was truncated; narrow the scope before scheduling exploration')
                if not isinstance(next_work, dict):
                    raise ValidationError("next_work must be an object")
                key = next_work.get('key')
                if batch is not None:
                    _text(key, 'work_batch.key')
                    if key in keys:
                        raise ValidationError('work_batch keys must be distinct; share one work across routes')
                    keys.add(key)
                for key in ("question", "why_now", "plan"):
                    _text(next_work.get(key), f"next_work.{key}")
                selected = next_work.get("route_refs")
                if not isinstance(selected, list) or not selected or len(set(selected)) != len(selected) or any(ref not in routes for ref in selected):
                    raise ValidationError("next_work.route_refs must identify considered routes")
                if not next_work.get("root_reason") and not next_work.get("predecessors"):
                    raise ValidationError("next_work needs root_reason or predecessors")
                if action in {"defer", "propose_stop"}:
                    raise ValidationError("A deferred or stopped decision cannot schedule work")
                from .native_store import normalize_proposal
                proposal = normalize_proposal(next_work['question'], next_work['why_now'], next_work['plan'],
                    next_work.get('inputs'), next_work.get('work_kind', 'probe'), next_work.get('strategy', 'continue'),
                    next_work.get('anchor_ref'), next_work.get('question_ref'), next_work.get('root_reason'),
                    next_work.get('predecessors'), enforce_protocol=True)
                self.validate_proposal_references(db, proposal)
                work_input_refs.append(_work_input_references(proposal))
                if next_work.get('work_kind') in {'combine', 'restore'}:
                    from .frozen_refs import is_frozen
                    if not proposal['inputs'] or not all(is_frozen(ref) for ref in proposal['inputs']):
                        raise ValidationError('Combination and restoration require fixed published inputs')
                    if next_work['work_kind'] == 'combine' and not any(
                        len(routes[ref].get('component_refs') or []) >= 2 for ref in selected):
                        raise ValidationError('Combination work requires a route with at least two fixed component refs')
            next_work = fields.get('next_work')
            expected = fields.get('expected_results', [])
            if not isinstance(expected, list) or len(set(expected)) != len(expected):
                raise ValidationError('expected_results must name distinct existing work intent IDs')
            previous = db.execute('SELECT data FROM exploration_decisions WHERE scope_id=? AND generation=? '
                                  'ORDER BY rowid DESC LIMIT 1', (scope_id, run['generation'])).fetchone()
            previous = json.loads(previous['data']) if previous else {}
            expected = list(expected)
            previous_missing = {item['intent_id']: item for item in previous.get('expected_missing', [])
                                if isinstance(item, dict) and item.get('intent_id')}
            for item in previous_missing.values():
                ident = item.get('intent_id')
                if ident and ident not in expected:
                    expected.append(ident)

            def expected_status():
                received, missing = [], []
                for expected_id in expected:
                    row = db.execute("SELECT * FROM workflow_intents WHERE kind='exploration_work' AND intent_id=?", (expected_id,)).fetchone()
                    details = json.loads(row['details']) if row else {}
                    if not row or details.get('scope_id') != scope_id or details.get('generation') != run['generation']:
                        raise ValidationError('Expected work must belong to this scope and generation')
                    task_id = details.get('task_id')
                    task = db.execute('SELECT state FROM exploration_tasks WHERE task_id=?', (task_id,)).fetchone() if task_id else None
                    state = task['state'] if task else row['state']
                    facts = [dict(item) for item in db.execute('SELECT result_id,kind,source_ref FROM exploration_results '
                        "WHERE scope_id=? AND (json_extract(data,'$.node_id')=? OR json_extract(data,'$.intent_id')=?) ORDER BY result_id",
                        (scope_id, details.get('node_id'), expected_id))]
                    received.append({'intent_id':expected_id, 'state':state, 'intent_state':row['state'], 'results':facts})
                    if not any(item['kind'] == 'published' for item in facts):
                        prior = previous_missing.get(expected_id, {})
                        acknowledgment = {key: prior[key] for key in ('acknowledged_by_decision', 'acknowledgement_reason')
                                          if key in prior}
                        missing.append({'intent_id':expected_id, 'state':state, **acknowledgment,
                                        'reason':'No published result; execution state is not scientific refutation'})
                return received, missing

            received, missing = expected_status()
            missing_results = fields.get('missing_results', previous.get('missing_results', []))
            if not isinstance(missing_results, list):
                raise ValidationError('missing_results must be a list')
            provisional = fields.get('provisional', False)
            if not isinstance(provisional, bool):
                raise ValidationError('provisional must be a boolean')
            if (any(not item.get('acknowledged_by_decision') for item in missing)
                    and not provisional and action not in {'defer', 'propose_stop'}):
                raise ConflictError('Expected evidence is missing; make an explicit provisional choice or defer')
            revises = fields.get('revises_decision_id')
            if revises and not db.execute('SELECT 1 FROM exploration_decisions WHERE decision_id=? AND scope_id=? AND generation=?',
                                          (revises, scope_id, run['generation'])).fetchone():
                raise ValidationError('A revised decision must belong to this scope and generation')
            comparisons = _comparisons(self, db, fields.get('comparisons', []))
            supersede = fields.get('supersede_intent_ids', [])
            if not isinstance(supersede, list) or any(not isinstance(item, str) for item in supersede):
                raise ValidationError('supersede_intent_ids must be a list')
            for intent_id in supersede:
                row = db.execute("SELECT * FROM workflow_intents WHERE kind='exploration_work' AND intent_id=?", (intent_id,)).fetchone()
                details = json.loads(row['details']) if row else {}
                if not row or details.get('scope_id') != scope_id or row['state'] not in {'pending', 'waiting_budget'}:
                    raise ConflictError('Only an unstarted intent in this scope can be superseded')
                settled = settle_exploration_work(self, db, row, 'superseded', 'superseded_by_decision')
                # This very decision explicitly considered the supersession. Keep
                # its fact and notice without creating an unhandled self-result.
                if settled:
                    result_ids = [*result_ids, settled['result_id']]
            if supersede:
                received, missing = expected_status()
            actor = {"host_id": host, "session_id": session}
            decision_id = self._next(db, "exploration_decision", "ED")
            if action in {'defer', 'propose_stop'}:
                for item in missing:
                    item.setdefault('acknowledged_by_decision', decision_id)
                    item.setdefault('acknowledgement_reason', reason)
            current_routes = {ref: route for ref, route in routes.items()}
            route_changes = fields.get("route_changes", [])
            if not isinstance(route_changes, list):
                raise ValidationError("route_changes must be a list")
            for change in route_changes:
                ref = change.get("route_ref") if isinstance(change, dict) else None
                if ref not in current_routes:
                    raise ValidationError("Changed routes must be considered by this decision")
                old = current_routes.pop(ref)
                disposition = change.get("disposition")
                if disposition not in {"untried", "investigating", "deferred", "excluded", "adopted", "combination_proposed"}:
                    raise ValidationError("Unknown route disposition")
                change_reason = _text(change.get("reason"), "route change reason")
                condition = change.get("reactivation_condition", old.get("reactivation_condition"))
                if disposition in {"deferred", "excluded"}:
                    _text(condition, "reactivation_condition")
                if old['disposition'] in {'deferred', 'excluded'} and disposition not in {'deferred', 'excluded'}:
                    evidence = (fields.get('reactivation_evidence') or {}).get(ref) or {}
                    _text(evidence.get('changed_conditions'), 'reactivation changed_conditions')
                    reactivation_refs = evidence.get('evidence_refs')
                    if not isinstance(reactivation_refs, list) or not reactivation_refs:
                        raise ValidationError('Reactivation needs fixed evidence for the changed conditions')
                    for evidence_ref in reactivation_refs:
                        self._require_ref(db, evidence_ref)
                refs = change.get("evidence_refs", old.get("evidence_refs", []))
                _changes(self, db, {"evidence_refs": refs}, _ROUTE_FIELDS)
                data = {key: old.get(key) for key in _ROUTE_FIELDS}
                data.update(disposition=disposition, disposition_reason=change_reason,
                            reactivation_condition=condition, evidence_refs=refs, reason=change_reason)
                db.execute("INSERT INTO exploration_routes VALUES(?,?,?,?,?,?,?)", (old["route_id"], old["revision"] + 1,
                    scope_id, scope["goal_version"], encoded(data), encoded(actor), now()))
                current_routes[ref] = _latest(db, "route", old["route_id"])
            for selected_work in works:
                for ref in selected_work['route_refs']:
                    route = current_routes[ref]
                    if route['disposition'] in {'deferred', 'excluded'}:
                        raise ConflictError('Reactivating a deferred or excluded route requires an explicit route change')
                    if route['disposition'] == 'untried':
                        data = {key: route.get(key) for key in _ROUTE_FIELDS}
                        data.update(disposition='investigating', disposition_reason=reason, reason=reason)
                        db.execute('INSERT INTO exploration_routes VALUES(?,?,?,?,?,?,?)',
                            (route['route_id'], route['revision'] + 1, scope_id, scope['goal_version'], encoded(data), encoded(actor), now()))
                        current_routes[ref] = _latest(db, 'route', route['route_id'])
            data = {"scope_ref": scope["ref"], "considered_route_refs": route_refs,
                    "basis_digest": snapshot["basis_digest"], "source_sequence": snapshot["source_sequence"],
                    "versions": contract_versions(self, db, host, session),
                    "result_ids": result_ids, "missing_results": missing_results,
                    "action": action, "reason": reason, "route_changes": route_changes,
                    "remaining_options": fields.get("remaining_options", []), "next_work": next_work,
                    "work_batch": batch, "expected_results": expected, "received_results": received,
                    "expected_missing": missing, "provisional": provisional, "revises_decision_id": revises,
                    "comparisons": comparisons, "reactivation_evidence": fields.get('reactivation_evidence'),
                    "supersede_intent_ids": supersede}
            db.execute("INSERT INTO exploration_decisions VALUES(?,?,?,?,?,?,?)",
                       (decision_id, scope_id, scope["revision"], run["generation"], encoded(data), encoded(actor), now()))
            for result_id in result_ids:
                db.execute("UPDATE exploration_results SET handled_by_decision=? WHERE result_id=?", (decision_id, result_id))
            intent_ids = []
            if works:
                # All route revisions and result acknowledgements above are
                # already final for this transaction. Reconcile once per batch.
                from .review_lifecycle import reconcile
                reconcile(self, db, operation)
            work_bases = {}
            for index, selected_work in enumerate(works):
                intent_id = 'exploration-work:' + decision_id + (f':{index + 1}' if batch is not None else '')
                selected_refs = [current_routes[ref]['ref'] for ref in selected_work['route_refs']]
                work_roots = sorted(set(snapshot['view']['roots']) | set(work_input_refs[index]))
                basis_key = (tuple(work_roots), tuple(sorted(selected_refs)))
                if basis_key not in work_bases:
                    work_bases[basis_key] = scope_work_basis(self, db, work_roots, scope_id, selected_refs,
                                                           ignore_review_scope_events=True)
                post_digest = work_bases[basis_key]
                details = {"scope_id": scope_id, "scope_ref": scope["ref"], "generation": run["generation"],
                           "decision_id": decision_id, "basis_digest": post_digest, "basis_mode": "scope-work-v2",
                           "roots": snapshot["view"]["roots"], "next_work": selected_work,
                           "work_input_refs": work_input_refs[index], "route_refs": selected_refs}
                if selected_work.get('key'):
                    duplicate = db.execute("SELECT intent_id FROM workflow_intents WHERE kind='exploration_work' "
                        "AND json_extract(details,'$.generation')=? AND json_extract(details,'$.scope_id')=? "
                        "AND json_extract(details,'$.next_work.key')=?", (run['generation'], scope_id, selected_work['key'])).fetchone()
                    if duplicate:
                        raise ConflictError(f"Work key already allocated to {duplicate['intent_id']}; reuse its fixed result or choose a new attempt key")
                db.execute("INSERT INTO workflow_intents VALUES(?,?,?,?,?,?)",
                           (intent_id, "exploration_work", session, "pending", encoded(details), now()))
                if db.execute('PRAGMA user_version').fetchone()[0] >= 16:
                    from .runtime_budget import reserve_exploration_work
                    reserve_exploration_work(self, db, intent_id, details)
                intent_ids.append(intent_id)
            intent_id = intent_ids[0] if intent_ids else None
            self._event(db, "exploration.decided", {"decision_id": decision_id, "scope_ref": scope["ref"],
                                                     "result_ids": result_ids, "intent_id": intent_id, "actor": actor})
            return {"decision_ref": f"explore/decision/{decision_id}", "decision_id": decision_id,
                    "route_refs": [current_routes[ref]["ref"] for ref in route_refs], "intent_id": intent_id,
                    "intent_ids": intent_ids, "expected_missing": missing, "comparisons": comparisons}
        return self._mutate("exploration.decide", payload, operation, work)

    def exploration_materialize(self, host, session, intent_id, operation):
        payload = {"host": host, "session": session, "intent_id": intent_id}

        def work(db):
            from .evidence_read import build_evidence_view
            require_controller(db)
            run = _main(db, host, session)
            row = db.execute("SELECT * FROM workflow_intents WHERE kind='exploration_work' AND intent_id=?", (intent_id,)).fetchone()
            if not row:
                raise NotFoundError("Unknown exploration intent")
            details = json.loads(row["details"])
            if details["generation"] != run["generation"]:
                raise ConflictError("Exploration intent belongs to another generation")
            if row["state"] == "materialized":
                return {"intent_id": intent_id, "state": row["state"], "task_id": details["task_id"], "node_id": details["node_id"]}
            if row["state"] not in {"pending", "waiting_budget"}:
                raise ConflictError("Exploration intent is not pending")
            require_new_work(db, host, session, role="main", waiting=True)
            if self._project(db)["config"].get("exploration_policy") != "exploration-v1":
                return settle_exploration_work(self, db, row, 'cancelled', 'exploration_disabled')
            reason = _work_invalid_reason(self, db, details)
            if reason:
                return settle_exploration_work(self, db, row, 'stale', reason)
            try:
                check_node(self, db)
            except BudgetError as error:
                changed = row['state'] != 'waiting_budget'
                if changed:
                    db.execute("UPDATE workflow_intents SET state='waiting_budget',updated_at=? WHERE intent_id=?", (now(), intent_id))
                return {"intent_id": intent_id, "state": "waiting_budget", "reason": str(error), 'notify_waiting': changed}
            work = details["next_work"]
            db.execute('SAVEPOINT exploration_propose')
            try:
                node = self.propose(work["question"], work["why_now"], work["plan"], intent_id + ":node",
                    inputs=work.get("inputs"), purpose=work.get("work_kind", "probe"), strategy=work.get("strategy", "continue"),
                    anchor_ref=work.get('anchor_ref'), question_ref=work.get('question_ref'),
                    root_reason=work.get("root_reason"), predecessors=work.get("predecessors"), enforce_protocol=True,
                    asserted_at={"host_id": host, "session_id": session, "turn": None, "operation_id": operation},
                    control_actor=(host, session), _db=db)
            except (ValidationError, NotFoundError) as error:
                db.execute('ROLLBACK TO exploration_propose')
                db.execute('RELEASE exploration_propose')
                return settle_exploration_work(self, db, row, 'failed', str(error))
            db.execute('RELEASE exploration_propose')
            task = self.workflow(host, session, "task", {"node_id": node["node_id"]}, intent_id + ":task", _db=db)
            for ref in details["route_refs"]:
                route_id, route_rev = _parse_ref(ref, "route")
                db.execute("INSERT INTO exploration_bindings VALUES(?,?,?,?,?,?,?)",
                           (details["decision_id"], route_id, route_rev, details["scope_id"],
                            _parse_ref(details["scope_ref"], "scope")[1], node["node_id"], "probe"))
            details.update(node_id=node["node_id"], task_id=task["task_id"])
            if db.execute('PRAGMA user_version').fetchone()[0] >= 16:
                from .runtime_budget import materialize_exploration_work
                materialize_exploration_work(self, db, intent_id, task['task_id'])
            db.execute("UPDATE workflow_intents SET session_id=?,state='materialized',details=?,updated_at=? WHERE intent_id=?",
                       (session, encoded(details), now(), intent_id))
            self._event(db, "exploration.work_materialized", {"intent_id": intent_id, "node_id": node["node_id"], "task_id": task["task_id"]})
            return {"intent_id": intent_id, "state": "materialized", "node_id": node["node_id"], "task_id": task["task_id"]}
        return self._mutate("exploration.materialize", payload, operation, work)

    def exploration_record_result(self, db, node_id, source_key, kind, source_ref, details):
        if db.execute("PRAGMA user_version").fetchone()[0] < 13:
            return
        rows = list(db.execute("SELECT DISTINCT b.scope_id,b.route_id FROM exploration_bindings b "
                               "JOIN exploration_scopes s ON s.scope_id=b.scope_id "
                               "JOIN workflow_project p ON p.generation=s.generation "
                               "WHERE b.node_id=? AND p.id=1 ORDER BY b.scope_id,b.route_id", (node_id,)))
        for scope_id in sorted({row["scope_id"] for row in rows}):
            route_ids = [row["route_id"] for row in rows if row["scope_id"] == scope_id]
            self._exploration_result_in_scope(db, scope_id, source_key, kind, source_ref,
                                               {"node_id": node_id, "route_ids": route_ids, **details})

    def _exploration_result_in_scope(self, db, scope_id, source_key, kind, source_ref, details):
        result_id = "XR-" + hashlib.sha256(encoded([scope_id, source_key]).encode()).hexdigest()[:20]
        inserted = db.execute("INSERT OR IGNORE INTO exploration_results VALUES(?,?,?,?,?,?,?,?,?)",
                   (result_id, scope_id, source_key, kind, source_ref,
                    db.execute("SELECT COALESCE(MAX(event_id),0) FROM events").fetchone()[0],
                    encoded(details), None, now()))
        if inserted.rowcount:
            self._event(db, 'exploration.result_recorded', {'result_id': result_id, 'scope_id': scope_id, 'kind': kind})
            sequence = db.execute('SELECT COALESCE(MAX(event_id),0) FROM events').fetchone()[0]
            db.execute('UPDATE exploration_results SET source_sequence=? WHERE result_id=?', (sequence, result_id))
        return result_id

    def exploration_record_change(self, db, target_ref, source_key, kind, source_ref):
        if db.execute('PRAGMA user_version').fetchone()[0] < 13 or not target_ref:
            return
        run = db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()
        scopes = _scopes(db, run['generation']) if run else []
        if not scopes:
            return
        from .review_lifecycle import closure
        for scope in scopes:
            refs = [*(scope.get('baseline_refs') or []), *(scope.get('question_refs') or [])]
            routes = []
            for row in db.execute("SELECT r.* FROM exploration_routes r JOIN "
                    "(SELECT route_id,MAX(revision) revision FROM exploration_routes WHERE scope_id=? GROUP BY route_id) x "
                    "ON r.route_id=x.route_id AND r.revision=x.revision", (scope['scope_id'],)):
                route = _decode(row, 'route')
                routes.append(route['route_id'])
                for field in ('hypothesis_refs', 'baseline_refs', 'evidence_refs'):
                    refs.extend(route.get(field) or [])
            declared = self._project(db)['config'].get('r2_review_scope')
            if declared and declared['generation'] == run['generation']:
                refs.extend(declared['refs'])
            if target_ref in closure(self, db, refs):
                self._exploration_result_in_scope(db, scope['scope_id'], source_key, kind, source_ref,
                                                  {'target_ref': target_ref, 'route_ids': routes})

    def exploration_pending(self, host, session):
        with self._read() as db:
            run = _main(db, host, session)
            if self._project(db)["config"].get("exploration_policy") != "exploration-v1":
                return []
            grouped = {}
            for row in db.execute("SELECT intent_id,details FROM workflow_intents WHERE kind='exploration_work' "
                    "AND state IN ('pending','waiting_budget') AND json_extract(details,'$.generation')=? ORDER BY rowid", (run["generation"],)):
                grouped.setdefault(json.loads(row['details'])['scope_id'], []).append(row['intent_id'])
            return [items[index] for index in range(max((len(items) for items in grouped.values()), default=0))
                    for items in grouped.values() if index < len(items)]

    def exploration_task_check(self, host, session, task_id, operation):
        payload = {"host": host, "session": session, "task_id": task_id}

        def work(db):
            require_controller(db)
            _main(db, host, session)
            return check_exploration_task(self, db, task_id)
        return self._mutate("exploration.task_check", payload, operation, work)

    def exploration_page(self, collection, *, cursor=None, limit=50, ref=None, scope_id=None):
        if cursor is not None and not isinstance(cursor, dict):
            raise ValidationError('Exploration cursor must be an object')
        kinds = {"exploration_scopes": "scope", "exploration_routes": "route",
                 "exploration_decisions": "decision", "exploration_results": "result", "exploration_intents": "intent"}
        if collection not in kinds:
            raise ValidationError("Unknown exploration collection")
        kind = kinds[collection]
        with self._read() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version < 13:
                return {"available": False, "items": [], "cursor": None, "semantics": "legacy"}
            if ref and kind in {"scope", "route"}:
                ident, revision = _parse_ref(ref, kind)
                table = {"scope": "exploration_scopes", "route": "exploration_routes"}[kind]
                row = db.execute(f"SELECT * FROM {table} WHERE {kind}_id=? AND revision=?", (ident, revision)).fetchone()
                if not row:
                    raise NotFoundError(f"Unknown exploration reference: {ref}")
                return _decode(row, kind)
            project_id = self._project(db)["project_id"]
            generation = db.execute("SELECT generation FROM workflow_project WHERE id=1").fetchone()[0]
            scopes = _scopes(db, generation)
            if scope_id is not None:
                scopes = [item for item in scopes if item['scope_id'] == scope_id]
                if not scopes:
                    raise ValidationError('Unknown scope in this generation')
            scope_ids = [item['scope_id'] for item in scopes]
            if kind == "scope":
                values = scopes
            elif kind == "route" and scopes:
                rows = db.execute("SELECT r.* FROM exploration_routes r JOIN "
                                  "(SELECT route_id,MAX(revision) revision FROM exploration_routes WHERE scope_id IN (SELECT value FROM json_each(?)) GROUP BY route_id) latest "
                                  "ON r.route_id=latest.route_id AND r.revision=latest.revision ORDER BY r.route_id",
                                  (encoded(scope_ids),))
                values = [_decode(row, "route") for row in rows]
            elif kind in {"decision", "result"} and scopes:
                table = {"decision": "exploration_decisions", "result": "exploration_results"}[kind]
                key = kind + "_id"
                values = []
                for row in db.execute(f"SELECT * FROM {table} WHERE scope_id IN (SELECT value FROM json_each(?)) ORDER BY rowid", (encoded(scope_ids),)):
                    item = dict(row)
                    item["data"] = json.loads(item["data"])
                    if "actor" in item:
                        item["actor"] = json.loads(item["actor"])
                    item["ref"] = f"explore/{kind}/{item[key]}"
                    values.append(item)
            elif kind == 'intent' and scopes:
                values = []
                for row in db.execute("SELECT * FROM workflow_intents WHERE kind='exploration_work' "
                        "AND json_extract(details,'$.scope_id') IN (SELECT value FROM json_each(?)) ORDER BY rowid", (encoded(scope_ids),)):
                    item = dict(row)
                    item['details'] = json.loads(item['details'])
                    item['ref'] = item['intent_id']
                    values.append(item)
            else:
                values = []
            if ref and kind in {'decision', 'result', 'intent'}:
                found = next((item for item in values if item['ref'] == ref), None)
                if not found:
                    raise NotFoundError(f'Unknown exploration reference: {ref}')
                return found
            signature = {"project_id": project_id, "collection": collection, "generation": generation,
                         "scope_id": scope_id, "scope_refs": [item['ref'] for item in scopes],
                         "revisions": hashlib.sha256(encoded(values).encode()).hexdigest()}
            offset = 0
            if cursor is not None:
                if any(cursor.get(key) != value for key, value in signature.items()):
                    raise ConflictError("read_view_changed: refresh exploration")
                offset = cursor.get('offset')
                if type(offset) is not int or not 0 <= offset <= len(values):
                    raise ValidationError('Invalid exploration cursor offset')
            limit = max(1, min(int(limit or 50), 100))
            items = values[offset:offset + limit]
            next_offset = offset + len(items)
            return {"available": True, "items": items, "total": len(values),
                    "cursor": {**signature, "offset": next_offset} if next_offset < len(values) else None}
