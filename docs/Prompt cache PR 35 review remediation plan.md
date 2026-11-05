# PR #35: review remediation plan

2026-11-04. Status: Commits A–C and the follow-up node-finish correction are implemented and locally checked. The terminal handoff authorization and late-boundary corrections are implemented; the user-input handoff itself remains blocked by the pinned host's queue contract. No new real-provider API experiment has been run. See the [review response](./evidence/context-cache/review-response.md) for current results and open gates.

Target: PR #35, branch `cursor/prompt-cache-stabilization`, starting at `a463f91` (includes main/v0.2.2 at `6f36885`). Continue in the existing feature worktree. Preserve unrelated changes; follow `AGENTS.md`; sync main by merge when necessary. Do not modify the old dirty checkout, rewrite history, merge the PR, release, or restart the shared service.

For the subsequently reproduced input-handoff and transient-authorization defects at `555b449`, follow the [terminal handoff repair plan](<./Prompt cache terminal handoff repair plan.md>). The user chose native queuing of new inputs until the accepted terminal action finishes; this does not cancel pending node finishes.

This plan governs the remaining review work. The [original delivery plan](<./Prompt cache stabilization development plan.md>) remains background. Preserve the source diagnosis and historical trial evidence as historical records.

## 1. Review decisions and limits

Sources: [Chinese review and inline findings](https://github.com/RinshannKaihou/auto-research-opencode/pull/35#pullrequestreview-5765644305), [English review](https://github.com/RinshannKaihou/auto-research-opencode/pull/35#issuecomment-6627326516).

| Finding | Decision | Work |
|---|---|---|
| Existing-hint path mutates the host array | Confirmed; merge blocker | Commit A |
| Unknown-option warning not asserted | Accept | Commit A |
| Test name claims a delivered result; public plan contains local paths | Accept narrow cleanup | Commit A |
| Compaction might receive the tail | Verify before changing routing | Commit B |
| Reasoning replay not established | Verify wire preservation; retain native limitation | Commit B |
| Coordinator repeats waits rather than ending turns | Confirmed behavior; cause not isolated | Commit C and later API gate |
| `project` changes at wait/wake | Accept limitation; defer renderer change | Commit C tests and response |
| Savings from unequal work | Accept presentation correction | Response/evidence update |

Keep `contextPlacement: "system"` as default. Preserve fresh context, the 12,000-character budget, full-text delivery, checkpoint/evidence matching, blind isolation, and execution controls. No Python renderer/schema/budget changes, turn snapshots, provider registry, tokenizer, pricing engine, or replacement probe framework.

The bounded control exception is a tail-mode response after an accepted coordinator wait or node finish: completing that response requires withholding tools. This is terminal-action completion, not a general tool-schema caching change. System mode retains its existing behavior.

Preferred PR readiness: Commits A-C and the local checks below pass. If a verified host-contract gap blocks B or C, an experimental-only merge remains a separate reviewer decision after A and the warning; do not mark the blocked finding resolved. Normal tail-mode use requires the later real-route behavior check. Default enablement remains a separate decision. No PR merge or publication is authorized by this planning task.

## 2. Commit A — ownership bug, test gap, and user warning

Files: `plugin/index.ts`, placement/plugin tests, `README.md`, `plugin/README.md`, and the public delivery plan.

1. Make the final append allocate a new array: `request.messages = [...request.messages, researchStateMessage(tail)]`. Do not rely on the boundary helper returning a copy. Preserve unchanged message/part objects and cache metadata.
2. Add full-hook regression cases with an existing history hint and with a newly added hint. Capture the original array and its contents before the hook. Assert it remains unchanged, the outgoing request contains exactly one final tail, and reusing the original history for another request does not replay the previous tail.
3. Update the existing-hint unit test to assert hint/content preservation without requiring a particular returned-array identity. The hook-level test establishes the ownership guarantee.
4. Spy on `process.stderr.write` for the unknown-option test. Assert exactly one matching warning for that setup, including the invalid value and system fallback, across multiple context calls. Restore the spy in `finally`; do not introduce a process-global warning registry.
5. Rename the coordinator test to describe its actual proposal/dispatch coverage. Wait, result, and wake coverage belongs in Commit C.
6. Add a short warning beside the root README option and in the plugin placement section: experimental; the one tail trial repeatedly waited instead of ending turns; use system for normal research while remediation is unverified.
7. Replace machine-specific checkout/home paths in the public implementation plan with repository-relative descriptions and the designated branch. Preserve the original diagnosis and historical experiment provenance; do not rewrite evidence to suggest it was rerun.

Accept when the ownership reproducer fails on the old code and passes on the fix; fallback and warning tests pass; default system behavior is unchanged. This commit is independently reviewable.

## 3. Commit B — narrow host checks before routing changes

Extend the existing isolated-host probe, or add a small adjacent fixture using its private-server/local-provider setup. Do not restore the archived WP probe. Use fake local credentials and no external model.

### Compaction

- Run one primary tool continuation, force one real host compaction, then run another primary continuation. The pinned host's earlier probe used `POST /api/session/{id}/compact` with an `id`; verify the installed API before using it.
- Observe the `context`, `compaction`, and `model.request` hooks and final HTTP bodies, correlated by session and deterministic test sequence. Observe title/generate only if needed for the same routing assertion; do not build a certification matrix.
- Assert the temporary tail occurs on intended primary requests, is absent from the compaction body and persisted summary/history, and appears once on the next eligible primary request.
- Do not infer request kind from the agent display name or a mutable last-kind variable. An `agent === "build"` whitelist would reject legitimate primary agents.
- If auxiliaries use separate hooks, keep production routing unchanged and record that result. If compaction really shares the context path, require a verified request-local discriminator before claiming support; document the blocker instead of inventing a routing heuristic.

The current records do not establish compaction injection: system X-003 had 95 primary calls, one compaction, and 95 context records; tail main had 192 primary calls, one compaction, and 192 context records. The extra system context record was in main (30 context records, 29 primary calls, zero compactions). Aggregate counts must not be used as proof either way.

### Reasoning

- Reuse a local scripted reasoning/tool fixture and the installed SDK adapter. Compare a continuation with and without the temporary tail, including the existing history cache hint.
- Assert prior reasoning text/native signature bytes, tool IDs/results, and ordering are preserved where the selected adapter supports replay. Assert the original history is unchanged. Avoid invalid fake signatures being mistaken for native acceptance.
- Distinguish exact wire preservation from provider acceptance and model behavior. A local fixture cannot establish native reasoning semantics; keep that limitation in the response.

Accept with a short command/result note naming the host, route, and observed hooks. Do not store credentials or real prompt text in public evidence.

## 4. Commit C — bounded wait completion in tail mode

Preferred implementation stays in TypeScript and uses the existing context hook. Do not begin with backend duplicate-wait errors or interruption of the active host call.

### Eligibility and execution identity

Do not infer control solely from a tool name in old history. Reuse `engine.executionToken(sessionID)` and the existing process-scoped `RuntimeState` in `plugin/src/controller-lifecycle.ts`:

- Add one optional, lazily initialized per-session map holding only `{execution, waiting}` for accepted tail-mode waits. This preserves the marker across a warm plugin rebind without a database change or a new state-management class.
- In `runTool`'s existing `case "wait"`, capture the execution token, await the current `engine.wait` call, and record the successful result's waiting IDs only if the token still matches. Failed waits and default system mode do not create a marker. Do not change `Engine.wait` itself.
- At the next primary context request, require main role, tail configuration, the same verified execution token, running project, and matching authoritative wait state. If a newer execution has started, invalidate the old marker; never stamp a new execution using a late result from an older one.
- A report notification delivered without resume during that same execution must not clear the marker: the coordinator still needs to end its turn so the existing engine can wake it. A real later execution has a different token and is unaffected.
- Retain at most one marker per session; invalidate stale execution markers and use existing session cleanup where available. Do not clear a new marker merely because a delayed end event from an older turn arrived.

The pinned host must complete the current tool-call/result group before requesting another model step. Commit B and the local wait test must verify that ordering, including a mixed wait/query group. Preserve all sibling outputs. If the host can request a continuation before sibling results arrive, stop at that concrete contract gap rather than dropping pending results or inventing a parser-based authorization rule.

No execution identity or unverifiable control state uses the original system/safety path. Existing cold-start and authorization restrictions remain authoritative; do not reconstruct permissions from stale tool-result text.

### One finishing request

For an eligible continuation:

1. Fetch fresh memory once and retain the original full text and original history for delivery checks.
2. Use the existing full-context system placement for this request, including evidence warnings. Append no temporary user tail and add no placement-only history hint.
3. Add a short system instruction to acknowledge the wait and end this response; explain that the engine will deliver the result and wake the coordinator.
4. Expose no tools for this finishing request. Merely removing the tail or returning a repeated-wait error still leaves tool polling possible. Preserve every completed tool result; do not abort, fabricate completion, or discard current context.
5. Let the host end the execution normally. Use the existing end/delivery/wake path. On a later real wake, restore normal tail placement and normal allowed tools from fresh state.

The only new control bookkeeping is the optional process-local, execution-keyed marker above. No database fields, durable latches, TTLs, or generic lifecycle state machine are planned. A backend repeated-wait error alone is not an acceptable fix because it can create an error retry loop. Removing tools on the finishing request prevents a valid repeated-wait or query-polling continuation; verify that outcome rather than adding both mechanisms by default.

### Regression cases

- An accepted wait produces one full-system, tool-free finishing request; no tail; original history unchanged; no second successful wait/tool-poll step occurs.
- Mixed completed wait/query group retains all results and finishes safely. Failed waits do not latch. A late old-execution wait result cannot latch a new execution.
- A node result arriving while the coordinator is still finishing remains deliverable. After execution end, the existing wake is submitted once; the next request has fresh result state, normal tools, and normal tail placement.
- Warm rebind preserves an accepted wait marker for the same execution. A later wake/new execution ignores it even though old wait tool results remain in history; an in-flight report notification does not prevent finishing the old execution.
- Checkpoint base delivery and evidence warnings remain valid on the full-system finishing request.
- Pause, stop, unknown control, missing authorization, blind specialists, and non-research sessions retain their existing restrictions. System mode remains unchanged.
- Extend coordinator coverage through dispatch, wait, real execution end, delivered result, and wake. Expect correct state and behavior; do not assert byte-identical system text across legitimate control transitions.

Use a controlled fake model that chooses tools only when exposed; assert the request offers none and the host reaches idle. This establishes the bounded tool-control path, not a claim that real-model behavior has already been measured.

If this cannot be made reliable within the narrow patch, keep tail explicitly experimental and system recommended. Planned production files for this commit are `plugin/index.ts`, the placement module if a pure helper is useful, and the optional field in `plugin/src/controller-lifecycle.ts`. Do not change engine or specialist lifecycle logic silently. Changes to specialist stop/summary/busy wiring must follow the repository's existing lifecycle verification skill and its drift/TLC checks.

## 5. Validation and evidence gates

Run focused tests after each commit, then `PYTHONDONTWRITEBYTECODE=1 make test` on the final branch. Run the existing local tail smoke plus the new wait/wake and compaction cases. Assert existing-hint ownership, complete tool pairing, marker budget, and non-persistence. Retain the lifecycle fingerprint check; do not claim full TLC was run unless it was.

Add one concise `docs/evidence/context-cache/review-response.md` mapping the review findings to dispositions, commits, commands/results, and remaining limitations. Keep historical numbers intact. Lead any updated PR summary with per-role usage and request counts; describe all hit rates and averages as observational. Cost stays unavailable unless actually reported; no custom pricing project is required.

The stable allowlist is unchanged. The waiting variant of `project` is confined to the full-system finishing request; confirmed wake clears waiting, so subsequent tail requests are expected to regain the pre-wait stable prefix while its cache is live. This is expected mitigation, pending stable-digest comparison across real wakes. Other ledger changes, the shared content budget and cache expiry can still change reuse. A renderer split is deferred.

### Real-route gate — outside the initial implementation handoff

No paid request is authorized by this planning document. After the user authorizes a concrete test goal/model/request budget:

**Route qualification comes first.** Before real research with tail, send both actual finishing shapes (accepted `research_wait` and `research_finish`): full system context, complete tool-call/result history, zero tool definitions, no tail and no new placement hint, with the chosen reasoning settings intact. Require provider acceptance and an actual terminal reply rather than further tools. Check the user's DeepSeek gateway and any Anthropic-family route intended for use independently. A 400, retry/pause, rejected reasoning replay, missing usage or inconclusive response leaves that route unqualified. Currently no real route is qualified, so normal research uses system. This is a documented experimental-use restriction; there is no runtime provider allowlist or automatic certification.

The pinned context hook has no typed tool-choice control; the local `--check-tool-choice-options` probe sends neither tested option alias on the wire. Do not assume an arbitrary `request.options.toolChoice` works, rewrite production HTTP bodies, or retain callable definitions based only on a prompt instruction. The tool-free path remains experimental until its exact native request is accepted.


1. Capture one representative wait-continuation payload without publishing its content. Compare otherwise-identical requests with and without the temporary tail, alternating order, up to 10 requests per arm with the same model/reasoning/output settings.
2. Send directly to the selected provider and classify the responses; never execute returned tool calls. Count tools versus final replies, errors, truncation, and reported usage. Missing/truncated usage or inconclusive behavior stays explicit. This is a small experiment, not a native-runner framework or exact-cost guarantee.
3. Verify reasoning acceptance on the actual chosen route. Preserve thinking settings and signatures; do not disable reasoning to make the test pass.
4. Only after the finishing path is sound, repeat the matched research pair, including wait calls, turn ends, notifications/wakes, request counts, unknown usage, report evidence and whole-run known usage. It must not continue an unbounded wait or post-finish loop. Include node message sequences, both finishing shapes and the coordinator stable-block digest immediately before wait and after confirmed wake. Investigate mismatches rather than asserting that every wake must be identical despite other ledger/budget changes.

Query-only polling before any terminal action remains possible in either placement. Consider an in-execution step cap for both modes as a separate control-design task before changing the default; this correction does not introduce one.

Keep system default throughout. A default change is a separate decision after suitable repeats and both exploration settings; no universal savings threshold or multi-provider certification is added here.

## 6. Scope and handoff

Target production changes: an immutable append, a small wait-finalization branch, and one optional execution-keyed marker in the existing runtime state. Keep implementation in the placement integration/module and the runtime-state type; no new production module is required. Tests and a bounded extension of the current host probe carry most of the work. If production growth exceeds roughly 150 added lines or requires a new subsystem, explain the concrete blocker before expanding scope. This is a review trigger, not a reason to omit necessary checks.

```text
Implement Commits A, B, and C from docs/Prompt cache PR 35 review remediation plan.md
on cursor/prompt-cache-stabilization, beginning at a463f91 or its reviewed successors.
Inspect status and AGENTS.md; preserve unrelated changes; sync main by merge only.

Keep system default. Fix message-array ownership and the warning test first.
Verify auxiliary routing and reasoning lowering with the existing isolated host.
Implement the bounded tail-only wait completion and its real end/result/wake tests.
Keep full fresh context and canonical delivery checks on the finishing request.
Do not redesign renderers, budgets, receipts, specialist lifecycle, or tool schemas
for caching. Do not add tokenizers, provider registries, or a new probe framework.

Run focused tests, final make test, and local host checks. Record a concise review
response with actual results and unresolved limitations. Stop at a concrete host
contract gap instead of inventing a guard. No paid API experiment, shared-service
restart, PR merge, release, or default change is included in this handoff.
```

## Follow-up to the dcf09c9 re-review

[Re-review](https://github.com/RinshannKaihou/auto-research-opencode/pull/35#issuecomment-6631854671): accepted node finishes now reuse the same marker/lifetime and full-system finishing path. The marker is installed only after `Engine.finish` succeeds for the captured execution. Context checks existing node authorization and the service's `node_finish_pending.turn_token`; the engine still commits only at real execution end. Failed calls, stale or mismatched pending finishes, system mode and warm rollback cannot activate this path. The existing warm-state map key is retained; missing `kind` means the earlier wait marker. No renderer, service, schema or finish-commit logic changes.
