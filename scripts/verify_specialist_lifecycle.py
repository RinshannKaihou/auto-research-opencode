#!/usr/bin/env python3
"""Re-check the specialist child-session lifecycle model.

Exit codes (GNU make prints this number as ``Error N`` and then exits 2 itself):

0  Pass. Covered function bodies match the sync commit, TLC checked invariant
   Safe on the current model, and the known breaks still violate Safe.
1  Counterexample. Safe is violated for the code the model describes. The
   trace is a product bug. Do not weaken Safe.
2  Model or tool failure. The spec, a config, or TLC failed, or a known break
   stopped failing. Not a product-bug verdict.
3  Drift. A covered function body, or the run() slice from the `const exited`
   statement through the finishChild() call that receives exited, differs
   from the sync commit recorded in
   skills/verify-specialist-child-lifecycle/SKILL.md. Re-sync the model before
   trusting a pass. A same-meaning rewrite is drift, not a counterexample.
4  Setup. git or java is missing, a required commit is not in this clone, or
   the TLC jar could not be downloaded or is not the pinned release.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = ROOT / "spec" / "specialist-child-lifecycle"
SPEC = SPEC_DIR / "SpecialistChildLifecycle.tla"
SKILL = ROOT / "skills" / "verify-specialist-child-lifecycle" / "SKILL.md"
JAR_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"
JAR_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
TLC_RELEASE = "v1.7.4"
# Banner TLC prints for that release. The manifest's Implementation-Version
# field is "2.0 2024-08-08"; the checker version is this banner.
TLC_VERSION = "2.19 of 08 August 2024"
TLC_REVISION = "5a47802"
TLC_BANNER = f"TLC2 Version {TLC_VERSION} (rev: {TLC_REVISION})"
TLC_TIMEOUT_SECONDS = 120
PRE28 = "8a2c6c944ef3606face6cc834250e50a3bd5be1b"
CHECKED_MARK = "Checked invariant Safe"

EXIT_OK = 0
EXIT_COUNTEREXAMPLE = 1
EXIT_MODEL = 2
EXIT_DRIFT = 3
EXIT_SETUP = 4

# Signature plus body, extracted the same way from the worktree and from the
# sync commit. The comment above a function is not part of the fingerprint.
COVERED = (
    ("plugin/src/specialists.ts", "sessionResult", "function"),
    ("plugin/src/specialists.ts", "messageID", "function"),
    ("plugin/src/specialists.ts", "probeWithin", "function"),
    ("plugin/src/specialists.ts", "textSince", "function"),
    ("plugin/src/specialists.ts", "noteProgress", "method"),
    ("plugin/src/specialists.ts", "noteBusy", "method"),
    ("plugin/src/specialists.ts", "appearsBusy", "method"),
    ("plugin/src/specialists.ts", "childBusy", "method"),
    ("plugin/src/specialists.ts", "armLimits", "method"),
    ("plugin/src/specialists.ts", "finished", "method"),
    ("plugin/src/specialists.ts", "stop", "method"),
    ("plugin/src/specialists.ts", "summarize", "method"),
    ("plugin/src/specialists.ts", "finishChild", "method"),
    ("plugin/src/specialists.ts", "settle", "method"),
    ("plugin/index.ts", "within", "function"),
    ("plugin/index.ts", "wait", "prop"),
    ("plugin/index.ts", "messages", "prop"),
    ("plugin/index.ts", "busy", "prop"),
    ("plugin/index.ts", "observeSpecialist", "assign"),
)

_PATTERNS = {
    "function": r"(?:export\s+)?(?:async\s+)?function\s+{name}\b",
    "method": r"(?<![\w.])(?:(?:private|public|protected)\s+)?(?:async\s+)?{name}\b",
    "prop": r"(?<![\w.]){name}\s*:\s*",
    "assign": r"(?<![\w.]){name}\s*=\s*",
}

CHECKS = (
    ("code on disk", "current.cfg", "pass"),
    ("pre-#28 main at 8a2c6c9", "pre28.cfg", "violation"),
    ("busy check removed", "noBusyCheck.cfg", "violation"),
    ("textSince returns every message", "textSinceAll.cfg", "violation"),
)

ACTIONS = {
    "CapTimerAborts": "The absolute cap aborts the work controller.",
    "StallTimerAborts": "The stall timer aborts the work controller.",
    "ProgressResetsStall": "A progress event resets the stall window.",
    "QuietWindowElapses": "The stall window elapses again.",
    "ParentAborts": "The delegating call aborts.",
    "SummarySendLandsAndStartsTurn": "The summary send lands and starts a turn while its promise is still pending.",
    "WatchSeesIdle": "finished() sees the work turn go idle.",
    "WatchSeesAbort": "finished() sees the abort first and calls stop().",
    "InterruptStopsTheTurn": "host.interrupt stops the running turn.",
    "InterruptReturnsWithoutStopping": "host.interrupt returns without stopping the turn.",
    "StopGraceSeesIdle": "stop() sees idle before the grace timer.",
    "StopGraceExpires": "stop()'s grace timer wins, so the exit is not verified.",
    "ReadTranscriptAndDecide": "finishChild reads the transcript and chooses summary, archive, or settle.",
    "SummarySendSettles": "The summary send promise resolves.",
    "SummarySendThrows": "The summary send promise rejects.",
    "SummaryGraceExpires": "The summary grace wins before the send settles.",
    "SummaryWatchSeesIdle": "The summary turn goes idle.",
    "SummaryGraceDuringWatch": "The summary grace aborts the summary controller.",
    "SummaryWatchSeesAbort": "finished() on the summary turn sees the abort and calls stop().",
    "DeadReadReturns": "The post-grace message read returns (it may be empty).",
    "BusyProbeSeesRunningChild": "The busy probe sees the child still running.",
    "BusyProbeSeesIdleChild": "The busy probe sees the child idle.",
    "BusyProbeHangsOrTimesOut": "The busy probe hangs, times out, or throws, which counts as busy.",
    "PostGraceReadSettles": "The message read after the summary grace returns textSince.",
    "BoundedSummaryReadSettles": "The summary message read returns textSince.",
    "RecordLedger": "specialist_finish records the result.",
    "DoneStutter": "The ledger write has finished.",
    "Initial predicate": "The child is running after kickoff, with the opening line already in the transcript.",
}


class ExtractError(Exception):
    pass


def fail(message: str, code: int = EXIT_MODEL) -> None:
    label = {1: "COUNTEREXAMPLE", 2: "MODEL", 3: "DRIFT", 4: "SETUP"}.get(code, "ERROR")
    print(f"{label}: {message}", file=sys.stderr)
    raise SystemExit(code)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _skip_comment(source: str, index: int) -> int:
    if source.startswith("//", index):
        newline = source.find("\n", index)
        return len(source) if newline < 0 else newline + 1
    if source.startswith("/*", index):
        end = source.find("*/", index + 2)
        if end < 0:
            raise ExtractError("unterminated block comment")
        return end + 2
    return index


def _skip_ws_comments(source: str, index: int) -> int:
    while index < len(source):
        if source.startswith("//", index) or source.startswith("/*", index):
            index = _skip_comment(source, index)
            continue
        if source[index] in " \t\r\n":
            index += 1
            continue
        break
    return index


def _skip_string(source: str, index: int) -> int:
    """Return the index after a string or template starting at index, else index."""
    if index >= len(source) or source[index] not in "'\"`":
        return index
    quote = source[index]
    index += 1
    while index < len(source):
        if source[index] == "\\":
            index += 2
            continue
        if source[index] == quote:
            return index + 1
        index += 1
    raise ExtractError("unterminated string")


def _match_delim(source: str, open_at: int, open_ch: str, close_ch: str) -> int:
    if source[open_at] != open_ch:
        raise ExtractError(f"expected {open_ch}")
    index = open_at + 1
    depth = 1
    while index < len(source):
        if source.startswith("//", index) or source.startswith("/*", index):
            index = _skip_comment(source, index)
            continue
        nxt = _skip_string(source, index)
        if nxt != index:
            index = nxt
            continue
        if source[index] == open_ch:
            depth += 1
        elif source[index] == close_ch:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ExtractError(f"unbalanced {open_ch}")


def _match_angle(source: str, open_at: int) -> int:
    index = open_at
    depth = 0
    while index < len(source):
        if source[index] == "<":
            depth += 1
        elif source[index] == ">":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ExtractError("unterminated generic")


def _param_open(source: str, index: int) -> int:
    index = _skip_ws_comments(source, index)
    if index < len(source) and source[index] == "<":
        index = _skip_ws_comments(source, _match_angle(source, index) + 1)
    if source.startswith("async", index) and not (index + 5 < len(source) and source[index + 5].isalnum()):
        index = _skip_ws_comments(source, index + 5)
    if index >= len(source) or source[index] != "(":
        raise ExtractError("no parameter list")
    return index


def _body_open(source: str, paren_close: int) -> int:
    index = _skip_ws_comments(source, paren_close + 1)
    if source.startswith("=>", index):
        index = _skip_ws_comments(source, index + 2)
        if index >= len(source) or source[index] != "{":
            raise ExtractError("arrow function has no block body")
        return index
    angle = paren = bracket = 0
    while index < len(source):
        if source.startswith("//", index) or source.startswith("/*", index):
            index = _skip_comment(source, index)
            continue
        nxt = _skip_string(source, index)
        if nxt != index:
            index = nxt
            continue
        if source.startswith("=>", index) and angle == paren == bracket == 0:
            index += 2
            continue
        char = source[index]
        if char == "<":
            angle += 1
        elif char == ">" and angle:
            angle -= 1
        elif char == "(":
            paren += 1
        elif char == ")" and paren:
            paren -= 1
        elif char == "[":
            bracket += 1
        elif char == "]" and bracket:
            bracket -= 1
        elif char == "{" and angle == paren == bracket == 0:
            close = _match_delim(source, index, "{", "}")
            after = _skip_ws_comments(source, close + 1)
            if after < len(source) and source[after] == "{":
                return after
            if after < len(source) and source[after] in "|&[":
                index = after
                continue
            return index
        index += 1
    raise ExtractError("no function body")


def _code_find_all(source: str, needle: str) -> list[int]:
    """Indexes of needle outside comments and strings."""
    found: list[int] = []
    index = 0
    while index < len(source):
        if source.startswith("//", index) or source.startswith("/*", index):
            index = _skip_comment(source, index)
            continue
        nxt = _skip_string(source, index)
        if nxt != index:
            index = nxt
            continue
        if source.startswith(needle, index):
            found.append(index)
            index += len(needle)
            continue
        index += 1
    return found


# A line that ends in one of these words has not finished a statement.
_NO_END_WORDS = frozenset({
    "await", "async", "new", "typeof", "void", "delete", "yield",
    "instanceof", "in", "of", "else", "catch", "finally", "extends",
    "case", "class", "function", "if", "for", "while", "do", "switch",
    "try", "const", "let", "var", "import", "export", "throw", "return",
    "get", "set", "static", "public", "private", "protected", "readonly",
    "implements", "interface", "type", "enum", "namespace", "declare",
    "abstract", "as", "satisfies", "infer", "keyof", "unique", "override",
    "from", "with",
})

# `{` after one of these opens a statement block, not an object literal.
_BLOCK_PREV = frozenset({">", ")", "=>", "try", "else", "do", "finally"})

_CONTINUATION_WORDS = frozenset({"instanceof", "in", "of", "as", "satisfies"})

_THREE_CHAR_OPS = frozenset({"===", "!==", "**=", ">>=", "<<=", "??="})

_TWO_CHAR_OPS = frozenset({
    "&&", "||", "??", "=>", "?.", "==", "!=", "<=", ">=",
    "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
    "**", "<<", ">>", "++", "--",
})


def _word_end(source: str, index: int) -> int:
    end = index + 1
    while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
        end += 1
    return end


def _starts_continuation(source: str, index: int) -> bool:
    """True when the token at index continues the previous line's statement."""
    if index >= len(source):
        return False
    three = source[index:index + 3]
    two = source[index:index + 2]
    if three in _THREE_CHAR_OPS or two in _TWO_CHAR_OPS - {"++", "--"}:
        return True
    if source[index] in ".,?:([+*/%=<>&|^":
        return True
    if source[index].isalpha() or source[index] in "_$":
        return source[index:_word_end(source, index)] in _CONTINUATION_WORDS
    return False


def _can_split(paren: int, bracket: int, brace_stack: list[str]) -> bool:
    """Newlines separate statements only outside calls, arrays, and object literals."""
    if paren or bracket:
        return False
    return not brace_stack or brace_stack[-1] == "block"


def _statement_start(source: str, index: int) -> int:
    """Index of the first token of the statement that contains index.

    The slice then includes a prefix such as ``const exited = true ||``.
    A newline is a boundary where automatic semicolon insertion would end the
    statement. A block ``{`` starts the next statement.
    """
    if index < 0 or index > len(source):
        raise ExtractError("statement index out of range")
    statement_start = _skip_ws_comments(source, 0)
    paren = 0
    bracket = 0
    brace_stack: list[str] = []
    prev_can_end = False
    prev_token = ""
    i = 0
    while i < len(source) and i <= index:
        if source.startswith("//", i) or source.startswith("/*", i):
            i = _skip_comment(source, i)
            continue
        ch = source[i]
        if ch in " \t\r":
            i += 1
            continue
        if ch == "\n":
            i += 1
            if prev_can_end and _can_split(paren, bracket, brace_stack):
                nxt = _skip_ws_comments(source, i)
                if nxt <= index and (nxt >= len(source) or not _starts_continuation(source, nxt)):
                    statement_start = nxt
                    prev_can_end = False
            continue
        nxt = _skip_string(source, i)
        if nxt != i:
            if _can_split(paren, bracket, brace_stack):
                prev_can_end = True
            prev_token = source[i]
            i = nxt
            continue
        if ch.isalpha() or ch in "_$":
            end = _word_end(source, i)
            word = source[i:end]
            if _can_split(paren, bracket, brace_stack):
                prev_can_end = word not in _NO_END_WORDS
            prev_token = word
            i = end
            continue
        if ch.isdigit():
            end = i + 1
            while end < len(source) and (source[end].isalnum() or source[end] in "._"):
                end += 1
            if _can_split(paren, bracket, brace_stack):
                prev_can_end = True
            prev_token = source[i:end]
            i = end
            continue
        three = source[i:i + 3]
        two = source[i:i + 2]
        if three in _THREE_CHAR_OPS or two in _TWO_CHAR_OPS:
            op = three if three in _THREE_CHAR_OPS else two
            if _can_split(paren, bracket, brace_stack):
                prev_can_end = op in {"++", "--"}
            prev_token = op
            i += len(op)
            continue
        if ch == "(":
            paren += 1
            prev_can_end = False
            prev_token = ch
            i += 1
            continue
        if ch == ")":
            if paren:
                paren -= 1
            if _can_split(paren, bracket, brace_stack):
                prev_can_end = True
            prev_token = ch
            i += 1
            continue
        if ch == "[":
            bracket += 1
            prev_can_end = False
            prev_token = ch
            i += 1
            continue
        if ch == "]":
            if bracket:
                bracket -= 1
            if _can_split(paren, bracket, brace_stack):
                prev_can_end = True
            prev_token = ch
            i += 1
            continue
        if ch == "{":
            kind = "block" if prev_token in _BLOCK_PREV else "object"
            brace_stack.append(kind)
            prev_can_end = False
            prev_token = ch
            i += 1
            if kind == "block":
                nxt = _skip_ws_comments(source, i)
                if nxt <= index and nxt < len(source) and source[nxt] != "}":
                    statement_start = nxt
            continue
        if ch == "}":
            if brace_stack:
                brace_stack.pop()
            if _can_split(paren, bracket, brace_stack):
                prev_can_end = True
            prev_token = ch
            i += 1
            continue
        if ch == ";" and _can_split(paren, bracket, brace_stack):
            nxt = _skip_ws_comments(source, i + 1)
            if nxt <= index:
                statement_start = nxt
            prev_can_end = False
            prev_token = ch
            i += 1
            continue
        if _can_split(paren, bracket, brace_stack):
            prev_can_end = False
        prev_token = ch
        i += 1
    if statement_start > index:
        raise ExtractError("statement start fell after the finished() call")
    return statement_start


def extract_run_exited_span(source: str) -> str:
    """The run() slice from the start of the `const exited` statement through finishChild.

    The statement prefix is part of the slice, so ``true || await this.finished(...)``
    changes the hash. The rest of run(), including launch and the catch path, is not.
    """
    run = extract_function(source, "run", "method")
    starts = _code_find_all(run, "this.finished(")
    if len(starts) != 1:
        raise ExtractError(f"{len(starts)} this.finished( calls in run()")
    statement = _statement_start(run, starts[0])
    calls = [at for at in _code_find_all(run, "this.finishChild(") if at > starts[0]]
    if len(calls) != 1:
        raise ExtractError(f"{len(calls)} this.finishChild( calls after finished() in run()")
    open_paren = calls[0] + len("this.finishChild(") - 1
    close = _match_delim(run, open_paren, "(", ")")
    arguments = run[open_paren + 1 : close]
    if re.search(r"(?<![\w.])exited\b", arguments) is None:
        raise ExtractError("finishChild in run() does not receive exited")
    return run[statement : close + 1]


def extract_function(source: str, name: str, kind: str) -> str:
    pattern = _PATTERNS[kind].format(name=re.escape(name))
    found: list[str] = []
    for match in re.finditer(pattern, source):
        try:
            paren = _param_open(source, match.end())
            close = _match_delim(source, paren, "(", ")")
            body = _body_open(source, close)
            end = _match_delim(source, body, "{", "}")
        except ExtractError:
            continue
        found.append(source[match.start() : end + 1])
    if len(found) == 1:
        return found[0]
    if not found:
        raise ExtractError(f"no {kind} definition of {name}")
    raise ExtractError(f"{len(found)} definitions of {name}")


def sync_commit() -> str:
    if not SKILL.is_file():
        fail(f"Missing {SKILL.relative_to(ROOT)}. The coverage record has no sync commit.", EXIT_MODEL)
    text = SKILL.read_text(encoding="utf-8")
    match = re.search(r"^Sync commit:\s+`([0-9a-f]{7,40})`\s*$", text, re.M)
    if not match:
        fail(
            "The verify skill has no `Sync commit: `<sha>`` line. "
            "That line is the commit whose covered function bodies the model describes.",
            EXIT_MODEL,
        )
    return match.group(1)


def git_show(commit: str, relative: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "show", f"{commit}:{relative}"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as error:
        fail(
            f"Cannot read {relative} at {commit}. Fetch history so this clone contains that commit.\n{error.output}",
            EXIT_SETUP,
        )
    raise AssertionError("unreachable")


# Not a whole function. The callable receives the worktree or sync-commit file text.
SLICES = (
    ("plugin/src/specialists.ts", "run:finished-to-finishChild", extract_run_exited_span),
)


def _compare_region(commit: str, relative: str, label: str, extract) -> str | None:
    """None when the region matches commit. A string when the worktree differs.

    A region the sync commit itself cannot extract is a coverage-record error, not drift.
    """
    try:
        current_text = (ROOT / relative).read_text(encoding="utf-8")
    except OSError as error:
        return f"{relative} {label}: cannot read the worktree file ({error})"
    recorded_text = git_show(commit, relative)
    try:
        recorded = extract(recorded_text)
    except ExtractError as error:
        fail(
            f"Sync commit {commit} has no fingerprinted {relative} {label} ({error}). "
            "The coverage record does not describe that commit. Fix the skill or the extractor. "
            "This is not a product-bug verdict.",
            EXIT_MODEL,
        )
    try:
        current = extract(current_text)
    except ExtractError as error:
        return f"{relative} {label}: the covered function no longer extracts ({error})"
    if sha256_text(current) != sha256_text(recorded):
        return f"{relative} {label}"
    return None


def drift_problems(commit: str) -> list[str]:
    """Fingerprinted regions that differ from commit. Empty means in sync."""
    problems: list[str] = []
    for relative, name, kind in COVERED:
        problem = _compare_region(
            commit,
            relative,
            name,
            lambda text, name=name, kind=kind: extract_function(text, name, kind),
        )
        if problem:
            problems.append(problem)
    for relative, label, extract in SLICES:
        problem = _compare_region(commit, relative, label, extract)
        if problem:
            problems.append(problem)
    return problems


def check_drift(commit: str) -> None:
    problems = drift_problems(commit)
    if not problems:
        return
    fail(
        "Covered function bodies differ from sync commit "
        f"{commit}. Do not trust a pass, and do not read this as a counterexample. "
        "A same-meaning rewrite is drift too. Re-sync the model before trusting TLC: "
        "read the diff of the functions below, update "
        "spec/specialist-child-lifecycle/SpecialistChildLifecycle.tla so each action matches "
        "the new await boundaries, run make verify-specialist-lifecycle, and only then move "
        "the sync commit in skills/verify-specialist-child-lifecycle/SKILL.md.\n"
        + "\n".join(problems),
        EXIT_DRIFT,
    )


def manifest_entries(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        raw = archive.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    entries: dict[str, str] = {}
    key: str | None = None
    buffer: list[str] = []
    for line in raw.splitlines():
        if line.startswith(" ") and key:
            buffer.append(line[1:])
            continue
        if key:
            entries[key] = "".join(buffer).strip()
        if ":" in line:
            key, value = line.split(":", 1)
            buffer = [value.strip()]
        else:
            key = None
            buffer = []
    if key:
        entries[key] = "".join(buffer).strip()
    return entries


def check_jar_metadata(jar: Path) -> None:
    entries = manifest_entries(jar)
    tag = entries.get("X-Git-Tag", "")
    revision = entries.get("X-Git-ShortRevision", "")
    if tag != TLC_RELEASE or revision != TLC_REVISION:
        fail(
            f"TLC jar manifest tag {tag!r} revision {revision!r} does not match "
            f"pinned {TLC_RELEASE} revision {TLC_REVISION}. Refusing to treat its result as a pass or a counterexample.",
            EXIT_SETUP,
        )


def ensure_jar() -> Path:
    cache = SPEC_DIR / ".cache"
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        fail(f"Cannot create {cache}: {error}", EXIT_SETUP)
    jar = cache / "tla2tools.jar"
    if jar.exists() and sha256_file(jar) == JAR_SHA256:
        check_jar_metadata(jar)
        return jar
    print(f"Downloading TLC {TLC_RELEASE} from {JAR_URL}")
    tmp = jar.with_suffix(".jar.partial")
    request = urllib.request.Request(JAR_URL, headers={"User-Agent": "auto-research-opencode-verify"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except Exception as error:  # noqa: BLE001 — download failure is a setup error
        tmp.unlink(missing_ok=True)
        fail(f"Could not download TLC {TLC_RELEASE}: {error}", EXIT_SETUP)
    digest = sha256_file(tmp)
    if digest != JAR_SHA256:
        tmp.unlink(missing_ok=True)
        fail(f"TLC jar sha256 {digest} does not match the pinned {JAR_SHA256}", EXIT_SETUP)
    tmp.replace(jar)
    check_jar_metadata(jar)
    return jar


def cfg_declares_safe(text: str) -> bool:
    return re.search(r"(?m)^\s*INVARIANT\s+Safe\s*$", text) is not None


def require_invariant(config: str) -> None:
    path = SPEC_DIR / config
    if not path.is_file():
        fail(f"Missing TLC config {path.relative_to(ROOT)}", EXIT_MODEL)
    if not cfg_declares_safe(path.read_text(encoding="utf-8")):
        fail(
            f"{config} has no INVARIANT Safe line, so a TLC pass would not have checked the property. "
            "This is a modeling error, not a product-bug verdict.",
            EXIT_MODEL,
        )


def check_pre28_commit() -> None:
    old = git_show(PRE28, "plugin/src/specialists.ts")
    problems = []
    if "private async summarize" in old:
        problems.append("8a2c6c9 unexpectedly has summarize(); the pre28 model would be wrong")
    if "达到专家时间上限，已收回部分报告" not in old:
        problems.append("8a2c6c9 is missing the timeout partial-report copy the pre28 model archives")
    if "exit_verified: exited" not in old:
        problems.append("8a2c6c9 does not record exit_verified from the stop result")
    if "signal.aborted" in old.split("const timer = setTimeout", 1)[-1].split("},", 1)[0]:
        problems.append("8a2c6c9's absolute timer checks signal.aborted; the pre28 cap model would be wrong")
    if problems:
        fail(
            "pre28 fixture does not match commit 8a2c6c9. This is a modeling error, not a product-bug verdict.\n"
            + "\n".join(problems),
            EXIT_MODEL,
        )


def parse_trace(text: str) -> list[tuple[str, dict[str, str]]]:
    if "The behavior up to this point is:" not in text:
        return []
    body = text.split("The behavior up to this point is:", 1)[1]
    body = body.split("states generated", 1)[0]
    states = []
    for chunk in re.split(r"\n(?=State \d+:)", body):
        header = re.search(r"State \d+: <([^>]+)>", chunk)
        if not header:
            continue
        name = re.sub(
            r" line \d+, col \d+ to line \d+, col \d+ of module \S+",
            "",
            header.group(1),
        )
        fields = dict(re.findall(r"/\\ (\w+) = (\S+)", chunk))
        states.append((name, fields))
    return states


def compact_trace(states: list[tuple[str, dict[str, str]]]) -> str:
    lines = []
    for index, (name, fields) in enumerate(states, start=1):
        interesting = " ".join(
            f"{key}={fields[key]}"
            for key in (
                "pc",
                "running",
                "limit",
                "outcome",
                "summaryLanded",
                "willVerify",
                "exitVerified",
                "archivedPre",
            )
            if key in fields
        )
        lines.append(f"  {index}. {name}  {interesting}")
    return "\n".join(lines)


def narrate(states: list[tuple[str, dict[str, str]]]) -> str:
    sentences = []
    for name, fields in states:
        if name == "TurnEnds":
            kind = fields.get("outcome", "?").strip('"')
            which = "summary" if fields.get("summaryLanded") == "TRUE" else "child"
            sentence = f"The {which} turn ends with outcome {kind}."
        else:
            sentence = ACTIONS.get(name)
        if sentence and sentence not in sentences:
            sentences.append(sentence)
    if not states:
        return ""
    final = states[-1][1]
    via_text_since = any(name in {"PostGraceReadSettles", "BoundedSummaryReadSettles"} for name, _fields in states)
    if final.get("archivedPre") == "TRUE" and via_text_since:
        sentences.append(
            "textSince returned the opening line from before the summary prompt, "
            "and finishChild archived it with "
            f"exit_verified={final.get('exitVerified', '?')}."
        )
    elif final.get("archivedPre") == "TRUE":
        sentences.append(
            "The ledger records that finish with the opening line archived "
            "(text from before any summary prompt) and "
            f"exit_verified={final.get('exitVerified', '?')}."
        )
    elif final.get("exitVerified") == "TRUE" and final.get("running") == "TRUE":
        sentences.append(
            "The ledger records exit_verified true while the child turn is still running."
        )
    return " ".join(sentences)


def tlc_summary(text: str) -> str:
    kept = []
    saw_checked = False
    for line in text.splitlines():
        if CHECKED_MARK in line:
            if not saw_checked:
                kept.append(line.strip())
                saw_checked = True
            continue
        if (
            line.startswith("Error:")
            or line.startswith("TLC2 Version")
            or line.startswith("Model checking completed")
            or "distinct states" in line
            or line.startswith("Finished in")
            or line.startswith("The depth of")
            or "fingerprint" in line
            or line.startswith("  calculated")
            or line.startswith("  because two")
            or "Estimates of the probability" in line
        ):
            kept.append(line)
    return "\n".join(kept)


def run_tlc(jar: Path, config: str) -> tuple[str, str]:
    """Return ('pass'|'violation'|'error'|'timeout'|'unchecked', output)."""
    try:
        with tempfile.TemporaryDirectory(prefix="tlc-specialist-") as meta:
            command = [
                "java",
                "-XX:+UseParallelGC",
                "-jar",
                str(jar),
                "-workers",
                "1",
                "-cleanup",
                "-nowarning",
                "-metadir",
                meta,
                "-config",
                str(SPEC_DIR / config),
                SPEC.name,
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=SPEC_DIR,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=TLC_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as error:
                output = error.stdout or ""
                if isinstance(output, bytes):
                    output = output.decode("utf-8", "replace")
                return "timeout", output
            except FileNotFoundError:
                fail("java is not on PATH. TLC needs a JVM. This is not a verdict on the product.", EXIT_SETUP)
            output = completed.stdout or ""
            returncode = completed.returncode
    finally:
        for leftover in SPEC_DIR.glob("SpecialistChildLifecycle_TTrace_*"):
            leftover.unlink()
    if TLC_BANNER not in output:
        fail(
            "TLC output does not identify itself as "
            f"{TLC_BANNER}. The jar that ran is not the pinned {TLC_RELEASE} checker. "
            "Refusing to treat the result as a pass or a counterexample.\n"
            + "\n".join(output.splitlines()[:8]),
            EXIT_SETUP,
        )
    if not cfg_declares_safe((SPEC_DIR / config).read_text(encoding="utf-8")):
        return "error", output
    checked = CHECKED_MARK in output or "Invariant Safe is violated" in output
    if "not completely specified" in output or "Deadlock reached" in output:
        return "error", output
    if "Invariant Safe is violated" in output:
        if CHECKED_MARK not in output:
            return "error", output
        return "violation", output
    if "No error has been found" in output and returncode == 0 and checked:
        return "pass", output
    if "No error has been found" in output and CHECKED_MARK not in output:
        return "unchecked", output
    return "error", output


def report(title: str, status: str, output: str) -> None:
    print(f"\n=== {title}: {status} ===")
    print(tlc_summary(output))
    states = parse_trace(output)
    if states:
        print(compact_trace(states))
        print(narrate(states))


def main() -> None:
    if not SPEC.is_file():
        fail(f"Missing {SPEC}", EXIT_SETUP)
    if shutil.which("git") is None:
        fail("git is not on PATH. The fingerprint compares covered functions to the sync commit.", EXIT_SETUP)
    commit = sync_commit()
    check_drift(commit)
    if shutil.which("java") is None:
        fail("java is not on PATH. TLC needs a JVM. This is not a verdict on the product.", EXIT_SETUP)
    check_pre28_commit()
    jar = ensure_jar()
    for _title, config, _want in CHECKS:
        require_invariant(config)
    print(f"TLC release {TLC_RELEASE} (TLC {TLC_VERSION}, rev {TLC_REVISION})")
    print(f"jar sha256 {JAR_SHA256}")
    print(f"Covered function bodies match sync commit {commit}")

    code_failed = False
    for title, config, want in CHECKS:
        status, output = run_tlc(jar, config)
        report(f"{title} ({config})", status, output)
        if status == "timeout":
            fail(
                f"TLC exceeded {TLC_TIMEOUT_SECONDS}s on {config}. "
                "This is a tool failure, not a verdict on the product.",
                EXIT_MODEL,
            )
        if status == "unchecked":
            fail(
                f"TLC reported no error for {config}, but the output never shows that invariant Safe was checked. "
                "A pass without that check is not a pass.",
                EXIT_MODEL,
            )
        if status == "error":
            fail(
                "TLC failed while checking the model. Treat this as a modeling or tool error, "
                "not as a verdict on the product, until the spec parses and every action assigns every variable.",
                EXIT_MODEL,
            )
        if status != want:
            if title == "code on disk" and status == "violation":
                code_failed = True
                continue
            fail(
                f"{title} was {status}, expected {want}. "
                "If a known break started passing, the model no longer describes that bug.",
                EXIT_MODEL,
            )
        if title == "code on disk" and status == "violation":
            code_failed = True

    if code_failed:
        fail(
            "Safe does not hold for the specialist lifecycle code on disk. "
            "The trace above is a counterexample. Do not weaken the invariant.",
            EXIT_COUNTEREXAMPLE,
        )
    print(
        "\nSafe holds for the code on disk. TLC checked invariant Safe. "
        "The pre-#28 model, the removed-busy-check model, and the textSince-returns-every-message "
        "model still produce counterexamples."
    )


if __name__ == "__main__":
    main()
