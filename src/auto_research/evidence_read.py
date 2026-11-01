"""Consistent, paged views of declared evidence and current risks."""

from collections import OrderedDict
import hashlib
import json
from uuid import uuid4

from . import epistemic, frozen_refs
from .errors import ConflictError, ValidationError
from .exploration_store import _current_scope, _decode
from .memory_store import parse_knowledge_ref
from .review_lifecycle import unresolved_challenges
from .workflow_store import encoded
from .material_read import MaterialReader, canonical_content, material_coverage


def _hash(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def basis_changes(before, after):
    old_records = {item['ref']: item for item in before['records']}
    new_records = {item['ref']: item for item in after['records']}
    old_routes = {item['route_id']: item['ref'] for item in before['routes']}
    new_routes = {item['route_id']: item['ref'] for item in after['routes']}
    old_results = {item['result_id'] for item in before['pending_results']}
    new_results = {item['result_id'] for item in after['pending_results']}
    # Content selected for delivery is an augmentation of the structural view,
    # not a change to the frozen reference itself.
    def materials(view):
        return {item['ref']: {key: value for key, value in item.items() if key != 'content_text'}
                for item in view['materials']}
    old_materials, new_materials = materials(before), materials(after)
    def structural_limits(view):
        return {encoded(item) for item in view['limitations'] if item['reason'] not in {
            'material_content_not_delivered', 'material_too_large_for_snapshot', 'material_not_utf8'}}
    old_limits, new_limits = structural_limits(before), structural_limits(after)
    return {'changed_refs': sorted(ref for ref in old_records.keys() | new_records.keys()
                                  if old_records.get(ref) != new_records.get(ref)),
            'changed_materials': sorted(ref for ref in old_materials.keys() | new_materials.keys()
                                        if old_materials.get(ref) != new_materials.get(ref)),
            'added_limitations': [json.loads(item) for item in sorted(new_limits - old_limits)],
            'removed_limitations': [json.loads(item) for item in sorted(old_limits - new_limits)],
            'changed_routes': sorted(key for key in old_routes.keys() | new_routes.keys()
                                     if old_routes.get(key) != new_routes.get(key)),
            'added_results': sorted(new_results - old_results), 'handled_results': sorted(old_results - new_results),
            'scope_before': (before.get('scope') or {}).get('ref'),
            'scope_after': (after.get('scope') or {}).get('ref'),
            'core_scope_before': before.get('conclusion_scope'),
            'core_scope_after': after.get('conclusion_scope'),
            'risks_changed': before['risks'] != after['risks'],
            'reviews_changed': before['review_history'] != after['review_history']}


def build_evidence_view(store, db, refs):
    """Read one structural view under the caller's SQLite read transaction."""
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
        raise ValidationError("Evidence roots must be a list of fixed references")
    refs = sorted(set(refs))
    bound = epistemic.read_bound(db)
    generation = db.execute("SELECT generation FROM workflow_project WHERE id=1").fetchone()[0]
    version = db.execute("PRAGMA user_version").fetchone()[0]
    scope = _current_scope(db, generation) if version >= 13 else None
    routes, pending = [], []
    if scope:
        rows = db.execute("SELECT r.* FROM exploration_routes r JOIN "
                          "(SELECT route_id,MAX(revision) revision FROM exploration_routes WHERE scope_id=? GROUP BY route_id) x "
                          "ON r.route_id=x.route_id AND r.revision=x.revision ORDER BY r.route_id", (scope["scope_id"],))
        routes = [_decode(row, "route") for row in rows]
        pending = [{**dict(row), "data": json.loads(row["data"])} for row in db.execute(
            "SELECT * FROM exploration_results WHERE scope_id=? AND handled_by_decision IS NULL ORDER BY result_id", (scope["scope_id"],))]

    records, edges, materials, limitations, publication_checks = {}, [], {}, [], {}
    queue, visited = list(refs), set()
    while queue:
        ref = queue.pop()
        if ref in visited:
            continue
        visited.add(ref)
        if ref.startswith("knowledge/"):
            try:
                kid, rev = parse_knowledge_ref(ref)
            except ValidationError:
                limitations.append({"ref": ref, "reason": "unsupported_reference"})
                continue
            row = db.execute("SELECT e.kind,e.node_id,r.* FROM knowledge_entries e "
                             "JOIN knowledge_revisions r USING(knowledge_id) WHERE e.knowledge_id=? AND r.revision=?",
                             (kid, rev)).fetchone()
            if not row:
                limitations.append({"ref": ref, "reason": "knowledge_missing"})
                continue
            pinned = store._decode_knowledge(row, db)
            current = store._decode_knowledge(store._latest_revision(db, kid), db)
            records[ref] = {"ref": ref, "pinned": pinned, "current": current}
            for edge in db.execute("SELECT user_ref,used_ref,from_dependencies,from_evidence FROM knowledge_support_edges "
                                   "WHERE user_ref=? ORDER BY used_ref", (ref,)):
                edges.append(dict(edge))
                queue.append(edge["used_ref"])
            for unresolved in epistemic.uncovered_refs(db, ref):
                used = unresolved["ref"]
                if frozen_refs.is_frozen(used):
                    # Material is a terminal source, not a knowledge propagation edge.
                    edges.append({"user_ref": ref, "used_ref": used, "from_dependencies": int(unresolved["field"] == "dependencies"),
                                  "from_evidence": int(unresolved["field"] == "evidence_refs")})
                    queue.append(used)
                else:
                    limitations.append({"ref": ref, "reason": "uncovered_reference", "detail": unresolved})
        elif frozen_refs.is_frozen(ref):
            resolution = frozen_refs.resolve(db, store.root, ref)
            materials[ref] = resolution
            if resolution["outcome"] != "resolved":
                limitations.append({"ref": ref, "reason": resolution["reason"]})
                continue
            if resolution.get("object"):
                limitations.append({"ref": ref, "reason": "material_content_not_delivered"})
            parsed = frozen_refs.parse(ref)
            pid = parsed.get("publication_id")
            if pid and parsed.get("item_id"):
                item = db.execute("SELECT kind,content,object_version,object_kind,source_path,knowledge_refs FROM publication_items WHERE publication_id=? AND item_id=?",
                                  (pid, parsed["item_id"])).fetchone()
                content = json.loads(item['content'])
                content_bytes = canonical_content(content)
                if len(content_bytes) > 2048:
                    content = None
                    materials[ref]['inline_digest'] = hashlib.sha256(content_bytes).hexdigest()
                    if not resolution.get('object'):
                        limitations.append({'ref': ref, 'reason': 'material_content_not_delivered'})
                materials[ref]["item"] = {**dict(item), "content": content,
                                           "knowledge_refs": json.loads(item["knowledge_refs"])}
                queue.extend(json.loads(item["knowledge_refs"]))
            elif pid:
                from .publication_display import DISPLAY_KIND
                for item in db.execute('SELECT item_id FROM publication_items WHERE publication_id=? AND kind<>? ORDER BY item_id',
                                       (pid, DISPLAY_KIND)):
                    child = f"pub/{pid}#{item['item_id']}"
                    edges.append({'user_ref': ref, 'used_ref': child, 'from_dependencies': 0, 'from_evidence': 1})
                    queue.append(child)
                queue.extend(f"knowledge/{row[0]}@{row[1]}" for row in db.execute(
                    "SELECT knowledge_id,revision FROM publication_knowledge WHERE publication_id=?", (pid,)))
            if pid:
                declared = json.loads(item['knowledge_refs']) if parsed.get('item_id') else None
                check = epistemic.publication_check(db, pid, bound, declared_refs=declared)
                publication_checks[ref] = {key: value for key, value in check.items() if key != 'sequence_bound'}
                # Raw source material may legitimately have no knowledge claims.
                # A selected report, however, cannot have unknown declared
                # coverage silently presented as a complete check.
                is_report = not parsed.get('item_id') or item['kind'] == 'report'
                if ref in refs and is_report and not check['targets_complete']:
                    limitations.append({'ref': ref, 'reason': 'publication_coverage_incomplete',
                                        'status': check['result']['status']})
        else:
            limitations.append({"ref": ref, "reason": "unsupported_reference"})

    risks = []
    for ref, entry in sorted(records.items()):
        for reason in epistemic.publication_reference_risk(db, ref, bound):
            risks.append({"kind": "reference", "target": ref, "detail": reason, "relevance": "related"})
        for impact in epistemic.valid_impacts(db, bound, version=ref):
            disposition = epistemic.current_disposition(db, impact["change_id"], ref, bound)
            risks.append({"kind": "impact", "target": ref, "change_id": impact["change_id"],
                          "disposition": dict(disposition) if disposition else None, "relevance": "related"})
        for notice in epistemic.version_notices(db, ref, bound):
            risks.append({"kind": "version_notice", "target": ref, "detail": notice, "relevance": "related"})
        for check in entry["pinned"].get("field_checks", []):
            if check["result"] != "consistent":
                risks.append({"kind": "field_check", "target": ref, "detail": check, "relevance": "related"})
    if version >= 11:
        for challenge in unresolved_challenges(db):
            target = challenge['target_ref']
            relevance = 'related' if target in visited else None
            if relevance is None and target.startswith('knowledge/'):
                identity = target.split('@', 1)[0]
                if any(ref.startswith(identity + '@') for ref in visited):
                    relevance = 'unknown'
            if relevance is None and frozen_refs.is_frozen(target):
                owner = frozen_refs.parse(target)
                if owner.get('publication_id') and any(
                    frozen_refs.is_frozen(ref) and frozen_refs.parse(ref).get('publication_id') == owner['publication_id']
                    for ref in visited
                ):
                    relevance = 'unknown'
            if relevance is None:
                continue
            risks.append({"kind": "review_challenge", "target": challenge["target_ref"],
                          "review_event": challenge["event_id"], "obligation_id": challenge["obligation_id"],
                          "evidence_refs": json.loads(challenge["evidence_refs"]),
                          "relevance": relevance})
    reviews, review_history = [], []
    if version >= 11:
        for row in db.execute("SELECT o.target_ref,o.obligation_id,e.event_id,e.action,e.evidence_refs,e.scope_snapshot "
                              "FROM review_obligations o JOIN review_lifecycle_events e USING(obligation_id) ORDER BY e.event_id"):
            if row['target_ref'] in visited:
                item = dict(row)
                item['evidence_refs'] = json.loads(item['evidence_refs'])
                item['scope_snapshot'] = json.loads(item['scope_snapshot'])
                review_history.append(item)
    if version >= 14:
        for row in db.execute("SELECT a.*,e.action,e.scope_snapshot,o.obligation_id,o.generation,o.change_id "
                              "FROM review_assessments a JOIN review_lifecycle_events e USING(event_id) "
                              "JOIN review_obligations o USING(obligation_id) ORDER BY a.event_id"):
            if row["target_ref"] in visited:
                item = dict(row)
                for key in ("identity", "limitations", "scope_snapshot"):
                    item[key] = json.loads(item[key])
                latest_reopen = db.execute("SELECT COALESCE(MAX(event_id),0) FROM review_lifecycle_events "
                                           "WHERE obligation_id=? AND action='reopen'", (row['obligation_id'],)).fetchone()[0]
                accepted = db.execute("SELECT e.event_id FROM review_lifecycle_events e JOIN review_obligations o USING(obligation_id) "
                    "WHERE o.generation=? AND o.target_ref=? AND o.change_id IS ? AND e.action='accept_previous' "
                    "AND json_extract(e.scope_snapshot,'$.report_link')=? AND e.event_id>COALESCE((SELECT MAX(r.event_id) "
                    "FROM review_lifecycle_events r WHERE r.obligation_id=o.obligation_id AND r.action='reopen'),0) "
                    "ORDER BY e.event_id DESC LIMIT 1", (generation, row['target_ref'], row['change_id'], row['event_id'])).fetchone()
                item['accepted_by_event'] = accepted[0] if accepted else None
                item['effective_in_generation'] = bool((row['generation'] == generation and row['event_id'] > latest_reopen) or accepted)
                reviews.append(item)
                if item['effective_in_generation'] and item['scope_snapshot'].get('result') == 'inconclusive':
                    risks.append({'kind': 'review_inconclusive', 'target': item['target_ref'],
                                  'review_event': item['event_id'], 'relevance': 'related'})
    risks.sort(key=encoded)
    for risk in risks:
        risk["risk_id"] = "risk-" + _hash(risk)[:24]
    for limitation in limitations:
        limitation["risk_id"] = "limit-" + _hash(limitation)[:24]
    conclusion_scope = store._project(db)['config'].get('conclusion_review_scope')
    if conclusion_scope and conclusion_scope['generation'] != generation:
        conclusion_scope = None
    view = {"roots": refs, "scope": scope, "conclusion_scope": conclusion_scope,
            "routes": routes, "pending_results": pending,
            "records": [records[key] for key in sorted(records)], "edges": sorted(edges, key=encoded),
            "materials": [materials[key] for key in sorted(materials)],
            "publication_checks": [dict(ref=key, **publication_checks[key]) for key in sorted(publication_checks)],
            "risks": risks, "reviews": reviews,
            "review_history": review_history,
            "limitations": sorted(limitations, key=encoded),
            "graph_complete": not any(item['reason'] != 'material_content_not_delivered' for item in limitations),
            "raw_material_read": "not_assessed"}
    return {"project_id": store._project(db)["project_id"], "generation": generation,
            "source_sequence": bound, "basis_digest": _hash(view), "view": view}


class EvidenceCache:
    """In-memory snapshots. Cursors never silently cross a changed read view."""

    def __init__(self, max_snapshots=16, max_bytes=16 * 1024 * 1024):
        self._snapshots = OrderedDict()
        self._max_snapshots = max_snapshots
        self._max_bytes = max_bytes
        self._bytes = 0
        self.materials = MaterialReader()

    def annotate_materials(self, store, view, host=None, session=None):
        """Attach delivered ranges without changing the scientific basis digest."""
        for material in view['materials']:
            item = material.get('item') or {}
            if material['outcome'] != 'resolved' or not (material.get('object') or material.get('inline_digest')):
                continue
            try:
                descriptor = self.materials.describe(store, material['ref'])
                public = {key: descriptor[key] for key in ('snapshot_id', 'digest', 'total_bytes', 'object', 'subpath')}
                with store._read() as db:
                    generation = db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()[0]
                    coverage = (material_coverage(db, host, session, generation, descriptor)
                                if host and session and db.execute('PRAGMA user_version').fetchone()[0] >= 15
                                else {'complete': False, 'covered_bytes': 0})
                material['material_identity'] = public
                material['material_read'] = coverage
                if coverage['complete']:
                    view['limitations'] = [limit for limit in view['limitations'] if not (
                        limit['ref'] == material['ref'] and limit['reason'] == 'material_content_not_delivered')]
            except (ValidationError, OSError) as error:
                material['read_error'] = str(error)
        return view

    def open(self, store, refs, include_materials=None, host=None, session=None):
        with store._read() as db:
            snapshot = build_evidence_view(store, db, refs)
        if include_materials:
            raise ValidationError('evidence_read_contract_changed: use material_ref to read frozen material')
        self.annotate_materials(store, snapshot['view'], host, session)
        with store._read() as db:
            if build_evidence_view(store, db, refs)['basis_digest'] != snapshot['basis_digest']:
                raise ConflictError('evidence_basis_changed: read the evidence again')
        return self._remember(store, snapshot)

    def draft_snapshot(self, store, draft_id, snapshot_id=None):
        with store._read() as db:
            row = db.execute('SELECT * FROM conclusion_drafts WHERE draft_id=?', (draft_id,)).fetchone()
            if not row:
                raise ValidationError('Unknown conclusion draft')
            data = json.loads(row['data'])
            view = data['view']
            snapshot = {'project_id': store._project(db)['project_id'], 'generation': row['generation'],
                        'source_sequence': row['source_sequence'], 'basis_digest': row['basis_digest'],
                        'host_id': row['host_id'], 'session_id': row['session_id'],
                        'draft_id': draft_id, 'view': view, 'snapshot_id': snapshot_id, 'root': str(store.root),
                        'format_version': data.get('format_version', 1)}
        body = json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
        if digest != row['manifest_digest']:
            raise ConflictError('Conclusion draft manifest has changed')
        return {**snapshot, 'body': body, 'manifest_digest': digest}

    def open_draft(self, store, draft_id):
        snapshot = self.draft_snapshot(store, draft_id)
        snapshot_id = self._remember(store, snapshot)
        return snapshot_id

    def _remember(self, store, snapshot):
        body = json.dumps(snapshot["view"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        size = len(body.encode("utf-8"))
        if size > self._max_bytes:
            raise ValidationError("evidence_snapshot_too_large: narrow the declared scope and preserve this limitation")
        snapshot_id = "evidence-" + uuid4().hex
        snapshot.update(snapshot_id=snapshot_id, root=str(store.root), body=body,
                        manifest_digest=hashlib.sha256(body.encode("utf-8")).hexdigest(), size=size)
        while self._snapshots and (len(self._snapshots) >= self._max_snapshots or self._bytes + size > self._max_bytes):
            _, old = self._snapshots.popitem(last=False)
            self._bytes -= old["size"]
        self._snapshots[snapshot_id] = snapshot
        self._bytes += size
        return snapshot_id

    def get(self, store, snapshot_id):
        snapshot = self._snapshots.get(snapshot_id)
        if not snapshot:
            raise ConflictError("evidence_snapshot_expired: read the evidence again")
        if snapshot["root"] != str(store.root):
            raise ValidationError("Evidence snapshot belongs to another project")
        self._snapshots.move_to_end(snapshot_id)
        return snapshot

    def page(self, store, *, refs=None, include_materials=None, draft_id=None, snapshot_id=None, cursor=None,
             material_ref=None, limit=4096, host=None, session=None):
        if material_ref or (cursor and cursor.get('part') == 'material'):
            return self.materials.page(store, material_ref, cursor, limit)
        if cursor:
            if snapshot_id is not None and snapshot_id != cursor.get("snapshot_id"):
                raise ValidationError("Evidence cursor does not match snapshot")
            snapshot_id = cursor.get("snapshot_id")
            offset = cursor.get("offset")
            if cursor.get('draft_id'):
                if draft_id is not None and draft_id != cursor['draft_id']:
                    raise ValidationError('Evidence cursor belongs to another conclusion draft')
                draft_id = cursor['draft_id']
        else:
            offset = 0
        if snapshot_id is None:
            snapshot_id = self.open_draft(store, draft_id) if draft_id else self.open(store, refs or [], include_materials, host, session)
        try:
            snapshot = self.get(store, snapshot_id)
        except ConflictError:
            if not draft_id:
                raise
            snapshot = self.draft_snapshot(store, draft_id, snapshot_id)
        if draft_id is not None and snapshot.get('draft_id') != draft_id:
            raise ValidationError('Evidence cursor belongs to another conclusion draft')
        if refs is not None and sorted(set(refs)) != snapshot["view"]["roots"]:
            raise ConflictError("read_view_changed: evidence roots changed")
        is_decision = snapshot.get('draft_id') and snapshot.get('format_version') == 2
        source = snapshot['body'].encode('utf-8') if is_decision else snapshot['body']
        if not isinstance(offset, int) or offset < 0 or offset > len(source):
            raise ValidationError("Invalid evidence cursor offset")
        limit = max(4 if is_decision else 1, min(int(limit or 4096), 16384))
        end = min(offset + limit, len(source))
        if is_decision:
            while end < len(source) and source[end] & 0xc0 == 0x80:
                end -= 1
            try:
                chunk = source[offset:end].decode('utf-8')
            except UnicodeDecodeError as error:
                raise ValidationError('Invalid UTF-8 decision cursor') from error
        else:
            chunk = source[offset:end]
        view = snapshot["view"]
        return {key: snapshot[key] for key in ("snapshot_id", "project_id", "generation", "source_sequence",
                                              "basis_digest", "manifest_digest")} | {
            "offset": offset, "end": end, "chunk": chunk, "total_chars": len(snapshot["body"]),
            **({'part': 'decision', 'offset_unit': 'utf8_byte', 'total_bytes': len(source)} if is_decision else {}),
            "draft_id": snapshot.get('draft_id'),
            "cursor": {"snapshot_id": snapshot_id, "offset": end,
                       **({'draft_id': snapshot['draft_id']} if snapshot.get('draft_id') else {})}
                      if end < len(source) else None,
            "counts": {key: len(view[key]) for key in ("records", "edges", "materials", "risks", "reviews", "limitations")},
            "graph_complete": view["graph_complete"], "raw_material_read": view["raw_material_read"]}
