---
name: verify-specialist-child-lifecycle
description: Use when changing specialist child stop, stall, summary, busy probe, textSince, or exit_verified recording in plugin/src/specialists.ts or the specialist event wiring in plugin/index.ts. Use when a change might record a running specialist as exited, or archive text from before the summary prompt.
---

# Verify specialist child lifecycle

## Property

A specialist child whose turn is still running is never recorded with `exit_verified: true`. When a stall or the absolute cap ends the run, the archived report is not text written before the summary prompt.

Harm: the node can treat a live child as finished, or can store an opening progress line as the expert report (issue #27).

## When to run

Run `make verify-specialist-lifecycle` after any edit under the fingerprinted functions below. Skip it when the diff does not touch those functions.

## Run

From the repo root:

```bash
make verify-specialist-lifecycle
```

That runs TLC from the stable TLA+ release v1.7.4 (TLC 2.19 of 08 August 2024, rev `5a47802`, jar sha256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`) on `spec/specialist-child-lifecycle/SpecialistChildLifecycle.tla`. The first run downloads the jar into `spec/specialist-child-lifecycle/.cache/` (gitignored), checks the sha256, and checks that the jar's manifest tag is `v1.7.4` and that TLC's banner is `TLC2 Version 2.19 of 08 August 2024 (rev: 5a47802)`. Java and git must be on `PATH`. This clone must contain sync commit `fb1b19b9d33723f9a35cb87f23a2e198595a060b` and pre-#28 commit `8a2c6c9`. The four checks finish in a few seconds after the jar is cached. TLC is killed if one check exceeds 120 seconds.

The script exits with its own status. GNU make prints that status as `Error N` and then exits 2 itself. Read the script's status and its first word (`DRIFT`, `COUNTEREXAMPLE`, `MODEL`, `SETUP`), not make's own exit code.

| Script exit | First word | Meaning |
|---|---|---|
| 0 | (none; the last paragraph says Safe holds) | Covered bodies match the sync commit, TLC checked invariant `Safe` for the code on disk, and the three known breaks still violate `Safe`. |
| 1 | `COUNTEREXAMPLE` | `Safe` failed for the code the model describes. The trace is a counterexample. Do not weaken `Safe`. |
| 2 | `MODEL` | The spec, a config, or TLC failed, a config has no `INVARIANT Safe`, TLC's output never shows that `Safe` was checked, TLC timed out, or a known break started passing. Not a product-bug verdict. |
| 3 | `DRIFT` | A fingerprinted function body differs from the sync commit. Re-sync the model before trusting a pass. This is not a counterexample. A same-meaning rewrite is drift too. |
| 4 | `SETUP` | git or java is missing, a required commit is not in this clone, or the TLC jar failed to download or is not release v1.7.4. |

TLA+ is the tool because the question is which orders of stall, cap, interrupt, summary send, message read, busy probe, and ledger write can happen. Nothing in this repo verifies TypeScript directly. The fingerprint is what keeps that model tied to the TypeScript: a pass is refused when the covered bodies have changed, even if TLC would still accept the old model.

## How to read a counterexample

The command prints one line per TLC state: the action name, then `pc`, `running`, `limit`, `outcome`, `summaryLanded`, `willVerify`, `exitVerified`, and `archivedPre`. The sentence under the trace is the same run in the code's words. `running=TRUE` is a child turn still in progress. `exitVerified=TRUE` is the ledger's `exit_verified`. `archivedPre=TRUE` means a stall or cap finish archived the opening line from before the summary prompt.

Three traces are printed on every successful run, and they are supposed to fail:

- `pre28.cfg` is main at `8a2c6c9` (issue #27). The cap fires, the turn ends `interrupted`, and `specialist_finish` archives the opening line with `exit_verified` true. There is no summary turn in that code. The cap does not wait for the parent abort: the timer at `8a2c6c9:252-255` fires anyway.
- `noBusyCheck.cfg` is the current code with the `childBusy` check in `summarize` removed. The summary send is still pending, `stop()` has already seen idle, the send then lands and starts a turn, and the ledger records `exit_verified` true while `running` is true.
- `textSinceAll.cfg` is the current code with `textSince` returning `sessionResult` of the whole transcript. The summary turn ends `interrupted`, so it adds no assistant text, `textSince` therefore returns the opening line, and `finishChild` archives that line. A summary turn that ends `succeeded` is modeled as having written later text, and that path does not set `archivedPre`.

A pass prints `Checked invariant Safe` for the code on disk. If that sentence is missing, or a config has no `INVARIANT Safe` line, the script exits 2 even when TLC says it found no error.

## Real bug or modeling error

| What you see | What it is |
|---|---|
| Exit 1, and the sync commit still matches the code TLC just checked | A real counterexample. Report it in the code's words. Do not weaken `Safe` or delete the action that produced it. |
| Exit 3, `DRIFT` | The covered bodies changed. Do not treat TLC as having passed or failed on the new code. Follow the update steps, then move the sync commit. |
| Exit 2, or TLC says a successor state is not completely specified, or TLC reports deadlock, or a known break started passing | A modeling error. Fix the spec. Do not treat it as a product bug. |
| Exit 4, `SETUP` | The tool or the git history is missing. Fix that and rerun. Not a product bug. |
| A trace needs `host.wait` to resolve while `running` is true, a completed busy probe to return false while `running` is true, or a send whose promise already settled to land later | Outside the model's host assumptions. Fix the model if the host really does that; do not call it a product bug until the model allows that host step and TLC still violates `Safe`. |
| `pre28.cfg`, `noBusyCheck.cfg`, or `textSinceAll.cfg` starts passing | The model no longer describes that break. Repair the variant before trusting a pass on the current code. |

## How to update the model

1. Read the diff of the fingerprinted functions. Each TLC action is one await boundary; code between awaits stays inside the action that resumes. Comments in `SpecialistChildLifecycle.tla` name the file and line.
2. Change the action that no longer matches. Keep the names (`running`, `limit`, `exitVerified`, `archivedPre`) so the invariant still means the same thing.
3. If you removed the busy check on purpose, the model of that code must fail the way `noBusyCheck.cfg` fails. If you changed `textSince` so it can return the opening line, it must fail the way `textSinceAll.cfg` fails. Those are the scratch breaks. A code edit alone exits 3 until this model is updated and the sync commit moves; it does not by itself select those variants.
4. Run `make verify-specialist-lifecycle`.
5. Move the sync commit below only after this command has been run against the new code and the actions still describe it. A pass on a stale model does not count. If the new code violates `Safe`, leave the sync commit where it is and report the counterexample. Moving the commit without updating the actions makes the next pass apply to code the model may not describe.

## Coverage

Sync commit: `fb1b19b9d33723f9a35cb87f23a2e198595a060b`

Confirmed against the fingerprinted functions in `plugin/src/specialists.ts` and `plugin/index.ts` at that commit. This verification does not change those files. The script hashes each function's signature and body (not the comment above it) and compares the hash to `git show` of this commit. If any hash differs, the script exits 3.

| Code | What the model includes |
|---|---|
| `sessionResult` `plugin/src/specialists.ts:169-179` and `messageID` `181-183` | Which assistant text and outcome a read returns. `textSince` and `finishChild` both use them. |
| `textSince` `195-199` | Text after the summary id only. The `textSinceAll` variant returns `sessionResult` of every message. |
| `probeWithin` `186-192` | A host call that must return; the fallback is used when it outlives the bound. |
| `Specialists.noteProgress` `251-253`, wired from `observeSpecialist` `plugin/index.ts:429-438` | Progress resets the stall window. |
| `Specialists.armLimits` `456-488` | Stall only after a quiet window. Cap fires even when progress continues. A parent abort already aborted the work controller, so the current cap does not mark. |
| `Specialists.finished` `491-502` | Race of `host.wait` against the abort. Idle returns true. Abort calls `stop` with the same wait. |
| `Specialists.stop` `504-514` | Interrupt, then race the wait against the grace. Grace means not verified. |
| `Specialists.finishChild` `600-676` | Limit plus a non-success outcome calls `summarize` unless the parent already aborted. Otherwise `exit_verified` is the `finished`/`stop` result, and an unverified exit does not archive the opening line. A non-empty `summary.text` is archived. |
| `Specialists.summarize` `521-597` | Send versus summary grace. Unsettled send: stop, then the message read, then the busy check at `574`. Settled send: `finished` on the summary controller, then `textSince` at `586` and `590`. A rejected send calls `stop` and cannot land afterwards. |
| `Specialists.childBusy` `273-283` | Hang, timeout, or throw counts as busy. |
| `plugin/index.ts:276-286` `host.busy` and `within` `30-34` | A probe that does not return is not proof of idle (`undefined` becomes true). |
| `Specialists.settle` `706-711` | The ledger write of `exit_verified`. |
| pre-#28 `plugin/src/specialists.ts` at `8a2c6c9:252-255`, `294-318`, `351-373` | Absolute timer only. It sets `timedOut` and aborts with no `signal.aborted` check, so a parent abort does not stop it. `sessionResult` text (`113-126`) is archived, including the opening line, with `exit_verified` from `stop`/`finished`. No summary and no busy check. |

`noteBusy` (`plugin/src/specialists.ts:256-265`, wired from `observeSpecialist`) is not a separate state. On this host `childBusy` calls `host.busy`, which reads `session.busy`, and a hung probe does not fall back to the event map.

## Bounds and assumptions

- One child, already running after kickoff. The transcript already contains the issue #27 opening line, and that line stays the last assistant text until a summary turn ends `succeeded`. An interrupted summary turn adds no assistant text.
- Time is event order, not milliseconds. Stall, cap, summary grace, stop grace, and the 2-second probe are competing events.
- `host.wait` resolves only when `running` is false (the test host: wait completes when the session is not busy). If the abort and the idle resolution are both pending, either may win.
- Accepting the summary message and starting its turn are one host step. The plugin promise can stay pending after that. A promise that has resolved or rejected does not land later.
- A completed busy probe reports `running` at that step. A hang, timeout, or throw counts as busy.
- Turn outcomes are `succeeded` or `interrupted`. Any other non-success outcome takes the same `summarize` branch as `interrupted`.
- `current()` stays true. A false `current()` returns without `specialist_finish`, so it cannot record `exit_verified: true`.
- The parent abort, the stall, the cap, the summary grace, and `stop`'s grace are the aborts in the model.
- `textSinceAll` is only the "return every message" break. A correct `textSince` never archives the opening line. A succeeded summary turn is later text, so returning every message still does not archive the opening line on that path.

Checked state space on the sync commit, current variant: 902 distinct states, depth 15. TLC's optimistic fingerprint-collision estimate was `3.2E-14`.

## Not covered

- `delegate`, `reserve`, launch phases, `host.create`, bind, and kickoff.
- `delegateBatch` and a second child.
- The `run` catch path (`plugin/src/specialists.ts:422-445`) that stops a child after a create, bind, or archive failure.
- `recover`, `recoverWarm`, `watchRecovered`, and `interruptUnverified`.
- A parent abort that is not a stall or a cap archiving the turn's own text. Property 2 is only the stall/cap archive.
- A succeeded turn that is also past the cap keeping its own report text. That text is the turn's report, not a timeout summary.
- `host.busy` missing, so `childBusy` would read `appearsBusy` from events. `plugin/index.ts` always installs `host.busy`.
- A busy probe that returns false while the turn is running. `childBusy` hard-coded to return false, an early `return` before the busy check, and dropping the `await` on `stopped()` are not separate TLC variants. They change a fingerprinted body, so the command exits 3. They become a counterexample only after the model is updated to describe them.
- An interrupted summary turn that still wrote assistant text. The model treats that turn as adding none, so `textSinceAll` archives the opening line.
- The comment above a fingerprinted function, and any edit outside the fingerprinted functions. Those are not drift and are not checked.
- Tool permissions, the ledger's other fields, and real OpenCode process exit beyond `running`.

## Not in CI

`.github/workflows/ci.yml` runs `make test` only. This target is not part of `make test`: the workflow does not install the pinned TLC jar, and a model failure is a different check from pytest, bun, and tsc. Run this command when a change touches a fingerprinted function. It is short enough to run on each of those changes. `tests/test_verify_specialist_lifecycle.py` checks that the fingerprint extractor sees a body edit; it does not run TLC.
