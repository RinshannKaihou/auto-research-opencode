"""Read-only, aggregate-only audit of explicitly supplied local research roots.

Copies stable database/WAL bytes into a temporary directory before opening SQLite.
Does not open the source through NativeStore, migrate it, or export research text.
This measures saved contexts, not all host requests or historical counterfactuals.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from statistics import median
import tempfile


def source_files(database):
    return [database] + [
        candidate for suffix in ("-wal",)
        if (candidate := Path(str(database) + suffix)).exists()
    ]


def fingerprints(paths):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def blocks(body):
    matches = list(re.finditer(r"(?m)^## ([^\n]+)\n", body))
    result = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        chunk = body[match.start():end].rstrip("\n")
        payload = body[match.end():end].strip()
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            value = None
        result[match[1]] = {"characters": len(chunk), "payload": payload, "value": value}
    return result


def entries(value):
    if isinstance(value, dict):
        if "statement" in value and "ref" in value:
            yield value
        else:
            for item in value.values():
                yield from entries(item)
    elif isinstance(value, list):
        for item in value:
            yield from entries(item)


def context_metrics(rows):
    values = []
    previous = {}
    repeats = 0
    for row in rows:
        parsed = blocks(row["body"])
        block = lambda name: parsed.get(name, {"characters": 0, "payload": "", "value": None})
        knowledge = list(entries(block("knowledge")["value"]))
        refs = {v["ref"] for v in knowledge if re.fullmatch(r"knowledge/[^@]+@\d+", v["ref"])}
        pending = block("pending_review")
        key = (row["host_id"], row["session_id"])
        if pending["payload"] and previous.get(key) == pending["payload"]:
            repeats += 1
        previous[key] = pending["payload"]
        size = len(row["body"])
        values.append({
            "pending_share": pending["characters"] / size,
            "pending_plus_hints_share": (pending["characters"] + block("structure_hints")["characters"]) / size,
            "knowledge_share": block("knowledge")["characters"] / size,
            "knowledge_slots": len(knowledge),
            "syntactically_complete_unique_knowledge_refs": len(refs),
            "scope_present_slots": sum("scope" in v for v in knowledge),
            "conditions_present_slots": sum("conditions" in v for v in knowledge),
            "pending_total": (pending["value"] or {}).get("pending_total", 0),
        })
    return {
        "requests": len(values),
        "unique_packs": len({row["pack_id"] for row in rows}),
        "medians": {name: median(v[name] for v in values) for name in values[0]} if values else {},
        "latest": values[-1] if values else None,
        "consecutive_identical_nonempty_pending_exposures": repeats,
    }, values


def inspect(root):
    database = root / ".research" / "state.sqlite3"
    files = source_files(database)
    before = fingerprints(files)
    with tempfile.TemporaryDirectory(prefix="remediation-plan-audit-") as directory:
        target = Path(directory)
        for source in files:
            (target / source.name).write_bytes(source.read_bytes())
        if before != fingerprints(source_files(database)) or before != fingerprints([target / p.name for p in files]):
            raise RuntimeError(f"Source changed while copying; retry later: {root}")
        with sqlite3.connect(str(target / database.name)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            integrity = [r[0] for r in db.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                raise RuntimeError(f"Snapshot integrity failed: {root}: {integrity}")
            tables = ["nodes", "knowledge_entries", "knowledge_revisions", "project_conclusions",
                      "review_todos", "knowledge_impacts", "usage_observations", "turn_bindings",
                      "context_requests", "context_packs"]
            counts = {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in tables}
            rows = list(db.execute(
                "SELECT c.*,p.body FROM context_requests c JOIN context_packs p USING(pack_id) "
                "JOIN workflow_sessions w ON c.host_id=w.host_id AND c.session_id=w.session_id "
                "WHERE w.role='main' ORDER BY c.context_id"
            ))
            stats, values = context_metrics(rows)
            result = {
                "project": root.name,
                "root": str(root),
                "source_file_sha256": before,
                "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
                "max_event_id": db.execute("SELECT MAX(event_id) FROM events").fetchone()[0],
                "integrity": integrity,
                "counts": counts,
                "review_states": dict(db.execute("SELECT state,COUNT(*) FROM review_todos GROUP BY state")),
                "successful_mutations": dict(db.execute("SELECT operation,COUNT(*) FROM requests GROUP BY operation")),
                "coordinator_contexts": stats,
            }
    return result, values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", type=Path, nargs="+")
    args = parser.parse_args()
    projects, all_values = [], []
    for root in args.roots:
        project, values = inspect(root.resolve())
        projects.append(project)
        all_values.extend(values)
    totals = Counter()
    operations = Counter()
    for project in projects:
        totals.update(project["counts"])
        operations.update(project["successful_mutations"])
    report = {
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": {
            "source": "stable per-project database/WAL copy; no globally simultaneous snapshot",
            "selection": "saved context_requests joined to packs and matching host/session with role=main",
            "characters": "len(## heading + newline + JSON payload), excluding trailing newlines; denominator=len(body)",
            "slots": "rendered objects with statement/ref; includes duplicate groups and truncated refs",
            "reference_shape": "syntactically_complete_unique_knowledge_refs is regex-only; no per-reference resolution or database lookup",
            "risks": "critical-risk recall requires an independently annotated fixture; not inferred here",
            "limitations": ["context_record coverage is unknown", "requests contains committed mutations, not failed calls or all work", "saved text cannot reconstruct arbitrary historical database state"],
        },
        "totals": dict(totals),
        "successful_mutations": dict(operations),
        "coordinator_requests": len(all_values),
        "request_weighted_medians": {key: median(value[key] for value in all_values) for key in all_values[0]} if all_values else {},
        "projects": projects,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
