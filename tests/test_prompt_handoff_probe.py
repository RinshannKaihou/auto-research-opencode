"""The prompt-handoff probe must report where a queued prompt was delivered."""
import sys
from pathlib import Path

SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
sys.path.insert(0, SCRIPTS)
try:
    import probe_prompt_handoff as probe
    import probe_research_run as probe_run
finally:
    sys.path.remove(SCRIPTS)


def event(seq, kind, inbox=None):
    return {"event": "event", "type": kind, "session": "ses_one", "inbox": inbox, "seq": seq}


def test_a_prompt_held_until_a_later_step_is_reported_as_the_same_execution():
    events = [
        event(1, "session.execution.started"),
        event(2, "session.inbox.enqueued", "msg_queued"),
        event(3, "session.inbox.delivered", "msg_first"),
        event(4, "session.inbox.delivered", "msg_queued"),
        event(5, "session.execution.succeeded"),
    ]
    assert probe.delivery_scope(events, "msg_queued") == "same_execution"


def test_a_prompt_queued_on_an_idle_session_starts_its_own_execution():
    events = [
        event(1, "session.inbox.enqueued", "msg_queued"),
        event(2, "session.execution.started"),
        event(3, "session.inbox.delivered", "msg_queued"),
    ]
    assert probe.delivery_scope(events, "msg_queued") == "new_execution"


def test_a_prompt_delivered_after_a_new_execution_started_is_reported_as_a_new_execution():
    events = [
        event(1, "session.execution.started"),
        event(2, "session.inbox.enqueued", "msg_queued"),
        event(3, "session.execution.succeeded"),
        event(4, "session.execution.started"),
        event(5, "session.inbox.delivered", "msg_queued"),
    ]
    assert probe.delivery_scope(events, "msg_queued") == "new_execution"


def test_a_prompt_without_a_delivery_event_is_reported_as_undelivered():
    events = [event(1, "session.inbox.enqueued", "msg_queued"), event(2, "session.execution.started")]
    assert probe.delivery_scope(events, "msg_queued") == "undelivered"


def test_events_without_a_durable_sequence_are_ignored():
    events = [
        {"event": "event", "type": "session.inbox.enqueued", "session": "ses_one", "inbox": "msg_queued"},
        {"event": "event", "type": "session.inbox.delivered", "session": "ses_one", "inbox": "msg_queued"},
    ]
    assert probe.delivery_scope(events, "msg_queued") == "undelivered"


def test_a_later_tool_request_is_a_normal_round():
    assert probe_run.handoff_gate({"normal_round": True, "new_executions_after_delivery": 1}) == {
        "gate": "passed", "reason": None}


def test_text_in_history_without_a_later_execution_is_blocked():
    gate = probe_run.handoff_gate({"normal_round": False, "new_executions_after_delivery": 0})
    assert gate == {"gate": "blocked", "reason": "no_later_execution"}


def test_a_later_execution_without_a_tool_request_is_blocked():
    gate = probe_run.handoff_gate({"normal_round": False, "new_executions_after_delivery": 2})
    assert gate == {"gate": "blocked", "reason": "no_tool_request_after_delivery"}
