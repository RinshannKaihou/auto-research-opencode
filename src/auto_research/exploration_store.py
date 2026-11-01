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
                 "baseline_refs", "budget_policy"}
_ROUTE_FIELDS = {"title", "rationale", "critical_assumption", "hypothesis_refs", "baseline_refs",
                 "evidence_refs", "next_probe", "cost_estimate", "reactivation_condition"}
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
    # Serialize here so invalid values fail before inserting a revision.
    encoded(value)
    return value


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
    if build_evidence_view(store, db, details["roots"])["basis_digest"] != details["basis_digest"]:
        settle_exploration_work(store, db, row, 'stale', 'exploration_basis_changed')
        return {"allowed": False, "reason": "exploration_basis_changed", 'state':'cancelled'}
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
                if _current_scope(db, run["generation"]):
                    raise ConflictError("A current exploration scope already exists")
                ident = self._next(db, "exploration_scope", "ES")
                revision, goal_version = 1, 1
                data = {"question_refs": [], "constraints": None, "comparison_basis": None,
                        "baseline_refs": [], "budget_policy": None, **changes}
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
                        "disposition": "untried", "disposition_reason": None, **changes}
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
            from .evidence_read import basis_changes, build_evidence_view
            require_controller(db)
            run = _main(db, host, session)
            require_new_work(db, host, session, manual=True, waiting=True, role="main")
            enabled = self._project(db)["config"].get("exploration_policy", "off") == "exploration-v1"
            if not enabled and (fields.get('next_work') is not None or fields.get('action') not in {'defer', 'propose_stop'}):
                raise ConflictError('Exploration is disabled; only closing decisions are allowed')
            if not snapshot or snapshot["generation"] != run["generation"]:
                raise ConflictError("evidence_snapshot_expired: read the evidence again")
            scope_id, revision = _parse_ref(fields.get("scope_ref"), "scope")
            scope = _latest(db, "scope", scope_id)
            if scope["generation"] != run["generation"] or scope["revision"] != revision:
                raise ConflictError("exploration_scope_changed: refresh the current scope")
            current = build_evidence_view(self, db, snapshot["view"]["roots"])
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
            if action not in {"probe", "deepen", "redirect", "defer", "recommend_combine", "propose_stop"}:
                raise ValidationError("Unknown exploration action")
            reason = _text(fields.get("reason"), "reason")
            next_work = fields.get("next_work")
            if next_work is not None:
                if not isinstance(next_work, dict):
                    raise ValidationError("next_work must be an object")
                for key in ("question", "why_now", "plan"):
                    _text(next_work.get(key), f"next_work.{key}")
                selected = next_work.get("route_refs")
                if not isinstance(selected, list) or not selected or any(ref not in routes for ref in selected):
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
            supersede = fields.get('supersede_intent_ids', [])
            if not isinstance(supersede, list) or any(not isinstance(item, str) for item in supersede):
                raise ValidationError('supersede_intent_ids must be a list')
            for intent_id in supersede:
                row = db.execute("SELECT * FROM workflow_intents WHERE kind='exploration_work' AND intent_id=?", (intent_id,)).fetchone()
                details = json.loads(row['details']) if row else {}
                if not row or details.get('scope_id') != scope_id or row['state'] not in {'pending', 'waiting_budget'}:
                    raise ConflictError('Only an unstarted intent in this scope can be superseded')
                db.execute("UPDATE workflow_intents SET state='superseded',updated_at=? WHERE intent_id=?", (now(), intent_id))
            actor = {"host_id": host, "session_id": session}
            decision_id = self._next(db, "exploration_decision", "ED")
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
                refs = change.get("evidence_refs", old.get("evidence_refs", []))
                _changes(self, db, {"evidence_refs": refs}, _ROUTE_FIELDS)
                data = {key: old.get(key) for key in _ROUTE_FIELDS}
                data.update(disposition=disposition, disposition_reason=change_reason,
                            reactivation_condition=condition, evidence_refs=refs, reason=change_reason)
                db.execute("INSERT INTO exploration_routes VALUES(?,?,?,?,?,?,?)", (old["route_id"], old["revision"] + 1,
                    scope_id, scope["goal_version"], encoded(data), encoded(actor), now()))
                current_routes[ref] = _latest(db, "route", old["route_id"])
            if next_work:
                for ref in next_work['route_refs']:
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
                    "result_ids": result_ids, "missing_results": fields.get("missing_results", []),
                    "action": action, "reason": reason, "route_changes": route_changes,
                    "remaining_options": fields.get("remaining_options", []), "next_work": next_work,
                    "supersede_intent_ids": supersede}
            db.execute("INSERT INTO exploration_decisions VALUES(?,?,?,?,?,?,?)",
                       (decision_id, scope_id, scope["revision"], run["generation"], encoded(data), encoded(actor), now()))
            for result_id in result_ids:
                db.execute("UPDATE exploration_results SET handled_by_decision=? WHERE result_id=?", (decision_id, result_id))
            intent_id = None
            if next_work:
                active = db.execute("SELECT intent_id FROM workflow_intents WHERE kind='exploration_work' "
                                    "AND state IN ('pending','waiting_budget','materialized') "
                                    "AND json_extract(details,'$.scope_id')=?", (scope_id,)).fetchall()
                for row in active:
                    intent = db.execute("SELECT state,details FROM workflow_intents WHERE intent_id=?", (row["intent_id"],)).fetchone()
                    detail = json.loads(intent["details"])
                    task = db.execute("SELECT state FROM exploration_tasks WHERE task_id=?", (detail.get("task_id"),)).fetchone()
                    live = task and task["state"] in {"queued", "starting", "running", "waiting", "stopping", "unverified"}
                    if live or (intent["state"] != "materialized" and row["intent_id"] not in fields.get("supersede_intent_ids", [])):
                        raise ConflictError("Existing exploration work must finish or be explicitly superseded")
                    if intent["state"] != "materialized":
                        db.execute("UPDATE workflow_intents SET state='superseded',updated_at=? WHERE intent_id=?", (now(), row["intent_id"]))
                intent_id = "exploration-work:" + decision_id
                # Reconcile the route changes before taking the post-decision
                # basis; the outer transaction will reconcile again harmlessly.
                from .review_lifecycle import reconcile
                reconcile(self, db, operation)
                post = build_evidence_view(self, db, snapshot["view"]["roots"])
                details = {"scope_id": scope_id, "scope_ref": scope["ref"], "generation": run["generation"],
                           "decision_id": decision_id, "basis_digest": post["basis_digest"],
                           "roots": snapshot["view"]["roots"], "next_work": next_work,
                           "route_refs": [current_routes[ref]["ref"] for ref in next_work["route_refs"]]}
                db.execute("INSERT INTO workflow_intents VALUES(?,?,?,?,?,?)",
                           (intent_id, "exploration_work", session, "pending", encoded(details), now()))
            self._event(db, "exploration.decided", {"decision_id": decision_id, "scope_ref": scope["ref"],
                                                     "result_ids": result_ids, "intent_id": intent_id, "actor": actor})
            return {"decision_ref": f"explore/decision/{decision_id}", "decision_id": decision_id,
                    "route_refs": [current_routes[ref]["ref"] for ref in route_refs], "intent_id": intent_id}
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
            current = build_evidence_view(self, db, details["roots"])
            if current["basis_digest"] != details["basis_digest"]:
                return settle_exploration_work(self, db, row, 'stale', 'exploration_basis_changed')
            try:
                check_node(self, db)
            except BudgetError as error:
                db.execute("UPDATE workflow_intents SET state='waiting_budget',updated_at=? WHERE intent_id=?", (now(), intent_id))
                return {"intent_id": intent_id, "state": "waiting_budget", "reason": str(error)}
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
        scope = _current_scope(db, run['generation']) if run else None
        if not scope:
            return
        from .review_lifecycle import closure
        refs = [*(scope.get('baseline_refs') or []), *(scope.get('question_refs') or [])]
        routes = []
        for row in db.execute("SELECT r.* FROM exploration_routes r JOIN "
                "(SELECT route_id,MAX(revision) revision FROM exploration_routes WHERE scope_id=? GROUP BY route_id) x "
                "ON r.route_id=x.route_id AND r.revision=x.revision", (scope['scope_id'],)):
            route = _decode(row, 'route')
            if route['disposition'] in {'investigating', 'adopted'}:
                routes.append(route['route_id'])
                refs.extend(route.get('hypothesis_refs') or [])
                refs.extend(route.get('evidence_refs') or [])
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
            return [row[0] for row in db.execute("SELECT intent_id FROM workflow_intents WHERE kind='exploration_work' "
                "AND state IN ('pending','waiting_budget') AND json_extract(details,'$.generation')=? ORDER BY rowid", (run["generation"],))]

    def exploration_task_check(self, host, session, task_id, operation):
        payload = {"host": host, "session": session, "task_id": task_id}

        def work(db):
            require_controller(db)
            _main(db, host, session)
            return check_exploration_task(self, db, task_id)
        return self._mutate("exploration.task_check", payload, operation, work)

    def exploration_page(self, collection, *, cursor=None, limit=50, ref=None):
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
            scope = _current_scope(db, generation)
            if kind == "scope":
                values = [scope] if scope else []
            elif kind == "route" and scope:
                rows = db.execute("SELECT r.* FROM exploration_routes r JOIN "
                                  "(SELECT route_id,MAX(revision) revision FROM exploration_routes WHERE scope_id=? GROUP BY route_id) latest "
                                  "ON r.route_id=latest.route_id AND r.revision=latest.revision ORDER BY r.route_id",
                                  (scope["scope_id"],))
                values = [_decode(row, "route") for row in rows]
            elif kind in {"decision", "result"} and scope:
                table = {"decision": "exploration_decisions", "result": "exploration_results"}[kind]
                key = kind + "_id"
                values = []
                for row in db.execute(f"SELECT * FROM {table} WHERE scope_id=? ORDER BY rowid", (scope["scope_id"],)):
                    item = dict(row)
                    item["data"] = json.loads(item["data"])
                    if "actor" in item:
                        item["actor"] = json.loads(item["actor"])
                    item["ref"] = f"explore/{kind}/{item[key]}"
                    values.append(item)
            elif kind == 'intent' and scope:
                values = []
                for row in db.execute("SELECT * FROM workflow_intents WHERE kind='exploration_work' "
                        "AND json_extract(details,'$.scope_id')=? ORDER BY rowid", (scope['scope_id'],)):
                    item = dict(row)
                    item['details'] = json.loads(item['details'])
                    item['ref'] = item['intent_id']
                    values.append(item)
            else:
                values = []
            signature = {"project_id": project_id, "collection": collection, "generation": generation,
                         "scope_ref": scope["ref"] if scope else None,
                         "revisions": hashlib.sha256(encoded(values).encode()).hexdigest()}
            offset = 0
            if cursor is not None:
                if any(cursor.get(key) != value for key, value in signature.items()):
                    raise ConflictError("read_view_changed: refresh exploration")
                offset = int(cursor["offset"])
            limit = max(1, min(int(limit or 50), 100))
            items = values[offset:offset + limit]
            next_offset = offset + len(items)
            return {"available": True, "items": items, "total": len(values),
                    "cursor": {**signature, "offset": next_offset} if next_offset < len(values) else None}
