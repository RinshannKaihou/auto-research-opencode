"""The evidence extractor must count each completed compaction separately."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "context_placement_evidence.py"
spec = importlib.util.spec_from_file_location("context_placement_evidence", SCRIPT)
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


def record(compactions):
    session = "ses_test"
    observed = {"requests": 226, "input": 1000, "cache_read": 2000,
                "cache_write": 0, "output": 300, "reasoning": 50}
    return {
        "sessions": {session: {"role": "main", "node_id": None, "pause_reason": None}},
        "host_sessions": {session: {
            "input": 1300, "cache_read": 2100, "cache_write": 0,
            "output": 350, "reasoning": 50, "observed": observed,
            "delta": {"input": 300, "cache_read": 100, "cache_write": 0,
                      "output": 50, "reasoning": 0},
        }},
        "observed": {session: observed}, "observations": [],
        "main_messages": {}, "notifications": [], "stability": {}, "unknown_usage": [],
        "compaction": compactions, "conclusion": None,
    }


def compaction(message, input_tokens):
    return {"session_id": "ses_test", "id": message, "status": "completed",
            "tokens": {"input": input_tokens, "output": 20, "reasoning": 0,
                       "cache": {"read": 50, "write": 0}}}


def test_two_completed_compactions_count_as_two_requests():
    summary = evidence.summarize(record([
        compaction("c1", 100),
        compaction("c2", 200),
    ]))
    assert summary["whole_run_known"]["totals"]["requests"] == 228
    assert [item["tokens"]["input"] for item in summary["compaction"]] == [100, 200]


def test_one_completed_compaction_counts_once():
    summary = evidence.summarize(record([compaction("c1", 300)]))
    assert summary["whole_run_known"]["totals"]["requests"] == 227
