"""The context-placement probe must fail when a research request lacks a tail."""
import sys
import pytest
from pathlib import Path

SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
sys.path.insert(0, SCRIPTS)
try:
    import probe_research_run as probe
finally:
    sys.path.remove(SCRIPTS)


def body(messages, research=True):
    tools = [{"type": "function", "function": {"name": "research_query"}}] if research else [
        {"type": "function", "function": {"name": "read"}}]
    return {"tools": tools, "messages": messages}


def text_part():
    return {"role": "user", "content": [
        {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}]}


def tail():
    return {"role": "user", "content": "\n".join([
        "<research_state>", "Injected research state for this request, not user input.", "",
        "## recent_node_notes", "[]", "</research_state>"])}


def tool_call():
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "research_query", "arguments": "{}"}}]}


def tool_result():
    return {"role": "tool", "tool_call_id": "call_1", "content": [
        {"type": "text", "text": "ok", "cache_control": {"type": "ephemeral"}}]}


def finishing(kind="wait"):
    return {"tools": [], "messages": [
        {"role": "system", "content": f"A research {kind} was accepted in this execution. Do not call tools."},
        tool_call(), tool_result()]}


def test_one_research_request_without_a_tail_fails():
    checks = probe.context_placement_checks(
        [body([text_part(), tail()]), body([text_part()])], "tail", "")
    assert checks["research_requests"] == 2
    assert checks["tail_requests"] == 1
    assert checks["unaccounted_requests"] == 1
    assert checks["all_research_requests_accounted_for"] is False
    assert checks["tail_is_last"] is False
    assert probe.context_placement_ok(checks) is False


def test_every_research_request_with_a_tail_passes():
    checks = probe.context_placement_checks(
        [body([text_part(), tail()]), body([text_part(), tool_call(), tool_result(), tail()])],
        "tail", "")
    assert checks["all_research_requests_accounted_for"] is True
    assert checks["tail_is_last"] is True
    assert checks["tool_continuations"] == 1
    assert checks["tail_follows_every_regular_tool_continuation"] is True
    assert checks["tool_pairing_ok"] is True
    assert probe.context_placement_ok(checks) is True


def test_a_tail_that_splits_a_tool_pair_is_rejected():
    checks = probe.context_placement_checks(
        [body([text_part(), tool_call(), tail()])], "tail", "")
    assert checks["tool_pairing_ok"] is False
    assert probe.context_placement_ok(checks) is False


def test_an_auxiliary_request_without_a_tail_is_ignored():
    checks = probe.context_placement_checks(
        [body([text_part(), tail()]), body([{"role": "user", "content": "title"}], research=False)],
        "tail", "")
    assert checks["research_requests"] == 1
    assert checks["all_research_requests_accounted_for"] is True


@pytest.mark.parametrize("kind", ["wait", "finish"])
def test_one_tool_free_finishing_request_is_accounted_for(kind):
    checks = probe.context_placement_checks(
        [body([text_part(), tail()]), body([text_part(), tool_call(), tool_result(), tail()]), finishing(kind)],
        "tail", "")
    assert checks["research_requests"] == 3
    assert checks["tail_requests"] == 2
    assert checks["finishing_requests"] == 1
    assert checks["wait_finishing_requests"] == (kind == "wait")
    assert checks["node_finishing_requests"] == (kind == "finish")
    assert checks["all_research_requests_accounted_for"] is True
    assert checks["finishing_requests_have_no_tools_and_no_tail"] is True
    assert probe.context_placement_ok(checks) is True


def test_a_finishing_request_with_a_missing_tool_result_is_rejected():
    bad = finishing()
    bad["messages"] = bad["messages"][:-1]
    checks = probe.context_placement_checks(
        [body([text_part(), tail()]), bad], "tail", "")
    assert checks["finishing_requests"] == 1
    assert checks["tool_pairing_ok"] is False
    assert probe.context_placement_ok(checks) is False


def test_a_finishing_request_with_a_tail_is_rejected():
    bad = finishing()
    bad["messages"] = bad["messages"] + [tail()]
    checks = probe.context_placement_checks(
        [body([text_part(), tail()]), bad], "tail", "")
    assert checks["finishing_requests"] == 1
    assert checks["finishing_requests_have_no_tools_and_no_tail"] is False
    assert probe.context_placement_ok(checks) is False


@pytest.mark.parametrize("kind", ["wait", "finish"])
def test_a_finishing_instruction_with_tools_is_rejected(kind):
    bad = finishing(kind)
    bad["tools"] = body([])["tools"]
    checks = probe.context_placement_checks(
        [body([text_part(), tool_call(), tool_result(), tail()]), bad], "tail", "")
    assert checks["finishing_requests"] == 1
    assert checks["finishing_requests_have_no_tools_and_no_tail"] is False
    assert probe.context_placement_ok(checks) is False
