# Terminal handoff: host queue contract and isolated smoke

2026-11-04 (updated 2026-11-05) · branch `cursor/prompt-cache-stabilization` · baseline `555b449`

This note records the host contract the [terminal handoff repair plan](../../../Prompt%20cache%20terminal%20handoff%20repair%20plan.md)
depends on, the isolated smokes that were actually run, and the one contract gap
that changes how the fix behaves. It is engineering evidence for OpenCode 2.2.5
with a scripted loopback provider; it says nothing about a real model.

## Host queue contract

`python3 scripts/probe_prompt_handoff.py` · OpenCode 2.2.5 (`opencode serve`),
private server on `127.0.0.1`, empty XDG tree, loopback OpenAI-compatible
provider, fake key, no external model or research data.

The probe sends a real user prompt (`POST /api/session/{id}/prompt`) while a
first prompt is blocked inside the provider, rewrites `delivery` from `steer` to
`queue` in the prompt hook, and records the durable event order.

| Question | Observed |
|---|---|
| Where does the prompt hook run? | Once, at enqueue, before `session.inbox.enqueued`. `messageID` is the native inbound id and survives to delivery. |
| Does the host honour a rewritten `delivery`? | Yes. `/api/session/{id}/inbox` records `queue`, and the provider sees no second request while the first execution is blocked. |
| Message ID, text, attachments, order | Preserved. The queued request carried the original text, the inline attachment content and the earlier turn; the first request carried neither. |
| Where is a queued prompt delivered? | **At the next step of the same execution**, not at a new execution boundary. `session.inbox.delivered` precedes the next `session.step.started`, and no new `session.execution.started` occurs between enqueue and delivery. |
| A queued prompt sent to an idle session | Starts its own execution immediately. |
| `synthetic(resume:false)` notifications and wake notices | Never reach the prompt hook. |

Recorded values: `queued_message_prompt_hook_count: 1`,
`inbox_delivery_while_busy: "queue"`, `first_request_had_queued_text: false`,
`queued_request_had_queued_text: true`, `queued_request_had_attachment_text:
true`, `synthetic_reached_prompt_hook: false`, `busy_queue_delivery_scope:
same_execution`, `idle_queue_delivery_scope: new_execution`.

The last row is the gap. The repair plan assumed `queue` defers a message to the
next execution. On 2.2.5 it defers to the next step of the running execution.
The fix therefore does not add a message queue, cancel the execution, or resend
a synthetic message. It keeps the accepted terminal action's tool-free barrier
for the remainder of that execution, and files the deferred guidance note only
when a later execution prepares its context. That is a partial mitigation, not
the agreed handoff: see the acceptance in the smoke sections below.

`tests/test_prompt_handoff_probe.py` covers the delivery-scope classifier with
enqueue-before-start, same-execution and new-execution event orders.

## Model step -> tool call -> input association

`python3 scripts/probe_step_association.py [--delivery steer|queue]` · same
private-XDG / loopback-provider fixture. A probe tool blocks the model request and
then the tool itself, and real prompts are sent in both windows.

| Question | Observed on 2.2.5 |
|---|---|
| Do context-hook messages expose native IDs? | Yes for user and assistant messages; a user message ID equals the prompt-hook `messageID`; tool-result messages have no id. |
| Does the tool know its generating step? | Yes. The tool execution `call.messageID` equals the `session.step.started` `assistantMessageID` of the step that produced the call. |
| Does the tool see that step in the transcript? | Yes. `ctx.session.context()` called from the tool already contains the assistant message with `call.messageID` and the user messages delivered before it. |
| Are primary requests serial per session? | Not used. The hook-level counter only shows that context callbacks do not overlap; it says nothing about request lifetimes, so the association does not rely on it. |
| Where does an input sent during model generation first appear? | The next provider body (the step after the tool result), never the in-flight request. It is not in the transcript at tool time either. |
| Where does an input sent while the tool runs first appear? | The next provider body as well. |
| Are real prompts and synthetic notices distinguishable in the transcript? | Yes. A real prompt is a `user` entry; a `resume:false` synthetic is never persisted as `user`. |
| Wire bodies carry message IDs? | No. Association uses the transcript's native message IDs, never the provider body. |

The plugin therefore stores only the generating step's assistant ID on the
terminal marker. When a finishing request is prepared, it reads the native
transcript and asks what real (`type: "user"`) input arrived **after that
assistant message**. That is a true generation-step association: a newer context
preparation, a session-level snapshot or a late RPC cannot change it, and it does
not assume request seriality. If the step is missing from the transcript (legacy
`9424fb0` warm marker or compaction), the boundary is unknown and the finishing
step takes the conservative text-response path instead of asserting there is no
new input.

The fake-host fixture models the same relationship explicitly: every tool call
appends an assistant transcript entry whose ID is exactly the `messageID` handed
to the tool, so transcript association is exercised rather than inferred from an
auto-incrementing counter. The real-host probe separately proves the
`step.started.assistantMessageID` = `call.messageID` equality.

### steer vs queue control

Same script barrier and send time, only the delivery differs:

| Delivery | Inputs first visible | Execution boundaries | Tool still executed | IDs preserved |
|---|---|---|---|---|
| `steer` | both pending inputs in the next provider body | 1 | yes | yes |
| `queue` | one input per step (bodies 2 and 3) | 1 | yes | yes |

`queue` is therefore observably different (one input per step) and is kept; the
control does not license dropping the earlier routing or claiming a new
execution boundary.

## Isolated research smoke: the acceptance is a normal round

`python3 scripts/probe_research_run.py --context-placement tail --wait-finish
--queued-input` · the same private-server/local-provider fixture. The provider
blocks the coordinator's accepted-wait finishing request; the probe sends a real
user prompt through `/api/session/{id}/prompt` and releases the request. A small
instrumentation plugin records every `context` hook (session, tool count,
queued text, finishing/post-terminal instruction) and the execution events.

The acceptance is **not** "the text is in history". It is: a later request in
the same session still carries the queued text, exposes tools, and is neither
the finishing request nor the post-terminal step.

| Request | Content |
|---:|---|
| 5 | Accepted wait finishing request: full system context, no tools, no tail, **no queued text** |
| 6 | The host delivered the queued prompt at the next step of the same execution: no tools, and the old finishing instruction is replaced by the post-terminal instruction because the newest real input differs from the marker's generating-step boundary |
| 7 | The wait result woke the coordinator in a new execution: normal tail placement and tools, the user text in history |

The probe records the blocked request's own index when it arrives and waits
for the coordinator's normal round within one bounded deadline after release. A
missing wake is reported as `timeout`, never as a structural host block. With
`--interleave-session` another session's request is inserted while the target
request is blocked; the target request index, the first post-terminal request and
the normal round still line up.

Recorded: `delivery: "queue"`, `finishing_had_text: false`,
`post_terminal_step: true`, `normal_round: true`, `gate: "passed"`,
`finishing_body_kind: "wait"`, `interleaved_requests: 1`, `guidance_notes: 1`. The post-terminal request is a recognised overlay kind in
`context_placement_checks`, so it participates in request accounting, full tool
pairing and the "no tools, no tail" gate instead of being skipped. The placement
checks stayed valid: 8–9 research requests depending on whether the wake
execution continues once more, 5–6 tailed, 3 overlay requests,
`tool_pairing_ok: true`, `persisted_history_has_tail: false`,
`max_cache_markers: 4`.

## Node finish: blocked on the host contract

`python3 scripts/probe_research_run.py --context-placement tail
--queued-input-node --expect-blocked`

The blocked finishing request is the node's accepted `research_finish`; the
prompt goes to the node session.

| Request | Content |
|---:|---|
| 4 | Node finishing request: full system context, no tools, no tail, **no queued text** |
| 5 | The queued prompt was delivered at a later step of the same node execution: no tools, post-terminal instruction selected by the same input boundary |
| — | No later node execution follows, so the queued input never reaches a normal round and no guidance note is filed |

With the same interleaved request, the blocked index still points at the node
finishing request (`finishing_body_kind: "finish"`).

Recorded: `delivery: "queue"`, `finishing_had_text: false`,
`post_terminal_step: true`, `normal_round: false`, `gate: "blocked"`,
`reason: "no_later_execution"`, `finishing_body_kind: "finish"`,
`interleaved_requests: 1`, `guidance_notes: 0`, node task `finished`,
8–9 research requests, 6–7 tailed, 2 overlay requests (one finishing, one
post-terminal), `tool_pairing_ok: true`, `persisted_history_has_tail: false`,
`max_cache_markers: 4`.

This is the blocking record for repair B. Queueing prevents the message from
entering the finishing request, but the pinned host only defers it to the next
step of the same execution; a finished node has no next execution. The plan does
not allow a plugin-owned queue, cancellation or a resent user message, so the
handoff is not complete on this host version.

## Limitations

- The provider is scripted, so this says nothing about real model behavior,
  provider acceptance of the tool-free request, cache reads, cost or answer
  quality.
- The host contract is the pinned 2.2.5 behaviour. A future host may move
  `queue` to an execution boundary; the plugin handles both cases, but the
  recorded scope is this version.
- The smoke covers one coordinator wait and one node finish, each with one
  queued prompt. A queued prompt arriving at a node whose execution has already
  ended is covered by the fake-host regression that keeps the closed work
  segment authoritative, not by a third isolated run.
