"""Orchestrator-driven exploration activation, proactive user asking, and auto-resume."""

from auto_research.exploration_store import contract_versions
from auto_research.native_store import NativeStore
from test_context_policy import sections
from test_opencode_host import Host, ledger_counts


def test_default_exploration_mode_is_auto_and_keeps_legacy_until_enabled(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call("workflow", action="run", fields={"state": "running"})

    policy = host.call("exploration_policy", mode=None)
    assert policy == {
        "generation": 0,
        "exploration_mode": "auto",
        "exploration_policy": "off",
        "context_policy": "memory-v2",
        "conclusion_contract": "legacy-v1",
    }
    store = NativeStore(host.root)
    with store._read() as db:
        versions = contract_versions(store, db, host.host_id, "ses_main")
    assert versions["exploration_mode"] == "auto"
    assert versions["exploration_policy"] == "off"
    summary = host.call("project_read", root=str(host.root), view="summary")
    assert summary["exploration"] is None

    ctx = host.call("memory_context", max_chars=12000, profile="opencode-auto")
    assert ctx["exploration_mode"] == "auto"
    assert ctx["exploration_policy"] == "off"
    blocks = sections(ctx)
    assert "Exploration control is auto" in blocks["role_instructions"]
    assert "research_exploration (action=enable)" in blocks["role_instructions"]
    assert "research_conclude" in blocks["role_instructions"]
    assert "pending_question" not in blocks["project"]

    # Direct single-path delivery concludes under legacy-v1 without exploration overhead.
    (host.root / "report.md").write_text("# Direct Result\n")
    pub = host.call(
        "publish",
        model_call=True,
        status="complete",
        summary="Direct answer",
        gaps=[],
        items=[{"item_id": "report", "kind": "report", "source_path": "report.md"}],
        knowledge_refs=[],
    )
    concluded = host.call(
        "conclude",
        model_call=True,
        fields={
            "summary": "Direct answer delivered",
            "final_ref": f"pub/{pub['publication_id']}#report",
            "outcome": "answered",
            "gaps": [],
            "review": {"status": "unreviewed", "refs": [], "limitations": ["Single-path check"]},
        },
    )
    assert concluded["outcome"] == "answered"


def test_orchestrator_enable_activates_contracts_creates_scope_and_counts_as_progress(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call("workflow", action="run", fields={"state": "running"})

    # Enable without scope first counts as meaningful progress in runtime_turn_finished.
    host.call("runtime_execution_start", execution_token="exec-1", event_seq=1)
    enabled = host.call(
        "exploration_orchestrate",
        model_call=True,
        operation_id="ses_main:enable-1",
        fields={
            "action": "enable",
            "reason": "Two competing candidate mechanisms must be compared before full effort",
            "scope": {
                "objective": "Select indexing mechanism",
                "task_kind": "selection",
            },
        },
    )
    assert enabled["action"] == "enable"
    assert enabled["exploration_mode"] == "auto"
    assert enabled["exploration_policy"] == "exploration-v1"
    assert enabled["context_policy"] == "memory-r1-v1"
    assert enabled["conclusion_contract"] == "evidence-v1"
    assert enabled["scope"]["scope_id"] == "ES-001"
    assert enabled["scope"]["ref"] == "explore/scope/ES-001@1"
    summary = host.call("project_read", root=str(host.root), view="summary")
    assert summary["exploration"]["mode"] == "auto"
    assert summary["exploration"]["policy"] == "exploration-v1"

    # Idempotent replay with same operation_id returns identical result without creating ES-002.
    replayed = host.call(
        "exploration_orchestrate",
        model_call=True,
        operation_id="ses_main:enable-1",
        fields={
            "action": "enable",
            "reason": "Two competing candidate mechanisms must be compared before full effort",
            "scope": {
                "objective": "Select indexing mechanism",
                "task_kind": "selection",
            },
        },
    )
    assert replayed["scope"]["ref"] == "explore/scope/ES-001@1"
    assert host.call("query", collection="exploration_scopes")["total"] == 1

    host.call("runtime_execution_end", turn="turn-1", outcome="succeeded", execution_token="exec-1", event_seq=2)
    progress = host.call("runtime_turn_finished", turn="turn-1")
    assert progress["meaningful"] is True
    assert progress["idle_turns"] == 0

    ctx = host.call("memory_context", policy="memory-r1-v1", max_chars=12000, profile="opencode-auto")
    blocks = sections(ctx)
    assert "Exploration is active" in blocks["role_instructions"]
    assert "research_prepare_conclusion" in blocks["role_instructions"]
    assert blocks["exploration"]["scopes_total"] == 1

    # Node executors may never call exploration_orchestrate.
    node = host.call(
        "propose",
        model_call=True,
        question="Q?",
        why_now="w",
        plan="p",
        root_reason="r",
    )
    host.call("open", session="ses_node", root=str(host.root), cwd=str(host.root),
              session_role="node_core", node_id=node["node_id"])
    err = host.error(
        "exploration_orchestrate",
        session="ses_node",
        model_call=True,
        fields={"action": "enable", "reason": "node try"},
    )
    assert "main" in err.lower() or "coordinator" in err.lower()


def test_user_policy_off_blocks_orchestrator_enable_and_ask(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call("workflow", action="run", fields={"state": "running"})

    off = host.call("exploration_policy", mode="off")
    assert off["exploration_mode"] == "off"
    assert off["exploration_policy"] == "off"

    blocks = sections(host.call("memory_context", max_chars=12000, profile="opencode-auto"))
    assert "Exploration control is auto" not in blocks["role_instructions"]
    assert "Exploration is active" not in blocks["role_instructions"]

    err_enable = host.error(
        "exploration_orchestrate",
        model_call=True,
        fields={"action": "enable", "reason": "Try enabling despite off"},
    )
    assert "disabled by user policy" in err_enable

    err_ask = host.error(
        "exploration_orchestrate",
        model_call=True,
        fields={"action": "ask", "reason": "Try asking despite off", "question": "Which path?"},
    )
    assert "disabled by user policy" in err_ask

    restored = host.call("exploration_policy", mode="auto")
    assert restored["exploration_mode"] == "auto"
    assert restored["exploration_policy"] == "off"
    res = host.call(
        "exploration_orchestrate",
        model_call=True,
        fields={"action": "enable", "reason": "Re-enabled under auto"},
    )
    assert res["exploration_policy"] == "exploration-v1"


def test_orchestrator_ask_pauses_awaiting_user_and_resumes_cleanly(tmp_path):
    host = Host(tmp_path)
    host.open()

    # Read-only fast-path when not awaiting user writes zero ledger rows.
    before_counts = ledger_counts(host.root)
    noop = host.call("runtime_resume_awaiting_user")
    assert noop == {"resumed": False, "reason": "not_awaiting_user"}
    assert ledger_counts(host.root) == before_counts

    # In manual mode, ask records the question without setting a running pause or sticky pending_question.
    manual_ask = host.call(
        "exploration_orchestrate",
        model_call=True,
        fields={
            "action": "ask",
            "reason": "Ambiguous evaluation metric",
            "question": "Optimize p99 latency or throughput?",
            "options": ["p99 latency", "Throughput"],
        },
    )
    assert manual_ask["paused"] is False
    assert manual_ask["state"] == "manual"
    manual_blocks_before = sections(host.call("memory_context", max_chars=12000, profile="opencode-auto"))
    assert "pending_question" not in manual_blocks_before["project"]
    host.call("focus", node_id=None, role="planner", mode="manual")
    host.call("note", body="用户指导：p99 latency", kind="correction")
    assert host.call("runtime_resume_awaiting_user") == {"resumed": False, "reason": "not_awaiting_user"}
    manual_blocks_after = sections(host.call("memory_context", max_chars=12000, profile="opencode-auto"))
    assert "pending_question" not in manual_blocks_after["project"]

    # Start running, then ask during autonomous execution.
    host.call("workflow", action="run", fields={"state": "running"})
    host.call("runtime_execution_start", execution_token="exec-ask", event_seq=1)
    asked = host.call(
        "exploration_orchestrate",
        model_call=True,
        fields={
            "action": "ask",
            "reason": "Full benchmark costs 8 GPU-hours vs synthetic microbenchmark",
            "question": "Should we run the synthetic probe first or commit to the full benchmark?",
            "options": ["Synthetic probe first", "Full benchmark"],
        },
    )
    assert asked["paused"] is True
    assert asked["state"] == "awaiting_user"
    status = host.call("status")
    assert status["workflow"]["run"]["state"] == "running"
    assert status["workflow"]["session"]["pause_reason"] == "awaiting_user"

    # Context reflects awaiting_user instructions and pending_question.
    paused_blocks = sections(host.call("memory_context", max_chars=12000, profile="opencode-auto"))
    assert paused_blocks["project"]["pause_reason"] == "awaiting_user"
    assert paused_blocks["project"]["pending_question"]["question"].startswith("Should we run")
    assert "waiting for the user's reply" in paused_blocks["role_instructions"]
    assert "resumes automatically" in paused_blocks["control_instructions"]

    # Turn finishing after ask counts as meaningful progress.
    host.call("runtime_execution_end", turn="turn-ask", outcome="succeeded", execution_token="exec-ask", event_seq=2)
    progress = host.call("runtime_turn_finished", turn="turn-ask")
    assert progress["meaningful"] is True

    # If the user explicitly paused the project (/research pause), ordinary chat does NOT auto-resume.
    host.call("workflow", action="run", fields={"state": "paused"})
    blocked = host.call("runtime_resume_awaiting_user")
    assert blocked == {"resumed": False, "reason": "not_awaiting_user"}

    # Restore running with session still awaiting_user, then verify user-prompt auto-resume.
    host.call("workflow", action="run", fields={"state": "running"})
    host.call("workflow", action="session", fields={"pause_reason": "awaiting_user"})
    resumed = host.call("runtime_resume_awaiting_user")
    assert resumed["resumed"] is True
    assert resumed["prior_question"]["question"].startswith("Should we run")
    after = host.call("status")
    assert after["workflow"]["session"]["pause_reason"] is None
    resumed_blocks = sections(host.call("memory_context", max_chars=12000, profile="opencode-auto"))
    assert "pending_question" not in resumed_blocks["project"]


def test_auto_mode_resets_per_generation_while_on_mode_persists(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call("workflow", action="run", fields={"state": "running"})

    # Generation 0: orchestrator enables exploration under auto mode, then run stops.
    host.call(
        "exploration_orchestrate",
        model_call=True,
        fields={
            "action": "enable",
            "reason": "Compare two designs in generation 0",
            "scope": {"objective": "Gen 0 choice", "task_kind": "selection"},
        },
    )
    assert host.call("exploration_policy", mode=None)["exploration_policy"] == "exploration-v1"

    # Switching mode='auto' mid-generation when scopes already exist preserves active generation scopes.
    mid = host.call("exploration_policy", mode="auto")
    assert mid["exploration_mode"] == "auto"
    assert mid["exploration_policy"] == "exploration-v1"

    host.call("workflow", action="run", fields={"state": "stopped"})

    # Generation 1 under auto mode starts fresh with exploration_policy='off', memory-v2, and legacy-v1.
    gen1 = host.call("workflow", action="run", fields={"state": "running", "new_generation": True})
    assert gen1["run"]["generation"] == 1
    pol1 = host.call("exploration_policy", mode=None)
    assert pol1["exploration_mode"] == "auto"
    assert pol1["exploration_policy"] == "off"
    assert pol1["context_policy"] == "memory-v2"
    assert pol1["conclusion_contract"] == "legacy-v1"
    assert host.call("context_policy")["policy_version"] == "memory-v2"

    # Standalone host context_policy='memory-r1-v1' probe is preserved across new_generation when exploration stayed off.
    host.call("context_policy", policy="memory-r1-v1")
    host.call("workflow", action="run", fields={"state": "stopped"})
    host.call("workflow", action="run", fields={"state": "running", "new_generation": True})
    assert host.call("context_policy")["policy_version"] == "memory-r1-v1"
    host.call("context_policy", policy="memory-v2")

    # Explicit user 'on' mode persists across generations.
    host.call("exploration_policy", mode="on")
    host.call("workflow", action="run", fields={"state": "stopped"})
    gen2 = host.call("workflow", action="run", fields={"state": "running", "new_generation": True})
    assert gen2["run"]["generation"] == 3
    pol2 = host.call("exploration_policy", mode=None)
    assert pol2["exploration_mode"] == "on"
    assert pol2["exploration_policy"] == "exploration-v1"
    assert pol2["context_policy"] == "memory-r1-v1"
    assert pol2["conclusion_contract"] == "evidence-v1"


def test_legacy_config_without_exploration_mode_preserves_off_and_on(tmp_path):
    import json
    import sqlite3

    host = Host(tmp_path)
    host.open()
    host.call("workflow", action="run", fields={"state": "running"})

    # Simulate an upgraded v0.2.7 project where the user had explicitly run `/research exploration off`
    # (persisted `{"exploration_policy": "off"}` with no `exploration_mode`).
    with sqlite3.connect(host.root / ".research/state.sqlite3") as db:
        db.execute("UPDATE project SET config=?", (json.dumps({"exploration_policy": "off"}),))
        db.commit()

    legacy_off = host.call("exploration_policy", mode=None)
    assert legacy_off["exploration_mode"] == "off"
    assert legacy_off["exploration_policy"] == "off"
    store = NativeStore(host.root)
    with store._read() as db:
        assert contract_versions(store, db, host.host_id, "ses_main")["exploration_mode"] == "off"
    ctx_off = host.call("memory_context", max_chars=12000, profile="opencode-auto")
    assert ctx_off["exploration_mode"] == "off"
    assert "Exploration control is auto" not in sections(ctx_off)["role_instructions"]
    assert "disabled by user policy" in host.error(
        "exploration_orchestrate",
        model_call=True,
        fields={"action": "enable", "reason": "Must stay blocked on upgraded off project"},
    )
    assert "disabled by user policy" in host.error(
        "exploration_orchestrate",
        model_call=True,
        fields={"action": "ask", "reason": "Must stay blocked", "question": "Proceed?"},
    )

    # Simulate an upgraded pre-v0.2.8 project with empty `{}` config: also stays off conservatively.
    with sqlite3.connect(host.root / ".research/state.sqlite3") as db:
        db.execute("UPDATE project SET config='{}'")
        db.commit()
    assert host.call("exploration_policy", mode=None)["exploration_mode"] == "off"

    # Simulate an upgraded v0.2.7 project where exploration was explicitly enabled.
    with sqlite3.connect(host.root / ".research/state.sqlite3") as db:
        db.execute(
            "UPDATE project SET config=?",
            (json.dumps({"exploration_policy": "exploration-v1", "context_policy": "memory-r1-v1"}),),
        )
        db.commit()
    legacy_on = host.call("exploration_policy", mode=None)
    assert legacy_on["exploration_mode"] == "on"
    assert legacy_on["exploration_policy"] == "exploration-v1"

    # Simulate an upgraded v0.2.7 project where host set `context_policy='memory-r1-v1'` WITHOUT exploration
    # (no `context_policy_origin`): switching to `auto` and starting a new generation preserves `memory-r1-v1`.
    with sqlite3.connect(host.root / ".research/state.sqlite3") as db:
        db.execute("UPDATE project SET config=?", (json.dumps({"context_policy": "memory-r1-v1"}),))
        db.commit()
    host.call("exploration_policy", mode="auto")
    host.call("workflow", action="run", fields={"state": "stopped"})
    host.call("workflow", action="run", fields={"state": "running", "new_generation": True})
    assert host.call("context_policy")["policy_version"] == "memory-r1-v1"


def test_waiting_session_rejects_exploration_orchestrate_without_misleading_auto_hint(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call("workflow", action="run", fields={"state": "running"})
    node = host.call(
        "propose",
        model_call=True,
        question="Background node question",
        why_now="Need initial probe",
        plan="Run background task",
        root_reason="Initial work",
    )
    task = host.call("workflow", action="task", fields={"node_id": node["node_id"]})
    host.call("workflow", action="session", fields={"pause_reason": "wait", "waiting": [task["task_id"]]})

    for action, extra in (
        ("enable", {}),
        ("ask", {"question": "Interrupt wait?"}),
    ):
        err = host.error(
            "exploration_orchestrate",
            model_call=True,
            fields={"action": action, "reason": "Called while waiting", **extra},
        )
        assert "waiting on dispatched node tasks" in err
        assert "/research auto" not in err
