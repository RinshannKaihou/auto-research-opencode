#!/usr/bin/env python3
"""Reproduce three current contract gaps without touching existing research runs.

This is a planning-evidence probe, not a product regression test. It imports the
repository's existing Host fixture and writes research records only inside
TemporaryDirectory instances. Its sole persistent output is JSON written to
stdout; redirect that output to an evidence file if desired. It makes no model
or network calls and changes neither product code nor the test suite.

Run from any directory with a Python environment containing pytest:
    python3 /path/to/repository/docs/evidence/remediation-plan-20261031/probe_current_contracts.py

The probes report observed behavior rather than asserting that today's gaps
must remain accepted. After repair, accepted flags should change as appropriate.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


REPOSITORY = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPOSITORY / "src"), str(REPOSITORY / "tests")]

from test_opencode_host import Host  # noqa: E402
from test_p0_contracts import checkpoint, contract, node, published  # noqa: E402


def response_observation(response: dict) -> dict:
    """Keep results structured and omit temporary paths or frozen contents."""
    if response["ok"]:
        return {"accepted": True}
    return {"accepted": False, "error": response["error"]}


def checkpoint_without_base() -> dict:
    with TemporaryDirectory(prefix="remediation-checkpoint-") as folder:
        host = Host(Path(folder))
        host.open()
        first = checkpoint(host, state={"version": "first"}, expected_revision=0)
        # The caller really observed revision 1 before another write advanced it.
        observed = host.call("memory_context", profile="opencode-auto")["text"]
        newer = checkpoint(host, state={"version": "newer"}, expected_revision=1)
        response = host.raw(
            "memory_write",
            model_call=True,
            action="checkpoint",
            fields={"state": {"based_on_revision": first["revision"]}},
        )
        return {
            "name": "checkpoint_without_base_after_newer_write",
            "observed_revision": first["revision"],
            "context_was_read": bool(observed),
            "head_before_stale_write": newer["revision"],
            "expected_revision_supplied": False,
            **response_observation(response),
            "appended_revision": response.get("value", {}).get("revision"),
        }


def unknown_coverage_conclusion() -> dict:
    with TemporaryDirectory(prefix="remediation-conclusion-") as folder:
        host = Host(Path(folder))
        host.open()
        publication_id = published(host)
        check = host.call("publication_check", publication_id=publication_id)
        final_ref = f"pub/{publication_id}#report"
        response = host.raw(
            "conclude",
            fields=contract(
                publication_id,
                outcome="answered",
                gaps=[],
                review={"status": "reviewed", "refs": [final_ref], "limitations": []},
            ),
        )
        value = response.get("value", {})
        return {
            "name": "empty_knowledge_coverage_and_final_as_review_ref",
            "declared_knowledge_refs": check["check_scope"]["declared_refs"],
            "publication_check_status": check["result"]["status"],
            "publication_check_targets_complete": check["targets_complete"],
            "requested_outcome": "answered",
            "requested_review_status": "reviewed",
            "review_ref_equals_final_ref": True,
            **response_observation(response),
            "stored_outcome": value.get("outcome"),
            "stored_review_status": value.get("review", {}).get("status"),
        }


def completed_node_finding_visibility() -> dict:
    with TemporaryDirectory(prefix="remediation-finding-") as folder:
        host = Host(Path(folder))
        host.open()
        host.call("workflow", action="run", fields={"state": "running"})
        node_id, session_id, task_id = node(host)
        source = host.call(
            "publish", session=session_id, model_call=True,
            status="complete", summary="Synthetic observation source", gaps=[],
            items=[{"item_id": "data", "kind": "text", "content": {"measured": 7}}],
            knowledge_refs=[],
        )
        statement = "PROBE_FINDING_ORDINARY_NODE_CLAIM: the synthetic measurement is 7."
        claim = host.call(
            "memory_write", session=session_id, model_call=True, action="record",
            fields={
                "kind": "claim", "statement": statement,
                "evidence_refs": [f"pub/{source['publication_id']}#data"],
                "scope": {"fixture": "synthetic"},
                "conditions": {"measurement_case": "one"},
            },
        )
        report = host.call(
            "publish", session=session_id, model_call=True,
            status="complete", summary="Node result published", gaps=[],
            items=[{"item_id": "report", "kind": "text", "content": {"text": statement}}],
            knowledge_refs=[claim["ref"]],
        )
        host.call("finish", session=session_id, state="finished", details={"reason": "Probe complete"})
        # Match the ledger operations used by Engine.completeNode.
        host.call("workflow", session=session_id, action="task_state",
                  fields={"task_id": task_id, "state": "finished"})
        host.call("workflow", session=session_id, action="session",
                  fields={"pause_reason": "segment_complete"})
        context = host.call("memory_context", profile="opencode-auto", max_chars=12000)
        sections = {}
        for section in context["text"].split("## ")[1:]:
            name, content = section.split("\n", 1)
            sections[name] = json.loads(content.strip())
        query = host.call("query", ref=claim["ref"])
        publication_check = host.call("publication_check", publication_id=report["publication_id"])
        task = host.call("query", ref=task_id)
        return {
            "name": "completed_node_claim_pinned_but_missing_from_coordinator_context",
            "node_id": node_id,
            "task_state": task["value"]["state"],
            "claim_ref": claim["ref"],
            "claim_kind": claim["kind"],
            "claim_revision": claim["revision"],
            "claim_node_id": claim["node_id"],
            "claim_pinned_by_publication": claim["ref"] in publication_check["check_scope"]["declared_refs"],
            "claim_statement_visible_in_context": statement in context["text"],
            "claim_ref_visible_in_knowledge_section": claim["ref"] in json.dumps(sections.get("knowledge", {})),
            "query_returns_full_claim": query["value"]["statement"] == statement,
            "query_returns_scope_and_conditions": bool(query["value"].get("scope")) and bool(query["value"].get("conditions")),
            "context_chars": len(context["text"]),
            "context_omitted_sections": context.get("omitted", []),
        }


def main() -> None:
    print(json.dumps({
        "evidence_kind": "synthetic_local_contract_probes",
        "writes_to_existing_research": False,
        "model_calls_executed": False,
        "probes": [checkpoint_without_base(), unknown_coverage_conclusion(), completed_node_finding_visibility()],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
