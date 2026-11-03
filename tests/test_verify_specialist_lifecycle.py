"""The lifecycle checker must notice a body edit, not only an exact missing string."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_specialist_lifecycle",
    ROOT / "scripts" / "verify_specialist_lifecycle.py",
)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)

SPECIALISTS = ROOT / "plugin" / "src" / "specialists.ts"
BUSY = 'if (exited && await this.childBusy(child)) return { text: "", exited: false }'


def body(source: str, name: str, kind: str) -> str:
    return verify.extract_function(source, name, kind)


def test_covered_functions_extract_and_balance():
    for relative, name, kind in verify.COVERED:
        source = (ROOT / relative).read_text(encoding="utf-8")
        extracted = body(source, name, kind)
        assert name in extracted
        assert extracted.count("{") == extracted.count("}")


def test_commenting_out_the_busy_check_changes_summarize():
    source = SPECIALISTS.read_text(encoding="utf-8")
    assert source.count(BUSY) == 1
    commented = source.replace(BUSY, "// " + BUSY, 1)
    assert BUSY in commented
    assert body(commented, "summarize", "method") != body(source, "summarize", "method")


def test_text_since_returning_every_message_changes_its_body():
    source = SPECIALISTS.read_text(encoding="utf-8")
    original = "return sessionResult(messages.slice(start + 1)).text"
    assert source.count(original) == 1
    broken = source.replace(original, "return sessionResult(messages).text", 1)
    assert body(broken, "textSince", "function") != body(source, "textSince", "function")


def test_child_busy_always_false_changes_its_body():
    source = SPECIALISTS.read_text(encoding="utf-8")
    original = "return probeWithin(probe, true)"
    assert source.count(original) == 1
    broken = source.replace(original, "return Promise.resolve(false)", 1)
    assert body(broken, "childBusy", "method") != body(source, "childBusy", "method")


def test_dropping_the_stopped_await_changes_summarize():
    source = SPECIALISTS.read_text(encoding="utf-8")
    original = "const exited = await stopped()"
    assert source.count(original) == 1
    broken = source.replace(original, "const exited = stopped()", 1)
    assert BUSY in broken
    assert body(broken, "summarize", "method") != body(source, "summarize", "method")


def test_early_return_before_the_busy_check_changes_summarize():
    source = SPECIALISTS.read_text(encoding="utf-8")
    broken = source.replace(BUSY, 'return { text: "", exited: true }\n          ' + BUSY, 1)
    assert BUSY in broken
    assert body(broken, "summarize", "method") != body(source, "summarize", "method")


def test_config_must_name_invariant_safe():
    assert verify.cfg_declares_safe("INVARIANT Safe\n")
    assert verify.cfg_declares_safe("  INVARIANT   Safe\n")
    assert not verify.cfg_declares_safe("INVARIANT Safety\n")
    assert not verify.cfg_declares_safe("\\* INVARIANT Safe\n")
    for _title, config, _want in verify.CHECKS:
        text = (verify.SPEC_DIR / config).read_text(encoding="utf-8")
        assert verify.cfg_declares_safe(text), config


def test_exit_codes_are_distinct():
    codes = {
        verify.EXIT_OK,
        verify.EXIT_COUNTEREXAMPLE,
        verify.EXIT_MODEL,
        verify.EXIT_DRIFT,
        verify.EXIT_SETUP,
    }
    assert codes == {0, 1, 2, 3, 4}


def test_worktree_matches_the_sync_commit():
    commit = verify.sync_commit()
    present = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=ROOT,
        check=False,
    )
    if present.returncode != 0:
        pytest.skip("sync commit is not in this clone")
    assert verify.drift_problems(commit) == []
