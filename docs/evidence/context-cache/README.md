# Context placement smoke record

2026-11-04 · branch `cursor/prompt-cache-stabilization` · revision `ad53cbb` (initial placement implementation `ce393d5`), based on `73d507a` / v0.2.1.

This note records the engineering checks for the opt-in `contextPlacement: "tail"` change. It is not a real-model cache or cost measurement. The default remains `"system"`.

## What was checked

- Unit tests for the split allowlist, the temporary `<research_state>` message, and the history cache hint.
- A lowering regression test against the installed Anthropic adapter: hints survive on text, system text and tool results, and are dropped on reasoning and tool-call parts. The helper therefore only selects text and tool-result parts, walking back to an earlier supported part or falling back to system placement. A hint on a dropped part still counts against the SDK's four-marker budget, so the limit check counts every schema-legal hint while selection stays narrowed.
- Python tests for the probe checker: a missing tail on any research request, or a tail that splits a tool pair, fails the check; an auxiliary request without a tail is ignored.
- Fake-host tests for a stable system prefix across node and coordinator ledger writes, checkpoint base delivery, evidence-warning placement, blind isolation, stale/blockless fallback, pause control text, unassociated sessions, and invalid option values.
- One isolated-host smoke run of `scripts/probe_research_run.py` with `--context-placement tail`, plus the `"system"` control.

## Commands and results

`make test` (run in the agent VM):

- Python: 851 passed.
- Bun: 254 passed, 0 failed.
- TypeScript: `tsc -p tsconfig.json --noEmit` clean.

`python3 scripts/probe_research_run.py --context-placement tail --request-dump <temp file>`:

- The isolated lifecycle completed with actions `propose`, `dispatch`, `node_finish`; one node finished.
- The final rerun made 6 provider requests (earlier runs 6–7); every research request carried a trailing `<research_state>` message. `all_research_requests_have_tail` and `tail_is_last` are both required now, so a request without a tail fails the run.
- `tail_absent_from_earlier_messages`: true. `tool_continuations`: 3, and `tail_follows_every_tool_continuation`: true for each one. `tool_pairing_ok`: true; no tool call is split from its result by the tail.
- `boundary_before_tail`: true. `tail_has_no_cache_marker`: true. `max_cache_markers`: 4.
- `persisted_history_has_tail`: false; the session context endpoint returned no injected state.

`python3 scripts/probe_research_run.py --context-placement system`:

- The same lifecycle completed with 7 provider requests; `max_cache_markers`: 4.

## Host and route

OpenCode 2.2.5 (`opencode serve`), private server on `127.0.0.1` with empty XDG directories, a scripted local OpenAI-compatible provider (`ari-probe/probe`), and no external model or shared service. The probe declares `compatibility.cacheFormat: "anthropic"` on that fake provider only so the SDK lowers inline cache markers into the request body where the test can inspect their positions. That declaration is not a claim about the provider the user actually runs.

## Outcome

In this lifecycle every research request carried the temporary message, each tool continuation kept its complete tool result before the tail, no tool pair was split, the message was not written into persisted history, and it did not carry a cache marker. A reusable marker was present on the message before it, and the request stayed at the SDK's four-marker limit.

## Limitations

- One synthetic lifecycle and one node; no specialist in this run. The provider is scripted, so its usage fields are fixed and this record says nothing about cache reads, cache writes, cost, or report quality.
- No title or compaction request occurred: all tail requests (6–7 per run) and all 7 system requests were primary research requests. The earlier OpenCode 2.2.5 observation that auxiliary requests use their own hooks was not re-checked for those kinds here.
- The shared 12,000-character budget was not measured. Budget coupling can still change or drop a stable block when the volatile part grows; the smoke run does not quantify that.
- The `"anthropic"` cache format was declared to observe marker lowering on the wire, not to certify any real provider's behavior.
- No real-model trial, no shared-service restart, no default change.
