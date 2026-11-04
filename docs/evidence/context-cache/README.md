# Context placement smoke record

2026-11-04 · branch `cursor/prompt-cache-stabilization` · revision `a990fc5` (initial placement implementation `ce393d5`), based on `73d507a` / v0.2.1.

This note records the engineering checks for the opt-in `contextPlacement: "tail"` change. It is not a real-model cache or cost measurement; the Step 3 real-API pair is recorded separately in [step3/README.md](step3/README.md). The default remains `"system"`. Compaction routing and reasoning wire preservation are recorded in [boundary/README.md](boundary/README.md).

## What was checked

- Unit tests for the split allowlist, the temporary `<research_state>` message, and the history cache hint.
- A lowering regression test against the installed Anthropic adapter: hints survive on nonblank text, system text and tool results, and are dropped on reasoning, tool-call and blank text parts. The helper therefore only selects nonblank text and tool-result parts, walking back to an earlier supported part or falling back to system placement. A trailing blank text part is covered explicitly and selects the earlier text. A hint on a dropped part still counts against the SDK's four-marker budget, so the limit check counts every schema-legal hint while selection stays narrowed.
- Python tests for the probe checker: a missing tail on any research request, or a tail that splits a tool pair, fails the check; an auxiliary request without a tail is ignored.
- Fake-host tests for a stable system prefix across node and coordinator ledger writes, checkpoint base delivery, evidence-warning placement, blind isolation, stale/blockless fallback, pause control text, unassociated sessions, and invalid option values.
- Fake-host tests for tail wait completion: one tool-free full-system finishing request, repeated preparation before execution end, terminal clearing, replayed older terminal events, failed waits, mixed wait/query groups, stale executions, warm rebinds, warm tail→system rollback, a report notification before finishing, wake-once, and the system-mode control.
- One isolated-host smoke run of `scripts/probe_research_run.py` with `--context-placement tail`, plus the `"system"` control, and the compaction/reasoning boundary fixture in [boundary/README.md](boundary/README.md).

## Commands and results

`PYTHONDONTWRITEBYTECODE=1 make test` (run in the agent VM):

- Python: 871 passed.
- Bun: 307 passed, 0 failed.
- TypeScript: `tsc -p tsconfig.json --noEmit` clean.

`python3 scripts/probe_research_run.py --context-placement tail --wait-finish --request-dump <temp file>`:

- The isolated lifecycle completed with actions `propose`, `dispatch`, `wait`, `node_finish`; one node finished.
- The run made 6 provider requests. Five research requests carried a trailing `<research_state>` message; the sixth was the accepted wait's tool-free finishing request with the full context in system and no tail.
- `all_research_requests_accounted_for`: true; `tail_requests`: 5; `finishing_requests`: 1; `finishing_requests_have_no_tools_and_no_tail`: true. The scripted model called `wait` once and the host ended the turn without exposing tools again.
- `tail_absent_from_earlier_messages`: true. `tool_continuations`: 3, `regular_tool_continuations`: 3, and `tail_follows_every_regular_tool_continuation`: true for each one. `tool_pairing_ok`: true; no tool call is split from its result by the tail.
- `boundary_before_tail`: true. `tail_has_no_cache_marker`: true. `max_cache_markers`: 4.
- `persisted_history_has_tail`: false; the session context endpoint returned no injected state.

A baseline run without `--wait-finish` made 8 provider requests and all 8 carried the tail (`finishing_requests: 0`), because that lifecycle never called `research_wait`.

`python3 scripts/probe_research_run.py --context-placement system`:

- The same lifecycle completed with 6 provider requests; `max_cache_markers`: 4.

## Host and route

OpenCode 2.2.5 (`opencode serve`), private server on `127.0.0.1` with empty XDG directories, a scripted local OpenAI-compatible provider (`ari-probe/probe`), and no external model or shared service. The probe declares `compatibility.cacheFormat: "anthropic"` on that fake provider only so the SDK lowers inline cache markers into the request body where the test can inspect their positions. That declaration is not a claim about the provider the user actually runs.

## Outcome

In this scripted lifecycle every research request either carried the temporary message or was the accounted-for tool-free finishing request. Each regular tool continuation kept its complete tool result before the tail, no tool pair was split, the message was not written into persisted history, and it did not carry a cache marker. A reusable marker was present on the message before it, and the request stayed at the SDK's four-marker limit. The separate boundary fixture covers a real host compaction and a mixed wait/query group.

## Limitations

- One synthetic lifecycle and one node; no specialist in this run. The provider is scripted, so its usage fields are fixed and this record says nothing about cache reads, cache writes, cost, or report quality.
- The `--wait-finish` lifecycle calls `research_wait` once and exercises the tool-free finishing request. It is a scripted model, not a real model-behavior check.
- No title request occurred. The boundary fixture covers one manual compaction; other auxiliary request kinds were not certified.
- The shared 12,000-character budget was not measured. Budget coupling can still change or drop a stable block when the volatile part grows; the smoke run does not quantify that.
- The `"anthropic"` cache format was declared to observe marker lowering on the wire, not to certify any real provider's behavior.
- This engineering note covers no real-model trial, no shared-service restart and no default change. Step 3 is recorded in [step3/README.md](step3/README.md): one real-API pair showed 71.2% lower uncached input per known request (64.9% lower known total including compaction; primary-only 73.9% / 68.2%). The tail coordinator also repeated `research_wait` 104 times, one final tail request has unknown usage, and the arms did different amounts of work, so the default-change gate is not met.
