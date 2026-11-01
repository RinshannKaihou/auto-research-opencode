"""Decisions, tasks and delivered results survive duplicate operations."""

import sqlite3

from test_exploration_foundation import scope_and_route
from test_opencode_host import Host


def decided(host):
    scope, route = scope_and_route(host)
    view = host.call("query", collection="evidence", refs=[])
    fields = {"scope_ref": scope["ref"], "snapshot_id": view["snapshot_id"],
        "route_refs": [route["ref"]], "result_ids": [], "action": "probe", "reason": "Resolve the tail assumption",
        "next_work": {"route_refs": [route["ref"]], "question": "Does the tail differ?", "why_now": "Compare routes",
                      "plan": "Measure the tail", "root_reason": "First bounded probe"}}
    decision = host.call("exploration_decide", model_call=True, fields=fields, operation_id="decision-once")
    return scope, route, fields, decision


def test_decision_materialization_is_atomic_and_idempotent(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call("workflow", action="run", fields={"state": "running"})
    _, _, fields, decision = decided(host)
    with sqlite3.connect(host.root / ".research/state.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM workflow_intents WHERE kind='exploration_work'").fetchone()[0] == 1
    first = host.call("exploration_materialize", intent_id=decision["intent_id"], operation_id="materialize-once")
    assert first["state"] == "materialized"
    assert host.call("exploration_materialize", intent_id=decision["intent_id"], operation_id="materialize-once") == first
    # Successful decision replay still works after the temporary evidence cache is lost.
    host.service.evidence._snapshots.clear()
    assert host.call("exploration_decide", model_call=True, fields=fields, operation_id="decision-once") == decision
    with sqlite3.connect(host.root / ".research/state.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM exploration_tasks").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM exploration_bindings").fetchone()[0] == 1
    assert host.call("exploration_task_check", task_id=first["task_id"])["allowed"]


def test_changed_route_invalidates_unstarted_intent(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call("workflow", action="run", fields={"state": "running"})
    scope, route, _, decision = decided(host)
    host.call("exploration_route", model_call=True, fields={"scope_ref": scope["ref"], "route_ref": decision['route_refs'][0],
              "reason": "Changed assumption", "changes": {"critical_assumption": "Different workload"}})
    result = host.call("exploration_materialize", intent_id=decision["intent_id"])
    assert result["state"] == "stale"
    with sqlite3.connect(host.root / ".research/state.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0


def test_publication_and_finish_are_distinct_pending_results(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call("workflow", action="run", fields={"state": "running"})
    _, _, _, decision = decided(host)
    work = host.call("exploration_materialize", intent_id=decision["intent_id"])
    host.call("workflow", action="task_session", fields={"task_id": work["task_id"], "session_id": "ses_node"})
    host.call("open", session="ses_node", root=str(host.root), cwd=str(host.root), session_role="node_core", node_id=work["node_id"])
    host.call("focus", session="ses_node", node_id=work["node_id"], role="core", mode="auto")
    host.call("publish", session="ses_node", status="partial", summary="No tail benefit", gaps=[],
              items=[{"item_id": "result", "kind": "text", "content": {"result": "mixed"}}], knowledge_refs=[])
    host.call("finish", session="ses_node", state="finished", details={"reason": "No tail benefit"})
    result = host.call("query", collection="exploration_results")
    assert [item["kind"] for item in result["items"]] == ["published", "finished"]
    assert all(item["handled_by_decision"] is None for item in result["items"])


def test_budget_exhaustion_preserves_unmaterialized_work(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('workflow', action='run', fields={'state':'running'})
    host.call('runtime_budget_set', limits={'max_total_nodes':1}, reason='bounded project')
    host.call('propose', question='Already spent', why_now='before', plan='check', root_reason='existing work')
    _, _, _, decision = decided(host)
    waiting = host.call('exploration_materialize',intent_id=decision['intent_id'])
    assert waiting['state'] == 'waiting_budget'
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM exploration_tasks').fetchone()[0] == 0
    host.call('runtime_budget_set',limits={'max_total_nodes':2},reason='explicit increase')
    work = host.call('exploration_materialize',intent_id=decision['intent_id'])
    assert work['state'] == 'materialized'
    assert host.call('exploration_materialize',intent_id=decision['intent_id'])['task_id'] == work['task_id']


def test_launch_claim_atomically_rechecks_the_decision_basis(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('workflow',action='run',fields={'state':'running'})
    scope, _, _, decision = decided(host)
    work = host.call('exploration_materialize',intent_id=decision['intent_id'])
    assert host.call('exploration_task_check',task_id=work['task_id'])['allowed']
    host.call('exploration_route',model_call=True,fields={'scope_ref':scope['ref'],'route_ref':decision['route_refs'][0],
        'reason':'the comparison changed','changes':{'critical_assumption':'new workload'}})
    claim = host.call('node_launch_claim',task_id=work['task_id'],concurrency=1)
    assert not claim['claimed'] and claim['reason'] == 'exploration_basis_changed'
    with sqlite3.connect(host.root / '.research/state.sqlite3') as db:
        assert db.execute("SELECT state FROM exploration_tasks WHERE task_id=?",(work['task_id'],)).fetchone()[0] == 'cancelled'
        assert db.execute("SELECT COUNT(*) FROM workflow_intents WHERE kind='node_launch'").fetchone()[0] == 0


def test_pending_intent_can_be_superseded_by_an_explicit_stop_decision(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call('workflow',action='run',fields={'state':'running'})
    scope, _, _, decision = decided(host)
    current = host.call('query',collection='evidence',refs=[])
    stopped = host.call('exploration_decide',model_call=True,fields={'scope_ref':scope['ref'],
        'snapshot_id':current['snapshot_id'],'route_refs':decision['route_refs'],'result_ids':[],
        'action':'propose_stop','reason':'The goal changed before execution',
        'supersede_intent_ids':[decision['intent_id']]})
    assert stopped['intent_id'] is None
    assert host.call('exploration_pending') == []
    intents = host.call('query',collection='exploration_intents')
    assert intents['items'][0]['state'] == 'superseded'
