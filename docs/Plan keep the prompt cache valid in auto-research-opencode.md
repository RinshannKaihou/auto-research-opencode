# Plan: keep the prompt cache valid in auto-research-opencode

Nov 3, 2026 · @Yipei

## Summary

Move the research state that changes with every ledger write out of the system prompt, so each model request reuses the cached conversation instead of paying for it again.

Today the plugin rebuilds up to 12,000 characters of research context for every model request and appends it to the system prompt. That text changes after almost every `research_*` write, and providers cache by prefix, so the whole conversation after it is re-billed as uncached input.

The fix keeps the blocks that change only at role or run-state transitions in the system prompt. Everything else goes after the conversation: either re-sent at the end of every request (option A) or frozen for the length of a turn (option B). A spike in Phase 0 picks the option.

1. **Phase 0 — measure and spike.** Record a baseline on real runs and check three host and provider behaviours that decide between A and B.
2. **Phase 1 — split the context.** TypeScript-only change behind a new plugin option, default unchanged.
3. **Phase 2 — decouple the renderers.** Python change so the stable part never shrinks or reorders because the volatile part grew.
4. **Phase 3 — fixed tool definitions (optional).** Stop rewriting tool schemas per state; reject disallowed calls at execution time.

In an illustrative model (40 steps, state changing on 60% of them) the cache hit rate rises from about 41% to 85%. Treat that as a hypothesis until the Phase 0 and Phase 1 measurements exist. Output tokens are not affected.

Decisions that need an owner:

- Which option ships if the spike allows both (A is recommended).
- When the new placement becomes the default, and on what evidence.
- Whether Phase 3 is wanted: the model would see tools it cannot use in some states.

## Background

The research context is the only large text in the system prompt that changes during a turn, and it sits in front of the whole conversation. Line numbers below are for v0.2.1 (commit `fd1c62d`).

**How it is injected.** The `context` session hook (`injectContext`, `plugin/index.ts:603`) runs before every model request. It calls `research.memory()` (`plugin/src/research.ts:507`), which asks the backend for `memory_context` with `max_chars: 12000` and pushes the result onto `request.system` (`index.ts:703`). The backend builds it in `memory_store.py` (`_context_view`, line 1462) and, under the R1 policy, `context_policy.py` (`render_context`, line 553).

**Why it changes so often.** The text is compact JSON with sorted keys, so it only changes when the ledger does. But most blocks are read straight from ledger state that the model itself writes, step by step:

| Block | Policy | Changes when |
| --- | --- | --- |
| `recent_node_notes` | both | the node calls `research_note` |
| `checkpoint`, `checkpoint_recovery` | both | any `research_checkpoint` |
| `findings`, `critical_now` | R1 | record, revise, publish, relate, dispose |
| `knowledge`, `affected_knowledge`, `retracted_refs`, `structure_hints` | legacy | record, revise, publish, relate, dispose |
| `review_backlog` / `pending_review`, `review_challenges` | R1 / legacy | publish, finish, revise, review relations |
| `questions` | both | propose, dispatch, close a node |
| `exploration` | R1, exploration on | scope, route, decide, work allocation |
| `omitted_index`, `clerical_index` | legacy / R1 | budget pressure, new hints |
| `project`, `attempt`, `conclusion` | both | run-state or pause transitions, conclusion |

**Other things that break the prefix.**

- Tool schemas are rewritten per state: `research_decide` and `research_conclude` (`index.ts:654–675`), and tools are removed when paused or when a node turn lacks authorization (`index.ts:676–695`). Tools come first in the prefix, so these invalidate everything, but only at transitions.
- If the backend takes more than 2 s, a stale variant replaces the text, with instructions stripped (`research.ts:539–552`).
- The evidence-delivery warning is pushed into the system prompt when it fires (`index.ts:707`).

**Why that costs so much.** Providers reuse a cached prefix only up to the first changed token: Anthropic caches tools, then system, then messages, and a system change invalidates the cached messages; DeepSeek needs a full match on a stored prefix unit. So every request after a write re-bills the entire history. That waste grows with the square of session length, which is why long node and coordinator sessions suffer most.

## Goals, non-goals and success criteria

The goal is a stable prefix: within one session and one run state, the system prompt and tool definitions stay byte-identical across ledger writes.

**Goals**

1. Ledger writes no longer change the system prompt or the tool definitions.
2. Uncached input per session falls, measured on paired real runs.
3. Research semantics are unchanged: checkpoint base delivery, evidence receipts, control instructions, blind-specialist isolation and per-role tool limits.
4. The change is reversible through a plugin option and needs no database migration.

**Non-goals**

- Reducing output tokens or changing the ledger protocol.
- Reducing the re-reading that comes from isolated node and specialist sessions.
- Choosing what goes into the context. Only its placement changes, plus the budget decoupling in Phase 2.
- Cache lifetime settings for long waits (listed under follow-ups).

**Success criteria**

| Criterion | How it is checked | Target |
| --- | --- | --- |
| Stable prefix | Fake-host test: system text and tool hash before and after note, checkpoint, record, publish | Identical |
| Stable part rarely changes | Per-part digests in `context_requests` on real runs | Changes only at run-state or role transitions |
| Less uncached input | Native usage (input, cache read, cache write) per session, paired runs | At least 50% lower per node session; confirm after Phase 0 |
| No delivery regressions | Counts of `checkpoint_base_required`, `checkpoint_conflict`, evidence warnings | No increase |
| No behaviour regressions | Runs finish; no-progress pauses; conclusion accepted | No increase |
| Output unchanged | Output tokens per session | Within run-to-run noise |

## Phase 0: measure and spike

Phase 0 changes no behaviour: it records a baseline and answers four host questions that decide between option A and option B.

**0.1 Measurement tools**

1. **Context churn.** Promote `context_churn.py` (from the diagnosis) to `scripts/context_churn.py`. It reads `context_requests` and `context_packs` read-only and reports, per session, how often consecutive requests carried a different block and which blocks changed. Add a column for the stable part only, using the same block allowlist as Phase 1, so the same script measures both before and after.
2. **Cache usage.** Extend `scripts/r2_measure.py`. It already deduplicates `native_usage` rows from `.research/observations/*.jsonl`, whose `tokens` field carries `input`, `output`, `reasoning`, `cache.read` and `cache.write`. Add a per-session breakdown of those five numbers and a hit ratio, cache read ÷ (input + cache read + cache write). Confirm on one request that the host's `input` excludes cached tokens before trusting the ratio.

**0.2 Baseline runs**

1. Pick one fixed goal that finishes in 30–60 minutes with two or three nodes and at least one specialist.
2. Run it twice with the current plugin: once with `context-policy r1` and exploration off, once with exploration on. Same model, same options, `concurrency` 1.
3. Save both script outputs under `docs/evidence/context-cache/baseline/`, with the model, host version and plugin commit.

**0.3 Host and provider spike**

Use an isolated host like `scripts/probe_opencode_host.py` does (private server, empty XDG directories), a scratch plugin that only appends a marker message in the `context` hook, and a local logging proxy in front of the provider (the Codex spike's `proxy.py` is a starting point).

For Q1 and Q2, reuse `scripts/probe_research_run.py`: it already runs a real host against a scripted OpenAI-compatible server (line 178) that sees every request body. Q3 and Q4 depend on the provider, so they need each real provider behind the proxy.

| Question | How to check | Option A needs |
| --- | --- | --- |
| Q1. Does a message added to `request.messages` reach the provider without being stored in the session? | Marker in the proxy log; absent from `session.context` afterwards | Reaches provider, not stored |
| Q2. Does the hook also run for compaction or title requests? | Log `request` keys and the `model.request` kind on each call | Requests can be told apart, so injection can be skipped |
| Q3. Does each provider in use accept a user message right after tool results? | Two-step tool loop through the proxy, per provider | Accepted, no error |
| Q4. With reasoning models, does the trailing message change how earlier reasoning is sent? | Compare consecutive request bodies in the proxy dump | In-turn reasoning still sent, and sent the same way each step |

**Decision rule.** Choose option A if Q1, Q3 and Q4 pass for every provider the project supports. Otherwise choose option B, which needs none of them. Q2 only decides whether a guard is needed. Record the answers in `docs/evidence/context-cache/spike.md`.

## Phase 1 design: split and place the context

Split the context by block into a stable part, which stays in the system prompt, and a volatile part, which is placed where its changes cannot invalidate the history.

**Block classification.** Stable is an allowlist; every other block, including any added later, is volatile. A volatile block left in the system prompt silently brings the bug back, while a stable block sent as volatile only costs its own size.

| Block | Part | Reason |
| --- | --- | --- |
| `role_instructions`, `control_instructions` | stable | Change only on pause, resume, stop or conclusion, when tools change too |
| `identity`, `project` | stable | Role, goal and run state; change only at transitions |
| `task`, `specialist_task`, `frozen_context` | stable | Fixed for the node, task or specialist |
| `method_guidance`, `index` | stable | Change only when guidance is re-registered |
| `attempt` | volatile | Small, and its state can change inside a turn |
| All other blocks, `omitted_index`, the stale notice | volatile | Read from ledger state written during the turn |

Text with no blocks stays where it is today: the blind-specialist body and the "control unavailable" fallback remain in the system prompt.

**Option A — trailing message (recommended).** On every request, push the stable part onto `request.system` and append one user message with the volatile part, wrapped in `<research_state>` and labelled as injected state, not user input. Per-request warnings, such as the evidence-delivery one, go in the same message. The history stays cached; the trailing block, up to about 12,000 characters less the stable part, is billed fresh on every request.

**Option B — per-turn snapshot.** Keep the whole context in the system prompt but compute it once per turn. Later steps of the turn reuse the same text unless a control field changed: role, run state, pause reason or attempt identity. One full-history miss per turn replaces one per write, and nothing changes in the message format. The cost is freshness: state written by other sessions, and block views of the model's own writes, appear only at the next turn.

&#91;embedded content: request prefix · today vs options A and B\]

Each row is one model request, read left to right. Providers reuse the cache only up to the first changed segment, so what matters is where the changing text sits.

|  | Option A | Option B |
| --- | --- | --- |
| Uncached input per step | Trailing block plus new tokens | New tokens; full history once per turn |
| State freshness | Every request | Turn start, or a control change |
| Host requirements | Spike Q1, Q3, Q4 pass | None |
| Code touched | `index.ts` only | `index.ts`, `research.ts` turn tracking |

**Rejected alternatives.** Moving the whole context to the trailing message would throw away caching of the long, stable role instructions. Appending only changed blocks as persisted messages would grow the history, needs host support for inserting mid-turn, and breaks the "delivered in this request" checks after compaction.

## Phase 1 implementation

Phase 1 is a TypeScript-only change in three files plus a new module, behind a plugin option whose default keeps today's behaviour. The patch from the diagnosis (`move-volatile-context-to-tail.patch`) is a starting point for steps 1 and 3.

1. **New module `plugin/src/context-placement.ts`.**
   - Export `STABLE_CONTEXT_BLOCKS` and `splitResearchContext(text)`, which returns `{stable, volatile}` by splitting on `^## <name>` lines, the same convention the backend parses at `memory_store.py:1413`.
   - Keep the `Research context:` header with the stable part, move the stale notice to the volatile part, and return text with no blocks as stable.
   - Export `researchStateMessage(parts)`, which builds the trailing user message.
   - Keep it out of `index.ts`: some plugin loaders treat every export of the entry module as a plugin.
2. **Plugin option `contextPlacement`** in `index.ts` (`options` at line 48): `"system"` (today, the default), `"tail"` (option A) or `"turn"` (option B). An unknown value falls back to `"system"` and logs once.
3. **Injection in `injectContext`** (`index.ts:697–707`):
   - `"tail"`: push the stable part onto `request.system`; collect the volatile part and the evidence-delivery warning; append them as one message after `await evidence`, so nothing else is appended later.
   - `"turn"`: push the frozen text (step 5) onto `request.system`.
   - Leave the error and "no drain authorization" paths in the system prompt: they are rare, and tools change there anyway.
   - If spike Q2 shows compaction or title requests pass through this hook, use today's behaviour for them.
4. **Delivery checks stay on the full text.** Keep calling `research.delivered(sessionID, text, request.messages, …)` with the unsplit text: all of it still reaches the request, so checkpoint receipts keep matching `memoryCache`. `observeEvidence` and `CheckpointDelivery.request` only read `tool-result` parts, so the added text part is ignored.
5. **Turn snapshot for `"turn"`** in `plugin/src/research.ts`:
   - Keep, per session, `{execution, stable, text, checkpoint_snapshot, checkpoint_owner}`; the turn key is `engine.executionToken(sessionID)` (`engine.ts:245`).
   - Reuse the frozen text while the execution token and the newly computed stable part are both unchanged; otherwise replace the snapshot.
   - Make `delivered()` match against the frozen snapshot, not the newest `memoryCache` entry. Without that, checkpoint receipts from context stop matching.
   - No execution token, as in some recovery paths: do not freeze.
6. **Record the placement** for measurement. In `memory()` (`research.ts:524`), add `selection.placement = {mode, stable_sha256, stable_chars, volatile_chars, tools_sha256}` to the `context_record` call. The service stores `selection` as given (`service.py:1321`), so no Python change is needed, and `context_churn.py` can read the stable digest directly. Pass a hash of the final request tools into memory() for tools\_sha256, so Phase 3 can be judged from the same records.

The injection branch for option A is short:

```ts
const { stable, volatile } = splitResearchContext(text)
request.system.push({ type: 'text', text: stable })
if (volatile) tail.push(volatile)
// … after `await evidence`, issues also go to `tail`
if (tail.length) request.messages.push(researchStateMessage(tail))
```

## Phase 2: decouple the renderers

Both renderers share one 12,000-character budget across all blocks, so a growing volatile block can shrink or drop a stable one; Phase 2 gives the stable part its own budget so it cannot change for that reason.

**Where the coupling is.**

- R1 (`context_policy.py:553`): `identity` is deleted when the indexes alone overflow (line 670). The control, role, task and project blocks are added only if they still fit (lines 688–692). `specialist_task`, `frozen_context` and `method_guidance` are added last, after findings, and dropped if findings left no room (lines 778–782).
- Legacy (`memory_store.py:1890–2019`): blocks are rendered in order against a running total. `review_challenges` is inserted first (line 1887), so it eats budget before the stable blocks; `frozen_context` and `method_guidance` come after `knowledge`. Each block is truncated by `bounded_value` or moved to `omitted_index` when the remainder is short.

**Change.**

1. Define `STABLE_BLOCKS` once in `context_policy.py`, matching the Phase 1 allowlist, and return it in the view as `placement.stable_blocks`. The plugin prefers this list over its own copy when present, so a new block cannot drift between the two.
2. Render stable blocks first, against their own budget, with today's per-block caps.
3. Render volatile blocks against the existing budget, with today's priorities and degradation order unchanged.
4. Keep the output text order deterministic. Placement is decided by block name, so the order only needs to be stable.

**Budget decision for the owner.** Today a node can carry about 10,000 characters of stable blocks when method guidance is long (role 1,600, control 800, identity 400, project 1,000, task 2,200, guidance up to about 4,000). Reserving that inside the 12,000 total would starve findings. The proposal is a separate stable budget of about 6,000 characters, with guidance capped inside it, and the volatile budget left at 12,000. The worst-case total grows, but the stable part is cached after its first request.

**Compatibility.** The new view field is optional, so an older bundled backend still works with the Phase 1 plugin. Run `bun run sync:python` after the change, as the README requires. No schema or migration change.

## Phase 3: fixed tool definitions (optional)

Do Phase 3 only if Phase 1 measurements show tool changes still cost real cache: they happen at state transitions, not on every write, so the expected gain is small.

Tool definitions come first in the prefix, so any change to them invalidates the system prompt and the history too. Today `injectContext` changes them per state:

| Change today | Trigger | Execution-time check to rely on | Proposal |
| --- | --- | --- | --- |
| `research_scope`, `research_route` removed (`index.ts:655`) | Exploration off | Backend exploration-policy checks, e.g. `exploration_store.py:916`; confirm both tools | Keep the tools; refuse with a clear message |
| `research_decide` removed, or narrowed to defer/propose\_stop (`index.ts:656–663`) | Exploration off, closing state | Not confirmed for `next_work` and `work_batch` | Keep the full schema; add the refusal if missing |
| `research_prepare_conclusion` removed (`index.ts:666`) | Not on `evidence-v1` | Not confirmed | Keep; refuse outside `evidence-v1` |
| `research_conclude` schema switched (`index.ts:667–675`) | Contract adoption | Backend validates fields per contract | One schema with both field sets optional; the description names both modes |
| Coordinator write tools removed (`index.ts:676–678`) | Pause, stop, segment end | `research_control_paused` (`plugin.test.ts:551`); confirm per tool | Owner decision: keeping them is a visible behaviour change |
| Node tools removed without drain authorization (`index.ts:689–695`) | Pause, unverified execution | n/a | Leave as is: a stop state that also changes the instructions |

**Steps.**

1. Audit each row and add the missing execution-time refusals, with tests, before removing any filtering.
2. Replace the per-state schema rewrites with one schema per role.
3. Keep the role-based filtering in `keepsTool`: it is fixed per session and does not churn.

**Trade-off.** The model will sometimes call a tool that is then refused, costing a round trip. The control instructions already say which actions are allowed, so this should be rare; the no-progress and error counters will show it if not.

## Testing plan

The key new test is an invariant: in the fake host, the system prompt and tools of a request are identical before and after a run of ledger writes. Everything runs under `make test`, as `AGENTS.md` requires.

**Unit tests — new `plugin/test/context-placement.test.ts`**

- R1 and legacy sample texts split as the classification table says, and every block lands in exactly one part.
- The `Research context:` header stays with the stable part; the stale notice moves to the volatile part.
- Text with no blocks (blind body, "control unavailable" fallback) is returned whole as stable.
- An unknown block name is volatile.
- When the backend sends `placement.stable_blocks` (Phase 2), it overrides the built-in list.

**Fake-host tests — `plugin/test/plugin.test.ts`**

The harness (`fakeHost`, `load`, `modelRequest`) sets `ctx.options` from `registryPath` and `python` only; extend `load()` to pass `contextPlacement`.

1. **Stable prefix, `"tail"`.** Start a run, propose and dispatch a node. Capture a node request, then call `research_note`, `research_checkpoint`, `research_record` and `research_publish`, and capture another. Assert `system` and the serialized `tools` are equal, the last message is the `<research_state>` message, and it changed.
2. **Same for the coordinator**, across `research_propose`, `research_dispatch` and a delivered node result.
3. **Turn snapshot, `"turn"`.** Within one execution token, `system` stays equal across writes; after the turn ends and a new one starts, it updates. A pause inside the turn replaces the snapshot.
4. **Checkpoint delivery under each mode.** Re-run the existing checkpoint-base tests (around line 631) with `"tail"` and `"turn"`: a base delivered in context is still accepted by the next `research_checkpoint`.
5. **Evidence warnings.** Parameterize the tests at lines 2816 and 2872: the warning is in `system` for `"system"` and `"turn"`, and in the trailing message for `"tail"`.
6. **Blind specialist isolation.** Extend the test at line 2259: under `"tail"` there is no trailing message, and no message contains the goal or a publication id.
7. **Default unchanged.** With no option set, every existing test passes untouched.

**Python tests — Phase 2, `tests/test_context_policy.py`**

- For both policies, the stable blocks render byte-identically as notes, findings and review items grow past the budget.
- No stable block is truncated or moved to `omitted_index` because of volatile content.
- The view carries `placement.stable_blocks`; the volatile budget and degradation order are unchanged.
- Update the `tests/fixtures/context_policy/` replay fixture if its expected text changes.

**Real host.** After unit tests pass, run `scripts/probe_research_run.py` with `contextPlacement` set to the chosen option, as a smoke test that a full run still completes against a real host. The probe writes plugins as plain paths today (line 177), so switch it to the package-with-options form first.

## Measurement and acceptance

The default changes only after paired real-model runs show less uncached input with no loss in behaviour or report quality; merging Phase 1 with the default unchanged needs only the tests and the spike.

**Paired runs.** Reuse `scripts/e3_evaluation.py`: it freezes a configuration, gives both groups the same tasks and seeds, randomizes run order and keeps scoring keys outside the research projects. Its four tasks run in one setup; add the Phase 0 baseline goal as a fifth, longer run. Its configuration fixes software versions per group, so add a per-group plugin options field.

| Setting | Control | Treatment |
| --- | --- | --- |
| `contextPlacement` | `"system"` | Option chosen in Phase 0 |
| Everything else | Same model, variant, host, plugin commit, tools, R1 policy, budgets, `concurrency` 1 | Same |
| Runs | 3 per task, exploration on, plus the baseline goal with exploration off and on | Same |

**What to report**, per session role (coordinator, node, specialist):

- Input, cache read, cache write, output and reasoning tokens, and the hit ratio from Phase 0.
- Uncached input in the provider's own terms: misses for DeepSeek, input plus cache writes for Anthropic.
- Cost at the provider's published prices for each token kind.
- From `context_requests`: stable-part changes per session against the run-state transitions in that session, and the trailing block's mean size.
- Rejection counts by code from `r2_measure.py`, including `checkpoint_base_required` and `checkpoint_conflict`. The evidence-delivery warning is not logged today, so add a counter beside `memoryObservations`.
- Outcome: nodes finished, no-progress pauses, retries, conclusions accepted, and the blind scores from the evaluation.

**Gates**

| Gate | Needs |
| --- | --- |
| Merge Phase 1, default unchanged | `make test` green; real-host probe passes; spike answers recorded |
| Make the new placement the default | Both exploration settings show at least 50% less uncached input per node session; no rise in rejections, pauses or retries; scores within run-to-run spread |
| Start Phase 3 | Tool hashes show tool changes still cause a measurable share of misses after Phase 1 |

Three runs per task are descriptive, not statistically significant, as the evaluation README already says. State that in the evidence record.

## Rollout and repository conventions

Ship in five pull requests, each small enough to review alone; only the fourth changes what users get by default.

&#91;embedded content: rollout · 5 pull requests, 4 gates\]

Each pull request waits for the gate above it. The paired runs follow PR 3, so they test the final configuration.

**Repository rules (`AGENTS.md`)**

- Branches use the `cursor/` prefix; sync with `main` by merge, never rebase or force-push.
- Verify with `make test`. Never push to `main`, merge pull requests, tag or release.
- The PR body has exactly three sections: Why, Verified (commands, results, and whether run in the agent VM or in GitHub CI) and Not verified.
- Before writing any compatibility or version wording, check the decisions in `docs/evidence/*/review-response.md`.

**Documentation per PR**

- `README.md`: add `contextPlacement` to the plugin options table with its default, and note that `opencode service restart` applies it.
- `plugin/README.md`: a short section on where the research context goes and why, beside the R1 policy section.
- `CHANGELOG.md`: one entry per PR that changes behaviour. Leave the version number to the maintainer.
- `docs/evidence/context-cache/README.md`: baseline, spike answers, paired results and limits, in the style of the other evidence records; link it from `docs/README.md`.
- Phase 2 also needs `bun run sync:python`, and the PR should say so.

**Compatibility and rollback.** No schema change and no migration; old projects work unchanged. The only stored difference is extra fields in `context_requests.selection`. Rolling back is setting `contextPlacement` to `"system"` and restarting the service.

## Risks and open questions

The biggest risks are host and provider behaviour the plugin cannot see from its own code; the spike exists to retire them before any default changes.

| Risk | Impact | Mitigation or check |
| --- | --- | --- |
| The host stores hook-added messages in the session | Every step adds a copy of the state to the history | Spike Q1; fall back to option B |
| A provider rejects a user message after tool results, or drops in-turn reasoning around it | Request errors, or weaker multi-step reasoning | Spike Q3 and Q4 per provider; option B |
| The model treats the trailing state as a user instruction, or over-weights it | Behaviour drift | `<research_state>` wrapper and label; instructions stay in the system prompt; blind scores in the paired runs |
| The hook also runs for compaction requests | A state snapshot is baked into the summary | Spike Q2; skip injection there (today's behaviour has the same exposure) |
| Checkpoint or evidence receipts stop matching | Writes refused with `checkpoint_base_required`, more re-reads | `delivered()` keeps the full text; tests 4 and 5; rejection counts in the paired runs |
| Blind specialist sees project data through the trailing message | Isolation broken | The backend sends blind sessions no blocks; test 6 asserts no trailing message |
| The stable part still changes before Phase 2, because of budget coupling | Smaller gain than expected | Stable digests in `context_requests` show it; Phase 2 |
| The trailing block is large for an exploring coordinator | Per-request cost stays high | Report its mean size; follow-up to send only what each role needs |
| Anthropic breakpoints land on the trailing block | A cache write each request that is never read | Measure cache writes; small next to today's re-billing |
| Option B shows stale state within a turn | Duplicate work or a decision on old counts | Refresh on control changes; the model sees its own tool results; paired runs |
| The 2-second backend fallback strips instructions | The stable part flips when the backend is slow | Count stale notices; keep the safety behaviour |

**Open questions for the owner**

- [ ] Which providers must be supported? That sets the spike's scope.
- [ ] Is a separate stable budget of about 6,000 characters acceptable (Phase 2)?
- [ ] Is keeping refused tools visible acceptable (Phase 3)?
- [ ] If option A passes the spike, ship only A and `"system"`, or keep B as well?

## Out of scope and follow-ups

These also affect token use but are separate changes, each worth measuring on its own after this plan lands.

- **Cache lifetime during waits.** The coordinator idles while nodes run; with Anthropic's default 5-minute cache, each wake re-reads its history. A 1-hour cache costs 2× on writes and needs the host to pass per-request cache options. Measure wait lengths first.
- **A smaller volatile part.** Drop what duplicates the session's own transcript, such as notes and the checkpoint it just wrote, or send each role only the blocks it uses.
- **Re-reading across isolated sessions.** Give nodes compact frozen excerpts as inputs instead of whole publications. Specialists already get a 1,600-character preview.
- **Output tokens.** The ledger protocol's structured writes and re-syntheses after checkpoint conflicts need their own study.
- **Pretty-printed tool results** (`index.ts:591`). On the repo's E3 trace, compact JSON was only about 2% smaller. Changing it also means updating the exact-text matching in `checkpoint-delivery.ts:36` and `evidence-delivery.ts:147`.

## Sources

- [Anthropic — Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [DeepSeek — Context caching](https://api-docs.deepseek.com/guides/kv_cache)
- [auto-research-opencode v0.2.1](https://github.com/RinshannKaihou/auto-research-opencode), commit `fd1c62d`
