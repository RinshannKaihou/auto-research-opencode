"""Service additions for the OpenCode host: manual node work, read-only
browsing by root, host-scoped session lookup, one-way takeover, the context
profiles and node specialists."""

import sqlite3

from auto_research.native_store import NativeStore
from auto_research.service import NativeService


class Host:
    """Drives the private service the way a host adapter does."""

    def __init__(self, tmp_path, host_id="opencode"):
        self.root = tmp_path / "project"
        self.root.mkdir(exist_ok=True)
        self.service = NativeService(tmp_path / "registry.sqlite3")
        self.host_id = host_id
        self.count = 0

    def raw(self, method, session="ses_main", host=None, **fields):
        self.count += 1
        request = {
            "transport_id": f"t{self.count}",
            "operation_id": f"{session}:op{self.count}",
            "host_id": host or self.host_id,
            "session_id": session,
            "method": method,
            **fields,
        }
        try:
            return self.service.handle(request)
        except Exception as exc:  # serve() turns these into error responses
            return {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}}

    def call(self, method, session="ses_main", host=None, **fields):
        response = self.raw(method, session, host, **fields)
        assert response["ok"] is True, response
        return response["value"]

    def error(self, method, session="ses_main", host=None, **fields):
        response = self.raw(method, session, host, **fields)
        assert response["ok"] is False, response
        return response["error"]["message"]

    def open(self, session="ses_main", host=None):
        return self.call("open", session, host, root=str(self.root), cwd=str(self.root), goal="Goal")


def ledger_counts(root):
    with sqlite3.connect(root / ".research" / "state.sqlite3") as db:
        return tuple(
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("requests", "events", "attempts", "associations")
        )


def test_manual_node_work_attaches_results_to_the_node(tmp_path):
    host = Host(tmp_path)
    host.open()
    node = host.call(
        "propose", model_call=True, question="Does X hold?", why_now="start",
        plan="measure X", root_reason="first question",
    )
    host.call("note", model_call=True, body="planning", kind="progress")
    planning = host.call("status")["attempt"]
    assert planning["node_id"] is None
    host.call("finish", state="finished", details={"reason": "switch-to-node-work"})
    host.call("focus", node_id=node["node_id"], role="core", mode="manual")
    (host.root / "report.md").write_text("# Result\n")
    publication = host.call(
        "publish", model_call=True, status="partial", summary="result", gaps=[],
        items=[{"item_id": "report", "kind": "report", "source_path": "report.md"}],
        knowledge_refs=[],
    )
    ref = f"pub/{publication['publication_id']}#report"
    host.call("memory_write", model_call=True, action="checkpoint", fields={"state": {"next": "x"}})
    host.call(
        "consume", model_call=True, source_ref=ref, use="baseline", relation_type="context"
    )
    page = host.call("history_page", collection="publications")
    assert page["items"][0]["node_id"] == node["node_id"]
    assert host.call("status")["attempt"]["node_id"] == node["node_id"]


def test_opencode_profile_replaces_only_role_and_control(tmp_path):
    host = Host(tmp_path)
    host.open()
    default = host.call("memory_context", max_chars=12000)["text"]
    manual = host.call("memory_context", max_chars=12000, profile="opencode-manual")["text"]
    for phrase in ("/research auto", "research_verify_specialist", "research_finish"):
        assert phrase in default
        assert phrase not in manual
    assert "/research work" in manual
    assert "Unknown context profile" in host.error("memory_context", profile="dsh-auto")


def test_project_read_browses_by_root_without_writing(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call(
        "propose", model_call=True, question="Q", why_now="now", plan="p", root_reason="r"
    )
    (host.root / "report.md").write_text("# Report\nbody\n")
    publication = host.call(
        "publish", model_call=True, status="complete", summary="done", gaps=[],
        items=[{"item_id": "report", "kind": "report", "source_path": "report.md"}],
        knowledge_refs=[],
    )
    ref = f"pub/{publication['publication_id']}#report"
    before = ledger_counts(host.root)
    reader = NativeService(tmp_path / "other-registry.sqlite3")

    def handle(request):
        try:
            return reader.handle(request)
        except Exception as exc:
            return {"ok": False, "error": {"message": str(exc)}}

    def read(view, **fields):
        response = handle(
            {"transport_id": view, "method": "project_read", "root": str(host.root),
             "view": view, **fields}
        )
        assert response["ok"] is True, response
        return response["value"]

    summary = read("summary")
    assert summary["project"]["goal"] == "Goal"
    assert summary["knowledge_kinds"] == sorted(
        ["observation", "hypothesis", "lesson", "decision", "open_question", "claim"]
    )
    assert "verified" not in summary["knowledge_statuses"]
    assert read("page", collection="nodes")["items"][0]["node_id"] == "X-001"
    assert read("knowledge")["items"]
    assert read("reference_get", ref=ref)["kind"] == "publication-item"
    content = read("reference_content", ref=ref)
    assert content["kind"] == "text"
    assert b"body" in __import__("base64").b64decode(content["chunk"])
    read("presentation")
    assert ledger_counts(host.root) == before

    empty = tmp_path / "empty"
    empty.mkdir()
    assert read("summary", root=str(empty))["project"] is None
    assert not (empty / ".research").exists()
    relative = handle({"transport_id": "r", "method": "project_read", "root": "rel",
                       "view": "summary"})
    assert relative["ok"] is False


def test_host_sessions_lists_only_that_hosts_live_sessions(tmp_path):
    host = Host(tmp_path)
    host.open()
    listed = host.call("host_sessions")
    assert [(row["session_id"], row["root"]) for row in listed] == [
        ("ses_main", str(host.root.resolve()))
    ]
    assert host.call("host_sessions", host="local") == []


def test_detach_succeeds_after_closing_the_open_segment(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call("note", model_call=True, body="opens a planning segment", kind="progress")
    assert "Finish the active work segment" in host.error("detach")
    host.call("finish", state="stopped", details={"reason": "detach"})
    host.call("workflow", action="session", fields={"detached": 1, "pause_reason": "detached"})
    host.call("detach")
    assert host.call("host_sessions") == []


def test_takeover_refuses_an_active_project(tmp_path):
    host = Host(tmp_path, host_id="local")
    host.open(session="dsh-main")
    host.call("workflow", session="dsh-main", action="run", fields={"state": "running"})
    message = host.error(
        "takeover_main", session="ses_new", host="opencode", root=str(host.root)
    )
    assert "stop the project" in message
    missing = tmp_path / "missing"
    missing.mkdir()
    assert "No research project" in host.error(
        "takeover_main", session="ses_new", host="opencode", root=str(missing)
    )
    assert not (missing / ".research").exists()


def test_takeover_retires_the_previous_main_one_way(tmp_path):
    host = Host(tmp_path, host_id="local")
    host.open(session="dsh-main")
    host.call("note", session="dsh-main", model_call=True, body="old work", kind="progress")
    state = host.call(
        "takeover_main", session="ses_new", host="opencode", root=str(host.root),
        cwd=str(host.root),
    )
    assert state["workflow"]["session"]["role"] == "main"
    assert state["workflow"]["run"]["main_session_id"] == "ses_new"
    assert state["workflow"]["run"]["state"] == "manual"
    with NativeStore(host.root)._read() as db:
        old = db.execute("SELECT * FROM attempts WHERE attempt_id='A-001'").fetchone()
        assert old["state"] == "stopped" and old["ended_at"]
        row = db.execute(
            "SELECT detached,pause_reason FROM workflow_sessions WHERE session_id='dsh-main'"
        ).fetchone()
        assert (row["detached"], row["pause_reason"]) == (1, "detached")
    assert "not associated" in host.error(
        "note", session="dsh-main", model_call=True, body="late write", kind="progress"
    )
    guidance = tmp_path / "guidance.md"
    guidance.write_text("# Method\n")
    host.call(
        "guidance_register", session="ses_new", host="opencode", path=str(guidance), version="v1"
    )
    host.call("note", session="ses_new", host="opencode", model_call=True, body="new", kind="progress")
    assert [row["session_id"] for row in host.call("host_sessions", host="opencode")] == ["ses_new"]


def test_opencode_auto_profile_gives_each_role_its_own_loop_duties(tmp_path):
    host = Host(tmp_path)
    host.open()
    main = host.call("memory_context", max_chars=12000, profile="opencode-auto")["text"]
    for phrase in ("research_dispatch", "research_wait", "research_conclude", "【Research 自动推进】"):
        assert phrase in main
    for phrase in ("research_verify_specialist", "/research work", "research_finish to end"):
        assert phrase not in main


def test_task_session_binds_the_host_minted_session_before_registration(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call("workflow", action="run", fields={"state": "running"})
    host.call("propose", model_call=True, question="Q", why_now="w", plan="p", root_reason="r")
    task = host.call("workflow", action="task", fields={"node_id": "X-001"})
    assert task["session_id"].startswith("research-")
    assert "not associated" in host.error(
        "workflow", session="ses_other", action="task_session",
        fields={"task_id": task["task_id"], "session_id": "ses_node"},
    )
    host.call("workflow", action="task_session", fields={"task_id": task["task_id"], "session_id": "ses_node"})
    host.call(
        "open", session="ses_node", root=str(host.root), cwd=str(host.root),
        session_role="node_core", node_id="X-001", context={},
    )
    node_view = host.call("memory_context", session="ses_node", max_chars=12000, profile="opencode-auto")["text"]
    assert "research_finish" in node_view and "workspace directory" in node_view
    assert "already registered" in host.error(
        "workflow", action="task_session", fields={"task_id": task["task_id"], "session_id": "ses_other"},
    )


def test_opencode_specialist_gets_its_own_role_and_task(tmp_path):
    host = Host(tmp_path)
    host.open()
    host.call("workflow", action="run", fields={"state": "running"})
    host.call("propose", model_call=True, question="Q", why_now="w", plan="p", root_reason="r")
    task = host.call("workflow", action="task", fields={"node_id": "X-001"})
    host.call("workflow", action="task_session", fields={"task_id": task["task_id"], "session_id": "ses_node"})
    host.call(
        "open", session="ses_node", root=str(host.root), cwd=str(host.root),
        session_role="node_core", node_id="X-001", context={},
    )
    host.call("focus", session="ses_node", node_id="X-001", role="core", mode="auto")
    node_view = host.call("memory_context", session="ses_node", max_chars=12000, profile="opencode-auto")["text"]
    assert "research_delegate" in node_view
    specialist = host.call(
        "specialist_create", session="ses_node", model_call=True,
        fields={"purpose": "domain", "label": "check", "prompt": "问题：复核结论", "inputs": [], "fanout_limit": 2},
    )
    host.call(
        "specialist_bind_child", session="ses_node", task_id=specialist["task_id"],
        child_session_id="ses_spec", node_id="X-001", cwd=str(host.root),
    )
    view = host.call("memory_context", session="ses_spec", max_chars=12000, profile="opencode-auto")["text"]
    assert "read-only specialist" in view and "问题：复核结论" in view
    assert "research_delegate" not in view
    assert "cannot modify" in host.error("note", session="ses_spec", model_call=True, body="x", kind="progress")
