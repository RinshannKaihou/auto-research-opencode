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
INDEX = ROOT / "plugin" / "index.ts"
BUSY = 'if (exited && await this.childBusy(child)) return { text: "", exited: false }'
RUN_EXITED = (
    "const exited = await this.finished(child, controller.signal)\n"
    "      if (!this.current()) return { task_id: reserved.task_id, state: 'unverified'"
)
RUN_FORCED = (
    "const exited = (await this.finished(child, controller.signal)) || true\n"
    "      if (!this.current()) return { task_id: reserved.task_id, state: 'unverified'"
)
RUN_PREFIX_TRUE = (
    "const exited = true || await this.finished(child, controller.signal)\n"
    "      if (!this.current()) return { task_id: reserved.task_id, state: 'unverified'"
)


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


def test_event_derived_busy_functions_are_covered():
    covered = {(relative, name) for relative, name, _kind in verify.COVERED}
    assert ("plugin/src/specialists.ts", "noteBusy") in covered
    assert ("plugin/src/specialists.ts", "appearsBusy") in covered


def test_wait_messages_and_the_run_slice_are_fingerprinted():
    covered = {(relative, name) for relative, name, _kind in verify.COVERED}
    assert ("plugin/index.ts", "wait") in covered
    assert ("plugin/index.ts", "messages") in covered
    assert ("plugin/src/specialists.ts", "run") not in covered
    slices = {(relative, label) for relative, label, _extract in verify.SLICES}
    assert slices == {("plugin/src/specialists.ts", "run:finished-to-finishChild")}


def _run_span(source: str) -> str:
    extractors = [fn for _rel, label, fn in verify.SLICES if label == "run:finished-to-finishChild"]
    assert len(extractors) == 1
    return extractors[0](source)


def _other_fingerprints(specialists: str) -> dict[tuple[str, str], str]:
    index = INDEX.read_text(encoding="utf-8")
    files = {
        "plugin/src/specialists.ts": specialists,
        "plugin/index.ts": index,
    }
    found = {}
    for relative, name, kind in verify.COVERED:
        found[(relative, name)] = verify.sha256_text(verify.extract_function(files[relative], name, kind))
    return found


def test_forcing_finished_true_changes_only_the_run_slice():
    source = SPECIALISTS.read_text(encoding="utf-8")
    assert source.count(RUN_EXITED) == 1
    assert source.count(RUN_PREFIX_TRUE) == 0
    span = _run_span(source)
    assert span.startswith("const exited = await this.finished(")
    assert "this.finishChild(" in span
    assert "exited" in span
    assert "this.stop(child)" not in span
    assert "专家任务" not in span
    assert span.count("{") == span.count("}")
    original = _other_fingerprints(source)

    # A prefix before this.finished( is still the same statement. true ||
    # short-circuits the call and forces exited true.
    prefixed = source.replace(RUN_EXITED, RUN_PREFIX_TRUE, 1)
    prefixed_span = _run_span(prefixed)
    assert prefixed_span.startswith("const exited = true || await this.finished(")
    assert "true ||" in prefixed_span
    assert prefixed_span != span
    assert _other_fingerprints(prefixed) == original

    forced = source.replace(RUN_EXITED, RUN_FORCED, 1)
    forced_span = _run_span(forced)
    assert forced_span.startswith("const exited = (await this.finished(")
    assert "|| true" in forced_span
    assert forced_span != span
    assert _other_fingerprints(forced) == original

    # An edit elsewhere in run(), including the catch path, is not this slice.
    kickoff = source.replace("`专家任务 ${reserved.task_id}`", "`专家任务改 ${reserved.task_id}`", 1)
    assert _run_span(kickoff) == span
    assert body(kickoff, "run", "method") != body(source, "run", "method")
    caught = source.replace(
        "const exited = child ? await this.stop(child).catch(() => false) : false",
        "const exited = true",
        1,
    )
    assert source.count("const exited = child ? await this.stop(child).catch(() => false) : false") == 1
    assert _run_span(caught) == span


def test_dropping_the_wait_await_changes_its_body():
    source = INDEX.read_text(encoding="utf-8")
    original = "await ctx.session.wait({ sessionID })"
    assert source.count(original) == 1
    broken = source.replace(original, "void ctx.session.wait({ sessionID })", 1)
    assert body(broken, "wait", "prop") != body(source, "wait", "prop")


def test_reversing_the_transcript_changes_messages():
    source = INDEX.read_text(encoding="utf-8")
    original = "return (await ctx.session.context({ sessionID })) as any[]"
    assert source.count(original) == 1
    broken = source.replace(original, "return ((await ctx.session.context({ sessionID })) as any[]).reverse()", 1)
    assert body(broken, "messages", "prop") != body(source, "messages", "prop")


def test_appears_busy_always_false_changes_its_body():
    source = SPECIALISTS.read_text(encoding="utf-8")
    original = "return this.sessionBusy.get(sessionID) === true"
    assert source.count(original) == 1
    broken = source.replace(original, "return false", 1)
    assert body(broken, "appearsBusy", "method") != body(source, "appearsBusy", "method")


def test_note_busy_that_never_sets_busy_changes_its_body():
    source = SPECIALISTS.read_text(encoding="utf-8")
    original = "this.sessionBusy.set(sessionID, true)"
    assert source.count(original) == 1
    broken = source.replace(original, "void sessionID", 1)
    assert body(broken, "noteBusy", "method") != body(source, "noteBusy", "method")


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
