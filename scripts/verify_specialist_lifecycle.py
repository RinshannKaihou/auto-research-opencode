#!/usr/bin/env python3
"""Re-check the specialist child-session lifecycle model.

Exit 0: the code on disk matches the current model and TLC says Safe holds,
        and the two known breaks still violate Safe.
Exit 1: Safe is violated for the code on disk (a real counterexample).
Exit 2: the model is out of sync with the code, or TLC itself failed.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = ROOT / "spec" / "specialist-child-lifecycle"
SPEC = SPEC_DIR / "SpecialistChildLifecycle.tla"
JAR_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar"
JAR_SHA256 = "c57d4d7bbee3ed2c89c32cb9c60cac728f751e80e2570bc4b7368ae39291fc21"
TLC_RELEASE = "v1.8.0"
# `java -jar tla2tools.jar` reports this build for that GitHub release.
TLC_VERSION = "2026.11.02.164701"
PRE28 = "8a2c6c944ef3606face6cc834250e50a3bd5be1b"

BUSY_CHECK = (
    'if (exited && await this.childBusy(child)) return { text: "", exited: false }'
)
# These must still be the control flow the model describes. A missing busy
# check alone selects the noBusyCheck variant. Anything else is out of sync.
ANCHORS = {
    "plugin/src/specialists.ts": [
        "function textSince(",
        'mark("cap")',
        'mark("stall")',
        "return this.stop(child, idle)",
        "return probeWithin(probe, true)",
        "textSince(messages, summaryMessageID)",
        "private async summarize(",
    ],
    "plugin/index.ts": [
        "specialists.noteProgress(sessionID)",
        "specialists.noteBusy(sessionID, event.data?.status?.type)",
        "result === undefined ? true : !!result",
    ],
}

ACTIONS = {
    "CapTimerAborts": "The absolute cap aborts the work controller.",
    "StallTimerAborts": "The stall timer aborts the work controller.",
    "ProgressResetsStall": "A progress event resets the stall window.",
    "QuietWindowElapses": "The stall window elapses again.",
    "ParentAborts": "The delegating call aborts.",
    'TurnEnds("succeeded")': "The child turn ends with outcome succeeded.",
    'TurnEnds("interrupted")': "The child turn ends with outcome interrupted.",
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
    "PostGraceReadSettles": "The bounded message read after the summary grace returns textSince only.",
    "BoundedSummaryReadSettles": "The summary message read returns textSince only.",
    "RecordLedger": "specialist_finish records the result.",
    "DoneStutter": "The ledger write has finished.",
    "Initial predicate": "The child is running after kickoff, with the opening line already in the transcript.",
}


def fail(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_jar() -> Path:
    cache = SPEC_DIR / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    jar = cache / "tla2tools.jar"
    if jar.exists() and sha256(jar) == JAR_SHA256:
        return jar
    print(f"Downloading TLC {TLC_RELEASE} from {JAR_URL}")
    tmp = jar.with_suffix(".jar.partial")
    try:
        urllib.request.urlretrieve(JAR_URL, tmp)
    except Exception as error:  # noqa: BLE001 — surface the download failure as a tool error
        fail(f"Could not download TLC: {error}")
    digest = sha256(tmp)
    if digest != JAR_SHA256:
        tmp.unlink(missing_ok=True)
        fail(f"TLC jar sha256 {digest} does not match the pinned {JAR_SHA256}")
    tmp.replace(jar)
    return jar


def detect_variant() -> str:
    missing = []
    for relative, needles in ANCHORS.items():
        text = (ROOT / relative).read_text()
        for needle in needles:
            if needle not in text:
                missing.append(f"{relative} is missing {needle!r}")
    if missing:
        fail(
            "Model is out of sync with the code (this is a modeling error, not a product-bug verdict).\n"
            + "\n".join(missing)
            + "\nUpdate spec/specialist-child-lifecycle/SpecialistChildLifecycle.tla so each action "
            "matches the new await boundaries, then move the sync commit in the verify skill."
        )
    source = (ROOT / "plugin/src/specialists.ts").read_text()
    if BUSY_CHECK in source:
        return "current"
    return "noBusyCheck"


def check_pre28_commit() -> None:
    try:
        old = subprocess.check_output(
            ["git", "show", f"{PRE28}:plugin/src/specialists.ts"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as error:
        fail(f"Cannot read pre-#28 specialists.ts at {PRE28}:\n{error.output}")
    problems = []
    if "private async summarize" in old:
        problems.append("8a2c6c9 unexpectedly has summarize(); the pre28 model would be wrong")
    if "达到专家时间上限，已收回部分报告" not in old:
        problems.append("8a2c6c9 is missing the timeout partial-report copy the pre28 model archives")
    if "exit_verified: exited" not in old:
        problems.append("8a2c6c9 does not record exit_verified from the stop result")
    if problems:
        fail("pre28 fixture does not match commit 8a2c6c9:\n" + "\n".join(problems))


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
    for name, _fields in states:
        sentence = ACTIONS.get(name)
        if sentence and sentence not in sentences:
            sentences.append(sentence)
    if not states:
        return ""
    final = states[-1][1]
    if final.get("archivedPre") == "TRUE":
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
    for line in text.splitlines():
        if (
            line.startswith("Error:")
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
    """Return ('pass'|'violation'|'error', output)."""
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
        completed = subprocess.run(
            command,
            cwd=SPEC_DIR,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    output = completed.stdout
    for leftover in SPEC_DIR.glob("SpecialistChildLifecycle_TTrace_*"):
        leftover.unlink()
    if "not completely specified" in output or "Deadlock reached" in output:
        return "error", output
    if "Invariant Safe is violated" in output:
        return "violation", output
    if "No error has been found" in output and completed.returncode == 0:
        return "pass", output
    return "error", output


def report(title: str, status: str, output: str) -> None:
    print(f"\n=== {title}: {status} ===")
    print(tlc_summary(output))
    states = parse_trace(output)
    if states:
        print(compact_trace(states))
        print(narrate(states))


def main() -> None:
    if shutil.which("java") is None:
        fail("java is not on PATH; TLC needs a JVM")
    if not SPEC.exists():
        fail(f"Missing {SPEC}")
    variant = detect_variant()
    check_pre28_commit()
    jar = ensure_jar()
    print(f"TLC release {TLC_RELEASE} (tool version {TLC_VERSION})")
    print(f"jar sha256 {JAR_SHA256}")
    print(f"Code on disk matches model variant {variant}")

    # The variant that describes the code must meet Safe only when that code is current.
    code_expect = "pass" if variant == "current" else "violation"
    plan = [("code on disk", f"{variant}.cfg", code_expect)]
    plan.append(("pre-#28 main at 8a2c6c9", "pre28.cfg", "violation"))
    if variant == "current":
        plan.append(("busy check removed", "noBusyCheck.cfg", "violation"))

    code_failed = False
    for title, config, want in plan:
        status, output = run_tlc(jar, config)
        report(f"{title} ({config})", status, output)
        if status == "error":
            fail(
                "TLC failed while checking the model. Treat this as a modeling or tool error, "
                "not as a verdict on the product, until the spec parses and every action assigns every variable.",
                code=2,
            )
        if status != want:
            if title == "code on disk" and status == "violation":
                code_failed = True
                continue
            fail(
                f"{title} was {status}, expected {want}. "
                "If a known break started passing, the model no longer describes that bug.",
                code=2,
            )
        if title == "code on disk" and status == "violation":
            code_failed = True

    if code_failed or variant != "current":
        print(
            "\nSafe does not hold for the specialist lifecycle code on disk. "
            "The trace above is a counterexample. Do not weaken the invariant."
        )
        raise SystemExit(1)
    print(
        "\nSafe holds for the code on disk. "
        "The pre-#28 model and the removed-busy-check model still produce counterexamples."
    )


if __name__ == "__main__":
    main()
