"""Evidence pages share a read view even when the ledger changes."""

import json

from auto_research.native_store import NativeStore
from test_context_policy import completed
from test_opencode_host import Host


def read_all(host, first):
    pages = [first["chunk"]]
    cursor = first["cursor"]
    while cursor:
        page = host.call("query", collection="evidence", cursor=cursor, limit=127)
        assert page["basis_digest"] == first["basis_digest"]
        pages.append(page["chunk"])
        cursor = page["cursor"]
    return json.loads("".join(pages))


def test_pages_pin_versions_and_materials_in_one_view(completed):
    host, store, _, _, claim, _ = completed
    first = host.call("query", collection="evidence", refs=[claim["ref"]], limit=127)
    assert first["cursor"]
    store.revise_knowledge({"ref": claim["ref"], "expected_revision": 1, "changes": {"statement": "Revised"},
                            "reason": "clarification", "affected_scope_mode": "none", "change_kind": "reword"}, "revise-evidence")
    old = read_all(host, first)
    assert old["records"][0]["pinned"]["ref"] == claim["ref"]
    assert old["records"][0]["current"]["ref"] == claim["ref"]
    assert any(item["ref"].startswith("pub/") for item in old["materials"])
    fresh_first = host.call("query", collection="evidence", refs=[claim["ref"]], limit=127)
    fresh = read_all(host, fresh_first)
    assert fresh_first["basis_digest"] != first["basis_digest"]
    assert fresh["records"][0]["current"]["revision"] == 2
    assert fresh["raw_material_read"] == "not_assessed"


def test_missing_reference_is_a_limitation_not_a_pass(tmp_path):
    host = Host(tmp_path)
    host.open()
    first = host.call("query", collection="evidence", refs=["knowledge/K-404@1"], limit=127)
    view = read_all(host, first)
    assert not first["graph_complete"]
    assert len(view["limitations"]) == 1
    assert view["limitations"][0]["reason"] == "knowledge_missing"
    assert view["limitations"][0]["ref"] == "knowledge/K-404@1"
    assert view["records"] == []


def test_retracted_ancestor_without_impact_is_visible(completed):
    host, store, _, _, claim, _ = completed
    withdrawn = store.revise_knowledge({"ref": claim["ref"], "expected_revision": 1,
        "changes": {"status": "retracted"}, "reason": "withdrawn", "change_kind": "retract",
        "affected_scope_mode": "versions", "affected_scope": [claim["ref"]]}, "withdraw-evidence")
    citer = store.record_knowledge({"kind": "claim", "statement": "Uses withdrawn result",
                                   "evidence_refs": [withdrawn["ref"]]}, "citer-evidence")
    first = host.call("query", collection="evidence", refs=[citer["ref"]], limit=127)
    view = read_all(host, first)
    assert {item["ref"] for item in view["records"]} >= {citer["ref"], withdrawn["ref"]}
    assert any(risk["kind"] == "reference" and risk["target"] == citer["ref"]
               and risk["detail"]["reason"] == "retracted_ref" for risk in view["risks"])


def test_expired_snapshot_is_not_rebuilt_silently(tmp_path):
    host = Host(tmp_path)
    host.open()
    first = host.call("query", collection="evidence", refs=[], limit=1)
    host.service.evidence._snapshots.clear()
    assert "evidence_snapshot_expired" in host.error("query", collection="evidence", cursor=first["cursor"])


def test_selected_report_with_no_declared_claims_preserves_unknown_coverage(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('focus', node_id=None, role='planner', mode='manual')
    publication = host.call('publish', status='complete', summary='report', gaps=[], knowledge_refs=[],
        items=[{'item_id':'report','kind':'report','content':{'text':'An unlinked research report'}}])
    ref = f"pub/{publication['publication_id']}#report"
    view = read_all(host, host.call('query', collection='evidence', refs=[ref], limit=127))
    assert view['publication_checks'][0]['result']['status'] == 'coverage unknown'
    assert any(item['ref'] == ref and item['reason'] == 'publication_coverage_incomplete'
               for item in view['limitations'])
