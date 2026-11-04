# Placement boundaries: compaction and reasoning

2026-11-04 · branch `cursor/prompt-cache-stabilization` · OpenCode 2.2.5 (`opencode serve`).

This note records the Commit B host checks for the opt-in `contextPlacement: "tail"` option. It uses the isolated private-server/local-provider pattern from `scripts/probe_research_run.py`: empty XDG directories, loopback fake credentials, a scripted OpenAI-compatible provider (`ari-probe/probe`), and no external model or shared service.

## Compaction routing

`python3 scripts/probe_placement_boundary.py` ran one primary tool continuation with a mixed `research_query` + `research_wait` group, one real host manual compaction through `POST /api/session/{id}/compact`, then one further primary continuation. The fixture also registered a separate instrumentation plugin that logged the `context`, `compaction` and `model.request` hooks.

The mixed group's next model request contained both tool results (`call_probe_query` and `call_probe_wait`) before the trailing tail, so the installed host completed the whole tool-call/result group before asking the model for another step. The wait in that fixture was deliberately rejected (the project had no dispatched task), so this checks grouping and sibling preservation, not wait acceptance.

Observed hooks and bodies, in order:

| Hook | Request kind | Temporary tail |
|---|---|---|
| `context` | primary | present |
| `context` | primary (tool continuation) | present |
| `compaction` | compaction | **absent** |
| `context` | primary (after compaction) | present |

- `model.request` reported `primary, primary, compaction, primary`; the compaction request carried 38 offered tools.
- The compaction body ended with the host's summarization instruction, not with `<research_state>`.
- The persisted history and the completed compaction summary contained no injected tail.
- The request after compaction carried exactly one final tail.

The installed host routes compaction through a separate `compaction` hook, not the `context` hook, so the production research plugin does not inject the tail into compaction requests. No request-kind routing heuristic, agent whitelist or production change was added.

## Reasoning replay

`bun test plugin/test/context-placement.test.ts` includes a wire-level fixture for the installed OpenAI-compatible adapter (with the same `cacheFormat: "anthropic"` compatibility used by the probe provider). It compares identical history with and without the temporary tail:

- the assistant `reasoning_content` text and encrypted `reasoning_details` signature bytes are preserved;
- the tool call ID, function name and arguments are preserved;
- the tool result ID and value are preserved, and the existing cache hint remains on the tool-result part;
- the messages before the tail are byte-identical to the no-tail control;
- the tail is last, has no cache marker, and the original host history is unchanged.

This establishes exact wire preservation through the selected adapter, not that a real provider accepts or replays the reasoning, and not any model-behavior or cache effect. The real-route reasoning check remains part of the later paid-request gate.

## Result

Compaction does not receive the tail, and reasoning/tool bytes survive the temporary tail at the wire level. Production routing is unchanged. The limitations above and the unverified real-provider acceptance are recorded in the review response.
