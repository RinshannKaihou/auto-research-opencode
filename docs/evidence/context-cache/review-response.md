# PR #35 review response

2026-11-04 · branch `cursor/prompt-cache-stabilization` · default remains `contextPlacement: "system"`.

This note maps the original reviews and the [dcf09c9 re-review](https://github.com/RinshannKaihou/auto-research-opencode/pull/35#issuecomment-6631854671) to dispositions, commits and checks. It does not change the historical Step 3 numbers.

## Finding dispositions

| Finding | Disposition | Commit / evidence |
|---|---|---|
| Existing-hint path mutates the host array (medium, merge blocker) | **Fixed.** The tail is appended into a new array; the full-hook regression covers both an existing and a newly added history hint, the original array stays byte-identical, and reusing it does not replay the tail. The regression fails on the old `push` and passes on the fix. | `08e20e9`; `plugin/test/plugin.test.ts` |
| Unknown-option warning not asserted (low) | **Fixed.** The stderr spy asserts exactly one warning containing the invalid value and the system fallback across three context calls, with the spy restored in `finally`. | `08e20e9` |
| Test name claimed a delivered result; public plan had local paths (low) | **Fixed.** The coordinator test is named for its proposal/dispatch coverage, and the development plan no longer publishes absolute checkout paths. | `08e20e9` |
| Compaction may receive the tail (medium, unverified) | **Verified, no routing change.** The isolated host runs a real manual compaction. Compaction uses the separate `compaction` hook and the `context` hook does not run for it; the compaction body, completed summary and persisted history have no tail. The next primary request has exactly one. No agent-name or last-kind heuristic was added. | `7c0c9cc`; [boundary/README.md](boundary/README.md) |
| Reasoning replay not established (low) | **Wire preservation verified; native acceptance remains unverified.** The installed OpenAI-compatible adapter preserves reasoning text, encrypted signature details, tool IDs, arguments, results and the existing cache hint; the prefix before the tail is byte-identical with and without it. This is not evidence that a real provider accepts or replays the reasoning. | `7c0c9cc`; `plugin/test/context-placement.test.ts` |
| Coordinator repeats waits rather than ending turns (high, blocks enabling tail) | **Bounded remediation implemented in tail mode.** An accepted wait leaves a process-local, execution-keyed marker. The next primary request of the same execution gets one full-system request with no temporary tail, no placement hint and no tools, which acknowledges the wait and ends the response. The marker stays for the active execution, so a repeated preparation remains finishing; a verified terminal transition, a newer execution or session cleanup clears it. Failed waits and system mode do not latch; a report notification delivered without resume does not clear it; a warm rebind keeps it for the same execution, while a warm tail→system rebind discards it. A replayed older terminal event is deduplicated and cannot clear a newer execution's marker. Real-model behavior is still unverified. | `4a139e0`, `a315f46`, `ce37705`; `plugin/test/plugin.test.ts` |
| `project` changes at wait/wake (relabelled by re-review) | **Expected mitigated; verify stable digest across real wakes.** The waiting variant is on the full-system finishing request. Confirmed wake clears waiting, so regular tail requests may regain the pre-wait stable prefix while the cache is live. Other ledger/budget changes and expiry can still affect reuse. No renderer change is required by this finding alone. | This note; native gate in the [remediation plan](<../../Prompt cache PR 35 review remediation plan.md>) |
| Savings headline compares unequal work (low, blocks default change) | **Presentation corrected.** Lead with per-role request counts and observational hit rates, not the whole-run total. The arms did different work, so 64.9% is descriptive, not a like-for-like cost claim. | [step3/README.md](step3/README.md) |

## Re-review dispositions

| Finding | Disposition | Evidence |
|---|---|---|
| Tool-free finishing request may be rejected with tool history | **Open native acceptance gate; no qualified real routes.** The pinned hook does not expose a typed tool-choice control, and setting both candidate option aliases emitted no `tool_choice: none` in 3/3 local primary requests. Keep the existing tool-free path experimental. Before real tail research, independently validate actual wait and node-finish payloads with full paired history, zero definitions and the selected reasoning settings. Do not claim DeepSeek or Anthropic-family acceptance from the fake provider or the earlier trial. Normal research uses system; the restriction is documented, not a runtime allowlist. | [Boundary record and reproducible command](boundary/README.md#re-review-finishing-request-provider-gate) |
| Accepted `research_finish` lacks the finishing path | **Implemented.** A successful finish latches the current execution; the next primary context requires existing node authorization and an authoritative pending finish with the same token. It keeps fresh full system context, original sibling results, no tail, no new placement hint and no tools. Repeat preparations remain finishing. Commit/delivery still wait for the real terminal event. Failed calls, mismatched pending tokens, stale execution markers and warm system rollback do not latch. The original warm-state key is retained for older wait markers. | Six new full-hook regressions; local lifecycle observes one node and one wait finishing request |
| `project` wait/wake churn previously called an enablement blocker | **Relabelled as expected mitigated, pending measurement**, as above. The next native pair must capture coordinator stable digests before waiting and after confirmed wake. | Development/remediation plans and user docs updated; no renderer split |
| Query polling without `research_wait`; broader step cap | **Deferred control-design decision before a default change.** Neither mode gains a general in-execution step cap here. The terminal-action correction does not bound work before a terminal action is accepted. | Later default-change gate; default stays system |

## Per-role Step 3 view (primary completed requests)

| Role | Requests, system / tail | Hit rate, system / tail | Uncached input per request, system / tail |
|---|---:|---:|---:|
| Coordinator | 29 / 191 | 53.4% / 95.7% | 18,931 / 2,531 |
| Nodes | 170 / 65 | 82.8% / 92.2% | 9,749 / 3,672 |
| Specialists | 27 / 19 | 74.2% / 86.9% | 4,809 / 1,082 |

These are observed values from one pair. Shorter tail sessions also lower uncached input per request, and the tail coordinator's request count includes the repeated waits. The tail arm used 26.5% more primary prompt tokens, mostly cache reads, and made 49 more primary requests. Cached tokens are not free; no price-weighted comparison is claimed because the gateway did not report cost.

## Verification (local macOS arm64 agent environment, not GitHub CI)

- `PYTHONDONTWRITEBYTECODE=1 make test`: **874 Python passed**, **313 Bun passed**, 0 failed; `tsc -p tsconfig.json --noEmit` clean.
- `python3 scripts/probe_research_run.py --context-placement tail --wait-finish`: completed; 4 tailed requests plus two tool-free finishing requests (one wait, one node finish), 2 regular tool continuations paired, one `research_wait` call, no persisted tail, 4 markers at most. The checker validates the complete finishing message list for tool pairing; a missing-result fixture fails the gate.
- Prior `dcf09c9` baseline without `--wait-finish`: all 8 requests tailed. On this revision an accepted node finish is intentionally full-system and tool-free; the historical all-tails count is not a current invariant.
- Same probe with `--context-placement system`: completed; 7 requests, 4 markers at most.
- `python3 scripts/probe_placement_boundary.py --check-tool-choice-options`: OpenCode 2.2.5 isolated host, local `ari-probe/probe` route; context hook only on primary requests, one compaction hook request without tail, completed summary and persisted history clean, mixed `research_query` + `research_wait` group preserved both results before the next request.
- `bun test plugin/test/context-placement.test.ts`: reasoning/tool wire fixture passes against the installed OpenAI-compatible adapter.
- Specialist lifecycle covered-function fingerprint check (`drift_problems(sync_commit())`): no drift. Full TLC was not rerun.
- **16 tail terminal-action tests passed; 12 Python evidence/probe tests passed.** Node tests cover accepted/mixed sibling results, repeated preparation, no commit before terminal event, rejected finish/transcript-only input, authoritative-token mismatch, warm rebind, system rollback and system mode. Existing wait tests cover accepted, repeated preparation before execution end, terminal clearing, replayed older terminal events, failed, mixed-group, stale-execution, warm-rebind, warm tail→system rollback, notification-before-finishing, wake-once, evidence-warning and system-mode cases.

## Remaining limitations

- No real-provider API request was made under this remediation. Native acceptance and actual turn completion for both tool-free finishing shapes, the wait-continuation resend comparison, reasoning acceptance, and a repeated matched research pair with node sequences and stable digests across wakes remain behind the later gate. No real route is currently qualified for tail research.
- The local reasoning fixture proves wire preservation only; provider acceptance and model behavior are unknown.
- The isolated compaction fixture uses a local scripted provider and one manual compaction; it is not a certification of every auxiliary request kind.
- Wait/wake stable-prefix recovery is expected from the finishing/wake design, not yet verified on a real route. The renderer split is deferred; content-budget coupling and cache expiry remain.
- Default enablement is unchanged and the default-change gate (both exploration settings, repeats, no regressions) is not met.
- Full TLC model checking was not rerun; only the focused lifecycle comments and the existing specialist fingerprint check are relied on.
