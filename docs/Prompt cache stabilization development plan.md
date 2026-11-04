# Prompt cache placement: restart plan

2026-11-04. Status: Step 1, its focused Step 2 checks, and one Step 3 real-API pair are done on `cursor/prompt-cache-stabilization`. The pair is recorded in [evidence/context-cache/step3/README.md](./evidence/context-cache/step3/README.md); the tail coordinator showed repeated waiting whose relationship to placement remains undetermined, and the default remains `"system"` because the default-change gate (both exploration settings, repeats, no regressions) is not met.

Remaining PR #35 review work is governed by the [review remediation plan](<./Prompt cache PR 35 review remediation plan.md>). Its merge and experiment gates supersede the earlier follow-up recommendations below. The review dispositions and checks are recorded in [evidence/context-cache/review-response.md](./evidence/context-cache/review-response.md).

Source: [Plan: keep the prompt cache valid in auto-research-opencode](<./Plan keep the prompt cache valid in auto-research-opencode.md>). The source analysis is preserved unchanged. This document defines the first implementation and replaces the earlier nine-work-package development plan.

## 1. Restart point

- Code baseline: `fd1c62d` / v0.2.1.
- Development checkout: this feature worktree; no local absolute path is part of the public record.
- Development branch: `cursor/prompt-cache-stabilization`.
- Previous development is preserved at `15765cd` on `cursor/prompt-cache-before-restart-20261104`. Its tools, tests, evidence framework, and implementation plan are retired from the active branch. Do not copy them back as prerequisites.
- The earlier local dirty checkout used before this restart is not a development baseline and must retain its unrelated changes.

Follow the checkout's `AGENTS.md`. Keep each step reviewable; use `make test`; do not push main, merge, force-push, release, or restart a shared service automatically. No work directory may be created directly under `~/work`.

## 2. First delivery

Implement the source plan's **option A**, behind `contextPlacement: "system" | "tail"`, default `"system"`.

On each research model request, fetch the same fresh context as today. In tail mode, put its stable blocks in system and its changing blocks in one temporary user message after the persistent conversation. Keep the original full text for delivery checks.

Preserve the current backend, context policies, content selection, and 12,000-character budget. The first delivery is a placement change. Budget coupling may still cause some stable blocks to change or disappear; measure that limitation before considering the source plan's Phase 2.

Target measurable reduction in unnecessary uncached input. Do not promise a particular hit rate, an identical prefix across every state transition, or lower output tokens. Input/cache fields and report quality must be observed on the actual API configuration.

## 3. Scope decisions

| Keep in this delivery | Defer |
|---|---|
| Fresh context on every primary request | Turn snapshots / option B |
| TypeScript block splitting using the existing format | Python renderer changes, extra budgets, new layout protocol |
| One correct history cache boundary | Previous-boundary locators and two-marker optimization |
| Existing checkpoint/evidence/control/isolation semantics | General packet/receipt refactoring |
| Default system mode and simple rollback | Default change |
| Focused tests and one actual API comparison | Multi-provider certification, capability registry, E3 evaluation integration |
| API-reported usage | External tokenizers, custom pricing engines, native-runner framework |

Do not implement separate cost caps, native calibration protocols, a snapshot-analysis subsystem, or a new telemetry schema to deliver this optimization. A real API trial uses an explicitly chosen test goal, model, and small output limit; report measured usage and any estimates honestly.

## 4. Step 1 — implement the small opt-in change

Read the source plan's Phase 1 and the existing `injectContext`, `Research.memory`, `Research.delivered`, `CheckpointDelivery.request`, and `EvidenceDelivery.request` functions. Resolve symbols on the baseline rather than copying old line numbers.

Expected production changes:

1. Add `plugin/src/context-placement.ts` for splitting, temporary-message construction, and the small cache-hint helper. Do not export helpers from `plugin/index.ts`.
2. Add the option and injection branch in `plugin/index.ts`.
3. Keep `Research.memory()` and `Research.delivered()` signatures and the original unsplit text unless a concrete integration issue requires a small, justified change. No request-local packet framework is planned.

Use the source plan's stable allowlist once in TypeScript:

```text
role_instructions, control_instructions, identity, project,
task, specialist_task, frozen_context, method_guidance, index
```

Everything else, including future unknown blocks, is volatile. Split the existing `## name\n...` format without rewriting block values. Keep the `Research context:` header with the stable part. Every rendered block must appear exactly once. Do not parse and reserialize ledger JSON merely to relocate it.

Blockless blind/safety context stays in system. Stale or unavailable-control responses retain the existing system placement and warnings; use the original path rather than reinterpreting their safety instructions. Preserve the current tool filtering and node authorization checks.

For normal tail requests:

1. Retain the persistent conversation before appending anything.
2. Fetch the context once and split its rendered text.
3. Prepare the existing checkpoint/evidence delivery checks with the original full context and unchanged tool results.
4. Put stable text in system. Collect volatile text and any evidence-delivery warning in one clearly labeled `<research_state>` user message.
5. Append that message after all existing tool results. Do not persist it through the synthetic-message/session APIs.

Do not change model choice, tool definitions, ledger protocol, cache TTL, or output formatting. Invalid option values use system mode and warn once.

### The necessary addition to the source plan: history cache boundary

On explicit-cache APIs, a breakpoint on the temporary tail does not establish a reusable history entry: that tail disappears before the next request. Place a supported SDK cache hint on a legal persistent-history content part **before** the tail, and verify its final wire position.

Use the existing SDK's lowering, preserve existing hints, and respect its marker limit. Clone edited message/part objects; preserve text, tool IDs/results, and reasoning/signatures. Skip unsupported parts. If no legal boundary or marker slot is available, use the original system path for that request. Do not rewrite HTTP payloads or build a marker registry.

For an implicit-cache API, let the SDK omit inline hint fields and assess reuse through the API's usage response. Document the route actually tested; do not infer protocol behavior from a provider display name or build a per-model allowlist.

The earlier experiment observed primary-only `context` hooks on OpenCode 2.2.5; auxiliary requests used their own hooks. Recheck the chosen host during the focused smoke test. Do not introduce a mutable `lastKind` guard.

## 5. Step 2 — verify the actual change

Reuse the existing fake-host harness and private-host probe patterns. Add only the small fixture or option handling needed to exercise the implementation. Do not restore the retired 2,196-line probe or its native mode.

Focused tests must establish:

- R1 and legacy context split correctly; unknown blocks are volatile; blockless/stale safety text stays in system; block content is preserved exactly once.
- Node ledger writes update the tail while unchanged stable blocks remain byte-identical in system. Existing legitimate control/tool changes remain allowed.
- Checkpoint bases and evidence receipts still work; evidence warnings appear in the intended location; tool-result content is unchanged.
- Blind specialists receive no project tail, and non-research sessions remain unaffected.
- Pause, stop, unknown-control, and missing-drain restrictions still apply.
- Default system mode preserves existing behavior.

One isolated-host smoke test must show that the temporary message reaches the final provider request, is absent from persisted history and the next request's history, follows complete tool results, and has the intended preceding cache boundary where applicable. Check auxiliary requests and the actual SDK marker limit in that test.

Run focused tests and `make test`. Use source and packed probes already required by the repository when packaging is affected; no separate packaging framework is needed. Record a short result note with revision, host/API route, commands, outcome, and limitations. Do not create a work-package evidence hierarchy.

Update `README.md`, `plugin/README.md`, and `CHANGELOG.md` narrowly for the option and its default. Do not bump versions or broaden compatibility claims without evidence.

## 6. Step 3 — compare through the actual API

After engineering checks pass, compare `"system"` and `"tail"` on the same representative research goal with the same model, reasoning settings, context policy, exploration setting, concurrency, and budgets. Use separate research projects so one run's ledger does not affect the other. Start with the API configuration the user actually uses; repeat if results are noisy.

Use the provider/host's returned usage. Check once how its input, cache-read, cache-write, output, and reasoning fields relate before adding them. If a field is missing, report it unavailable. No local tokenizer is needed. A small per-session grouping in the existing `scripts/r2_measure.py` is optional if the API/host output is otherwise inconvenient; do not rebuild the retired measurement subsystem.

Compare:

- Uncached input, cache reads/writes, output/reasoning, and model-request count.
- Completion, checkpoint/evidence rejections, unexpected pauses/retries, and whether the report answers the task with evidence.
- Any observed stable-block changes caused by budget coupling.

Report the complete run as well as coordinator/node sessions. Cached tokens still count as prompt tokens; higher cache reuse does not by itself mean fewer total input or output tokens. Use actual reported cost where meaningful, and label estimates separately. Do not require a universal percentage improvement or claim statistical proof from a small trial.

If the selected API rejects the message sequence or reasoning replay, keep system mode for it and report the incompatibility. Do not automatically substitute turn freezing. Keep system as the default until the actual trial supports a separate default-change decision.

## 7. Later work needs evidence

Revisit the source plan's Phase 2 only if renderer budget coupling materially limits the measured improvement. Revisit Phase 3 only if tool-schema changes materially explain remaining misses and the user accepts the visibility change. Output-token overhead, isolated-session rereading, and cache TTL are separate investigations.

Repeated waiting occurred in the tail arm: 104 `research_wait` calls (102 as the sole tool, median 2.1 s apart). The wait tool permits continued steps; placement's contribution remains undetermined. A temporary user message could interact with model behavior, and this pair cannot exclude that. The bounded tail-only wait and node-finish completion in the remediation plan is implemented and locally verified, but it has not been checked on a real model route; that check belongs to the later gate.

The stable allowlist is unchanged. The full-system finishing request carries the waiting variant of `project`; confirmed wake clears waiting, so regular tail requests are expected to regain their pre-wait stable prefix while the cache remains live. Verify stable digests across real wakes, accounting for other ledger and content-budget changes. This is expected mitigation, not an independently established blocker requiring a renderer split. Native acceptance of both tool-free finishing shapes remains a separate route gate; no real route is qualified yet.

Do not treat these follow-ups as prerequisites for the opt-in placement patch.

## Handoff prompt

```text
Implement Step 1 and its focused Step 2 checks from
docs/Prompt cache stabilization development plan.md, using
docs/Plan keep the prompt cache valid in auto-research-opencode.md as background.

Use cursor/prompt-cache-stabilization in the designated clean worktree, based on
fd1c62d plus the restart-plan commit. Inspect status and applicable AGENTS.md first.
Preserve unrelated changes. Keep the backend and 12,000-character budget unchanged.

Deliver only the opt-in TypeScript placement change, one history cache boundary,
the relevant regression tests, and concise docs. Keep system as the default.
Preserve full-text checkpoint delivery, evidence matching, blind isolation, and
execution controls. Do not restore the retired WP tooling, introduce tokenizers,
implement a native runner, redesign renderers, or add turn snapshots/tool changes.

Run focused tests and make test, then report files changed, commands/results,
and any actual unresolved blocker. No paid model trial, shared-service restart,
merge, release, or default change is part of this handoff.
```
