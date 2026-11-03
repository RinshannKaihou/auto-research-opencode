---
name: verify-specialist-child-lifecycle
description: Use when changing specialist child stop, stall, summary, busy probe, or exit_verified recording in plugin/src/specialists.ts or the specialist event wiring in plugin/index.ts. Use when a change might record a running specialist as exited, or archive text from before the summary prompt.
---

# Verify specialist child lifecycle

## Property

A specialist child whose turn is still running is never recorded with `exit_verified: true`. When a stall or the absolute cap ends the run, the archived report is not text written before the summary prompt.

Harm: the node can treat a live child as finished, or can store an opening progress line as the expert report (issue #27).

## When to run

Run `make verify-specialist-lifecycle` after any edit under the covered functions below. Skip it when the diff does not touch those functions or `plugin/index.ts`'s specialist event and busy wiring.

## Run

From the repo root:

```bash
make verify-specialist-lifecycle
```

That runs TLC (TLA+ release v1.8.0, tool version 2026.11.02.164701, jar sha256 `c57d4d7bbee3ed2c89c32cb9c60cac728f751e80e2570bc4b7368ae39291fc21`) on `spec/specialist-child-lifecycle/SpecialistChildLifecycle.tla`. The first run downloads the jar into `spec/specialist-child-lifecycle/.cache/` (gitignored) and checks the sha256. Java must be on `PATH`. The three checks finish in about a second after the jar is cached.

The script exits 0 when the code on disk satisfies invariant `Safe` and the checker still finds counterexamples for the pre-#28 behavior and for the model with the busy check removed. The script exits 1 when `Safe` fails for the code on disk: the trace is a counterexample, and the last paragraph says `Safe` does not hold. The script exits 2 when the model or the tool failed; that is not a product-bug verdict. GNU make prints that script status as `Error 1` or `Error 2` and then exits 2 itself whenever the script did not exit 0. Read the script's last paragraph, not make's own status, to tell a counterexample from a modeling error.

TLA+ is the tool because the question is which orders of stall, cap, interrupt, summary send, message read, busy probe, and ledger write can happen. Nothing in this repo verifies TypeScript directly.

## How to read a counterexample

The command prints one line per TLC state: the action name, then `pc`, `running`, `limit`, `outcome`, `summaryLanded`, `willVerify`, `exitVerified`, and `archivedPre`. The sentence under the trace is the same run in the code's words. `running=TRUE` is a child turn still in progress. `exitVerified=TRUE` is the ledger's `exit_verified`. `archivedPre=TRUE` means a stall or cap finish archived the opening line from before the summary prompt.

Two traces are printed on every successful run, and they are supposed to fail:

- `pre28.cfg` is main at `8a2c6c9` (issue #27). The cap fires, the turn ends `interrupted`, and `specialist_finish` archives the opening line with `exit_verified` true. There is no summary turn in that code.
- `noBusyCheck.cfg` is the current code with the `childBusy` check in `summarize` removed. The summary send is still pending, `stop()` has already seen idle, the send then lands and starts a turn, and the ledger records `exit_verified` true while `running` is true.

## Real bug or modeling error

| What you see | What it is |
|---|---|
| Exit 1, and the printed variant matches the code you just edited | A real counterexample. Report it in the code's words. Do not weaken `Safe` or delete the action that produced it. |
| Exit 2, or "Model is out of sync", or TLC says a successor state is not completely specified, or TLC reports deadlock | A modeling error. The spec no longer matches the awaits. Fix the spec. Do not treat it as a product bug. |
| A trace needs `host.wait` to resolve while `running` is true, a completed busy probe to return false while `running` is true, or a send whose promise already settled to land later | Outside the model's host assumptions. Fix the model if the host really does that; do not call it a product bug until the model allows that host step and TLC still violates `Safe`. |
| `pre28.cfg` or `noBusyCheck.cfg` starts passing | The model no longer describes that break. Repair the variant before trusting a pass on the current code. |

## How to update the model

1. Read the diff of the covered functions. Each TLC action is one await boundary; code between awaits stays inside the action that resumes. Comments in `SpecialistChildLifecycle.tla` name the file and line.
2. Change the action that no longer matches. Keep the names (`running`, `limit`, `exitVerified`, `archivedPre`) so the invariant still means the same thing.
3. If you removed the busy check on purpose, the command's "code on disk" run must fail the way `noBusyCheck.cfg` fails. That is the scratch break.
4. Run `make verify-specialist-lifecycle`.
5. Move the sync commit below only after this command has been run against the new code and the actions still describe it. A pass on a stale model does not count. If the new code violates `Safe`, leave the sync commit where it is and report the counterexample.

## Coverage

Sync commit: `fb1b19b9d33723f9a35cb87f23a2e198595a060b`

Confirmed against `plugin/src/specialists.ts` and the specialist wiring in `plugin/index.ts` at that commit. This verification does not change those files. `git diff fb1b19b9d33723f9a35cb87f23a2e198595a060b -- plugin/src/specialists.ts plugin/index.ts` must be empty before you treat a pass as applying to later product edits; if it is not, follow the update steps and then change this SHA.

| Code | What the model includes |
|---|---|
| `Specialists.armLimits` `plugin/src/specialists.ts:456-487` | Stall only after a quiet window; progress resets it (`noteProgress` at `251-253`, wired from `plugin/index.ts:432`). Cap fires even when progress continues. |
| `Specialists.finished` `491-502` | Race of `host.wait` against the abort. Idle returns true. Abort calls `stop` with the same wait. |
| `Specialists.stop` `504-514` | Interrupt, then race the wait against the grace. Grace means not verified. |
| `Specialists.finishChild` `600-676` | Limit plus a non-success outcome calls `summarize` unless the parent already aborted. Otherwise `exit_verified` is the `finished`/`stop` result, and an unverified exit does not archive the opening line. |
| `Specialists.summarize` `521-597` | Send versus summary grace. Unsettled send: stop, then the message read, then the busy check at `574`. Settled send: `finished` on the summary controller, then `textSince` (`195-199`) at `586` and `590`. A rejected send calls `stop` and cannot land afterwards. |
| `Specialists.childBusy` `273-283` and `probeWithin` `186-192` | Hang, timeout, or throw counts as busy. |
| `plugin/index.ts:276-286` `host.busy` and `within` `30-34` | A probe that does not return is not proof of idle (`undefined` becomes true). |
| `Specialists.settle` `706-710` | The ledger write of `exit_verified`. |
| pre-#28 `plugin/src/specialists.ts` at `8a2c6c9:252-255`, `294-318`, `351-373` | Absolute timer only. `sessionResult` text (`113-126`) is archived, including the opening line, with `exit_verified` from `stop`/`finished`. No summary and no busy check. |

`noteBusy` (`plugin/src/specialists.ts:256-265`, wired from `plugin/index.ts:433-437`) is not a separate state. On this host `childBusy` calls `host.busy`, which reads `session.busy`, and a hung probe does not fall back to the event map.

## Bounds and assumptions

- One child, already running after kickoff. The transcript already contains the issue #27 opening line.
- Time is event order, not milliseconds. Stall, cap, summary grace, stop grace, and the 2-second probe are competing events.
- `host.wait` resolves only when `running` is false (the test host: wait completes when the session is not busy). If the abort and the idle resolution are both pending, either may win.
- Accepting the summary message and starting its turn are one host step. The plugin promise can stay pending after that. A promise that has resolved or rejected does not land later.
- A completed busy probe reports `running` at that step. A hang, timeout, or throw counts as busy.
- Turn outcomes are `succeeded` or `interrupted`. Any other non-success outcome takes the same `summarize` branch as `interrupted`.
- `current()` stays true. A false `current()` returns without `specialist_finish`, so it cannot record `exit_verified: true`.
- The parent abort, the stall, the cap, the summary grace, and `stop`'s grace are the aborts in the model.

Checked state space on the sync commit: current variant 902 distinct states, depth 15; TLC's optimistic fingerprint-collision estimate was `3.2E-14`.

## Not covered

- `delegate`, `reserve`, launch phases, `host.create`, bind, and kickoff.
- `delegateBatch` and a second child.
- The `run` catch path (`plugin/src/specialists.ts:422-445`) that stops a child after a create, bind, or archive failure.
- `recover`, `recoverWarm`, `watchRecovered`, and `interruptUnverified`.
- A parent abort that is not a stall or a cap archiving the turn's own text. Property 2 is only the stall/cap archive.
- A succeeded turn that is also past the cap keeping its own report text. That text is the turn's report, not a timeout summary.
- `host.busy` missing, so `childBusy` would read `appearsBusy` from events. `plugin/index.ts` always installs `host.busy`.
- A busy probe that returns false while the turn is running.
- Tool permissions, the ledger's other fields, and real OpenCode process exit beyond `running`.

## Not in CI

`.github/workflows/ci.yml` runs `make test` only. This target is not part of `make test`: the workflow does not install the pinned TLC jar, and a model failure is a different check from pytest, bun, and tsc. Run this command when a change touches the covered functions. It is short enough to run on each of those changes.
