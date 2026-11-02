"""Read-only work-grouped projection of exploration results (exploration_inbox).

Groups the per-result obligation stream by the identity of the work that
produced them: (generation, scope_id, task/intent identity, attempt_id). The
groups are a navigation view; the formal decision contract still cites explicit
result IDs, and a group key is never expandable into "the model read all
current members" on write.
"""
from __future__ import annotations

import hashlib
import json

from .errors import ConflictError, NotFoundError, ValidationError

MEMBER_SAMPLE_CAP = 20
LIVE_TASK_STATES = {"queued", "starting", "running", "waiting", "stopping", "unverified"}
SETTLED_INTENT_STATES = {"completed", "cancelled", "stale", "failed", "superseded"}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _group_key(scope_id, work_id, attempt_id):
    return "XG-" + hashlib.sha256(_encoded([scope_id, work_id, attempt_id or ""]).encode()).hexdigest()[:16]


def inbox_groups(store, db, generation, scope_ids):
    """Deterministic work groups over this generation's exploration results.

    Each returned group carries the complete member list under members_full for
    fingerprinting and ref paging; render it through _render before exposing a
    page item.
    """
    intents_by_node = {}
    intents_by_task = {}
    intents_by_id = {}
    for row in db.execute("SELECT intent_id,state,details FROM workflow_intents WHERE kind='exploration_work' "
                          "AND json_extract(details,'$.generation')=?", (generation,)):
        details = json.loads(row["details"])
        record = {"intent_id": row["intent_id"], "intent_state": row["state"], "details": details}
        intents_by_id[row["intent_id"]] = record
        if details.get("node_id"):
            intents_by_node.setdefault(details["node_id"], []).append(record)
        if details.get("task_id"):
            intents_by_task[details["task_id"]] = record
    tasks_by_id = {row["task_id"]: {"state": row["state"], "node_id": row["node_id"]}
                   for row in db.execute("SELECT task_id,node_id,state FROM exploration_tasks")}
    attempts_by_id = {row["attempt_id"]: {"state": row["state"], "node_id": row["node_id"]}
                      for row in db.execute("SELECT attempt_id,node_id,state FROM attempts")}
    publications_by_id = {row["publication_id"]: {"attempt_id": row["attempt_id"], "status": row["status"]}
                          for row in db.execute("SELECT publication_id,attempt_id,status FROM publications")}

    def work_identity(source_key, data):
        """Resolve (work_id, attempt_id, identity_status) for one result row.

        Settlement and task source keys look like prefix:<opaque id>:<outcome>;
        the opaque ids themselves contain colons, so only the known prefix and
        the trailing outcome segment are structural. Recorded intent/task ids
        in the result payload are authoritative when present.
        """
        def strip_outcome(prefix):
            rest = source_key[len(prefix):]
            return rest.rsplit(":", 1)[0] if rest.count(":") >= 1 else rest

        if source_key.startswith("intent:"):
            candidate = data.get("intent_id") or strip_outcome("intent:")
            record = intents_by_id.get(candidate)
            return (candidate, None, "verified") if record else (candidate, None, "unknown")
        if source_key.startswith("task:"):
            task_id = data.get("task_id") or strip_outcome("task:")
            record = intents_by_task.get(task_id)
            return (record["intent_id"], None, "verified") if record else (task_id, None, "unknown")
        node_id = data.get("node_id")
        attempt_id = None
        if source_key.startswith("attempt:"):
            attempt_id = source_key.split(":", 1)[1]
        elif source_key.startswith("publication:"):
            publication = publications_by_id.get(source_key.split(":", 1)[1])
            attempt_id = publication["attempt_id"] if publication else None
        if attempt_id:
            attempt = attempts_by_id.get(attempt_id)
            attempt_node = attempt["node_id"] if attempt else node_id
            candidates = intents_by_node.get(attempt_node or node_id, [])
            if len(candidates) == 1:
                return candidates[0]["intent_id"], attempt_id, "verified"
            if len(candidates) > 1:
                return candidates[0]["intent_id"], attempt_id, "unknown"
            if attempt_node or node_id:
                return f"node:{attempt_node or node_id}", attempt_id, "unknown"
        if node_id:
            candidates = intents_by_node.get(node_id, [])
            if len(candidates) == 1:
                return candidates[0]["intent_id"], None, "verified"
        return (f"node:{node_id}" if node_id else source_key), None, "unknown"

    groups = {}
    for row in db.execute("SELECT result_id,scope_id,source_key,kind,source_ref,handled_by_decision,data "
                          "FROM exploration_results WHERE scope_id IN (SELECT value FROM json_each(?)) "
                          "ORDER BY result_id", (_encoded(list(scope_ids)),)):
        data = json.loads(row["data"])
        source_key = row["source_key"]
        if source_key.startswith(("scope:", "state:", "knowledge-change:", "review-event:")):
            source = ("scope" if source_key.startswith(("scope:", "state:"))
                      else "review" if source_key.startswith("review-event:") else "evidence")
            key = _group_key(row["scope_id"], f"source:{source_key}", "")
            group = groups.setdefault(key, _empty_group(key, row["scope_id"], source))
            group["work_identity"] = {"source_key": source_key}
            group["identity_status"] = "source"
        else:
            work_id, attempt_id, status = work_identity(source_key, data)
            identity = {"work_id": work_id, "attempt_id": attempt_id, "node_id": data.get("node_id")}
            if data.get("intent_id"):
                identity["intent_id"] = data["intent_id"]
            if work_id and work_id.startswith("exploration-work:"):
                identity["intent_id"] = work_id
            if status == "verified":
                key = _group_key(row["scope_id"], work_id, attempt_id)
                group = groups.setdefault(key, _empty_group(key, row["scope_id"], "work"))
                group["work_identity"] = identity
                group["identity_status"] = status
            else:
                # Unresolved or ambiguous identity: one group per result, so no
                # two different works can be merged into one unknown bucket.
                key = _group_key(row["scope_id"], f"result:{row['result_id']}", "")
                group = groups.setdefault(key, _empty_group(key, row["scope_id"], "work"))
                group["work_identity"] = {**identity, "unresolved": True}
                group["identity_status"] = "unknown"
        group["members_full"].append(row["result_id"])
        group["kinds"].append(row["kind"])
        if row["handled_by_decision"] is None:
            group["pending_members"] += 1
        else:
            group["handled_members"] += 1
            group["handled_by"].append(row["handled_by_decision"])
        if row["source_ref"] and row["source_ref"].startswith("pub/"):
            group["publication_refs"].append(row["source_ref"])
    ordered = []
    for key in sorted(groups):
        group = groups[key]
        work_id = group["work_identity"].get("work_id")
        attempt_id = group["work_identity"].get("attempt_id")
        record = intents_by_id.get(work_id) if group["source"] == "work" else None
        task = tasks_by_id.get(record["details"].get("task_id")) if record else None
        attempt_state = attempts_by_id.get(attempt_id, {}).get("state") if attempt_id else None
        task_state = task["state"] if task else None
        if group["source"] != "work":
            execution_state = "registered"
            task_state = None
        elif attempt_id:
            # This group's results belong to one attempt: the attempt's own
            # terminal state is authoritative, and a running retry of the same
            # work must not rewrite it. The work's current task state is
            # reported separately as task_state.
            execution_state = attempt_state or "unknown"
        elif record:
            if record["intent_state"] in SETTLED_INTENT_STATES:
                execution_state = f"settled:{record['intent_state']}"
            elif task_state:
                execution_state = task_state
            else:
                execution_state = record["intent_state"]
        else:
            execution_state = "unknown"
        members = group["members_full"]
        ordered.append({**_render(group, execution_state, attempt_state, task_state)})
    return ordered


def _empty_group(key, scope_id, source):
    return {"group_key": key, "scope_id": scope_id, "source": source, "members_full": [],
            "kinds": [], "pending_members": 0, "handled_members": 0, "handled_by": [],
            "publication_refs": [], "work_identity": {}, "identity_status": "unknown"}


def _render(group, execution_state, attempt_state=None, task_state=None):
    members = group["members_full"]
    return {"group_key": group["group_key"], "scope_id": group["scope_id"], "source": group["source"],
            "work_identity": group["work_identity"], "identity_status": group["identity_status"],
            "execution_state": execution_state,
            # attempt_state is the terminal state of the attempt that produced
            # these results; task_state is the work's current task (a retry may
            # be running under a different attempt).
            "attempt_state": attempt_state, "task_state": task_state,
            "members_total": len(members), "pending_members": group["pending_members"],
            "handled_members": group["handled_members"], "handled_by": sorted(set(group["handled_by"])),
            "result_ids": members[:MEMBER_SAMPLE_CAP],
            "members_omitted": max(0, len(members) - MEMBER_SAMPLE_CAP),
            "members_complete": len(members) <= MEMBER_SAMPLE_CAP,
            "kinds": sorted(set(group["kinds"])),
            "publication_refs": sorted(set(group["publication_refs"])),
            "pending": group["pending_members"] > 0,
            "expand": {"collection": "exploration_inbox", "ref": group["group_key"]},
            "members_full": members}


def inbox_page(store, db, *, cursor=None, limit=50, ref=None, scope_id=None):
    """Page the work groups, or one group's members via ref, without writing.

    The cursor fingerprint covers the complete member lists, so a member that
    arrives mid-read forces read_view_changed instead of quietly joining an
    already-served page.
    """
    from .exploration_store import _scopes
    generation_row = db.execute("SELECT generation FROM workflow_project WHERE id=1").fetchone()
    scopes = _scopes(db, generation_row["generation"]) if generation_row else []
    if scope_id is not None:
        scopes = [item for item in scopes if item["scope_id"] == scope_id]
        if not scopes:
            raise ValidationError("Unknown scope in this generation")
    if not scopes:
        return {"available": True, "items": [], "total": 0, "pending_groups_total": 0,
                "cursor": None, "semantics": "exploration-inbox-v1"}
    scope_ids = [item["scope_id"] for item in scopes]
    groups = inbox_groups(store, db, generation_row["generation"], scope_ids)
    pending_groups_total = sum(group["pending"] for group in groups)
    limit = max(1, min(int(limit or 50), 100))
    if ref is not None:
        group = next((item for item in groups if item["group_key"] == ref), None)
        if group is None:
            raise NotFoundError(f"Unknown exploration_inbox group: {ref}")
        members = group["members_full"]
        signature = {"collection": "exploration_inbox", "generation": generation_row["generation"],
                     "group": ref, "scope_id": group["scope_id"],
                     "members": hashlib.sha256(_encoded(members).encode()).hexdigest()}
        offset = _validate_cursor(cursor, signature)
        page = members[offset:offset + limit]
        end = offset + len(page)
        rendered = {key: value for key, value in group.items() if key != "members_full"}
        rendered.update(result_ids=page, members_omitted=max(0, len(members) - end),
                        members_complete=end >= len(members))
        return {"available": True, "group": rendered, "items": page, "total": len(members),
                "pending_groups_total": pending_groups_total,
                "cursor": {**signature, "offset": end} if end < len(members) else None,
                "semantics": "exploration-inbox-v1"}
    signature = {"collection": "exploration_inbox", "generation": generation_row["generation"],
                 "scope_id": scope_id, "scope_refs": [item["ref"] for item in scopes],
                 "groups": hashlib.sha256(_encoded(groups).encode()).hexdigest()}
    offset = _validate_cursor(cursor, signature)
    items = [{key: value for key, value in group.items() if key != "members_full"}
             for group in groups[offset:offset + limit]]
    end = offset + len(items)
    return {"available": True, "items": items, "total": len(groups),
            "pending_groups_total": pending_groups_total,
            "cursor": {**signature, "offset": end} if end < len(groups) else None,
            "semantics": "exploration-inbox-v1"}


def _validate_cursor(cursor, signature):
    if cursor is None:
        return 0
    if not isinstance(cursor, dict):
        raise ValidationError("Exploration cursor must be an object")
    if any(cursor.get(key) != value for key, value in signature.items()):
        raise ConflictError("read_view_changed: refresh exploration")
    offset = cursor.get("offset")
    if type(offset) is not int or offset < 0:
        raise ValidationError("Invalid exploration cursor offset")
    return offset
