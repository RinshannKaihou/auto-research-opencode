# Tail as the default context placement

2026-11-06. The user requested `tail` as the default after reviewing four completed local research runs. This decision supersedes the system-default decision in the earlier [review response](../review-response.md); it does not declare the earlier verification gates satisfied.

## Scope

- Omitting `contextPlacement` now selects `tail`, including existing configurations that omit the option.
- Explicit `"system"` retains the full-system path. Unknown values still warn once and fall back to `system`.
- The default reading policy remains Legacy (`memory-v2`); R1 (`memory-r1-v1`) remains opt-in. Neither policy implementation changes.
- No changes to schemas, context budgets, terminal handling, provider routing or the installed local plugin. Applying this source change requires updating the plugin and restarting the OpenCode service.

## Local observations behind the decision

All four runs used the same goal and `opencode-go/deepseek-v4.1-flash` with variant `max`. Each reached workflow `complete` and conclusion `partial`. These are user-run observations, not a new model experiment performed by this PR.

| Policy / placement | Calls with usage | Uncached input | Cache read | Weighted hit rate | Records without usage |
|---|---:|---:|---:|---:|---:|
| R1 / tail | 611 | 13,114,020 | 58,354,432 | 81.65% | 3 |
| R1 / system | 1,034 | 17,751,965 | 137,279,360 | 88.55% | 5 |
| Legacy / tail | 651 | 7,417,984 | 70,327,680 | 90.46% | 5 |
| Legacy / system | 679 | 30,474,051 | 51,151,232 | 62.67% | 12 |

Accounting includes coordinator, node and specialist sessions linked by each research ledger. Session totals, per-message usage and deduplicated plugin observations agreed for all recorded token fields. Input is noncached; prompt is input plus cache read plus cache write (zero here); hit rate is total cache read divided by total prompt. Missing usage is unknown, not zero. Private goals, transcripts and raw research artifacts are not included here.

Legacy tail had 75.66% less uncached input, with only 4.75% less total prompt. R1 tail had 26.13% less uncached input but substantially fewer calls and less total prompt. Its final delivery-repair node made 84 calls with zero reported cache reads, consuming 7,502,568 uncached input tokens (57.2% of that arm's uncached input); the cause remains unresolved. This cost stays in the final comparison. Over the first six hours, weighted hit rates were R1 tail/system 92.87%/89.49% and Legacy tail/system 91.20%/67.86%, but equal elapsed time still does not imply equal work.

## Limits retained

- One run per arm, different research paths and materials, system arms starting about two hours later, overlapping provider load and retries. These observations do not establish universal savings, a price-weighted cost reduction, research-quality superiority, or a winner between R1 and Legacy.
- No provider request-body capture or independent route/reasoning acceptance certification was added by this PR. Completion on this route is operational evidence, not certification of all terminal payloads or other providers.
- The historical [node terminal-input handoff limitation](../handoff/README.md) remains: an input delivered during node finishing may receive only a tool-free text response, without a subsequent normal node execution.
- Shared context-budget coupling, cache expiry, repeated-work behavior and the R1 zero-cache node remain unresolved. Explicit `system` is the rollback option.

## Verification for this change

Run in the local macOS arm64 agent environment, not GitHub CI:

- `PYTHONDONTWRITEBYTECODE=1 make test`: 886 Python tests passed; 352 Bun tests passed, zero failures; TypeScript typecheck passed. Regression coverage checks omitted placement under both reading policies, explicit system placement and unknown-value rollback. Existing terminal, blind-review, history-ownership and placement-boundary tests passed.
- `python3 scripts/probe_research_run.py --wait-finish --request-dump /tmp/default-tail-native-requests.json`: omitted-option lifecycle passed; one node finished. Wire inspection found six research requests: four tails and two tool-free finishing requests (one wait, one node finish), complete tool pairing, no unaccounted requests and at most four cache markers. This omitted-option command does not query persisted history.
- `python3 scripts/probe_research_run.py --context-placement system --request-dump /tmp/default-tail-system-control-requests.json`: explicit-system control passed; one node finished, seven requests, no temporary tails on wire.
- `python3 scripts/probe_research_run.py --context-placement tail --wait-finish --queued-input`: coordinator handoff gate passed; nine requests, one wait finishing request, one node finishing request, one post-terminal request, complete tool pairing, no persisted tail, and one guidance note delivered after the wake.

The native probes used isolated temporary projects, separate XDG state and a loopback scripted provider. No external-model request or shared-service restart was made. Full TLC was not rerun; terminal handoff and provider-certification limits above remain open. GitHub CI results are recorded separately on the PR.
