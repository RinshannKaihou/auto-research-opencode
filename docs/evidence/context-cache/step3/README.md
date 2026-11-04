# System vs tail placement: real-API pair

2026-11-04 · plugin code `a990fc5` (worktree `d4ba19d`) · OpenCode 2.2.5 · one paired run, exploration off.

This is the first real-model comparison of `contextPlacement: "system"` and `"tail"`. It is descriptive, not statistically significant. The default remains `"system"`.

The repeated-wait behavior in this pair led to the bounded tail-only wait completion recorded in [review-response.md](review-response.md); it is locally verified but has not been rerun on a real model route. The numbers below are unchanged historical observations.

## Setup

| Setting | Value |
|---|---|
| Model route | `gateway/volcengine/deepseek-v4.1-flash` (OpenAI-compatible gateway from the user's OpenCode config) |
| Host | Private `opencode serve`, one isolated XDG root and one research project per arm |
| Context policy | R1 (`memory-r1-v1`) |
| Exploration | off |
| Concurrency | 1 |
| Budgets | 24 turns per session, 3 nodes, 1800 s wall clock, 20,000,000 observed tokens |
| Goal | "Using Python 3 on this machine, measure the median wall-clock time of summing 1..1,000,000 with a for loop versus `sum(range(1, 1000001))`, 15 repetitions each, in the same process. Publish a short report with the script, both medians, and the raw timings. Ask an independent reviewer to check that the two conditions are comparable." |
| Run order | `tail`, then `system` |

The gateway reports implicit prefix-cache usage, not explicit `cache_control` results. A direct three-request check on the same model showed `prompt_tokens` is the inclusive total: a repeated 14,670-character prefix with a different final sentence reported `prompt_tokens: 2465` and `cached_tokens: 2432`; warming up and changing an early character both reported `cached: 0`. No cache-write field is returned. The host records `TokenUsage.input` as the non-cached portion, with `prompt = input + cache.read + cache.write`; `cache.write` was 0 in every row. Cost was not reported by this gateway, so this record uses tokens only.

## Accounting: primary, compaction, and unknown

The observation log records **primary** model requests. The host's `session_v2` aggregate also includes auxiliary compaction usage, which the observation rows omit. Both arms had exactly one compaction request:

| Item | System | Tail |
|---|---:|---:|
| Primary completed requests | 226 | 275 |
| Primary uncached input | 2,336,109 | 742,638 |
| Compaction session | node X-003 | coordinator |
| Compaction uncached input | 109,772 | 114,992 |
| Compaction cache read | 6,144 | 6,144 |
| Compaction output | 1,898 | 1,439 |
| **Whole-run known requests** | **227** | **276** |
| **Whole-run known uncached input** | **2,445,881** | **857,630** |
| Whole-run known cache read | 9,011,072 | 13,606,656 |
| Whole-run known prompt | 11,456,953 | 14,464,286 |
| Whole-run known output | 120,411 | 70,438 |
| Whole-run known reasoning | 87,450 | 62,973 |
| Hit ratio, whole-run known | 78.7% | 94.1% |
| Requests without usage | 0 | **1** |

The extractor counts each completed compaction message individually and keeps its per-message `tokens`; a session with two compactions therefore contributes two requests, not one, and no session-wide delta is repeated across them. This pair has one compaction per arm, so the totals above are unchanged.

The tail coordinator has one final assistant message without a usage record, immediately after the conclusion; it is counted as **one request with unknown usage**, not as zero. It is not included in the host aggregate either, so its tokens are genuinely unknown rather than zero. Complete accounting therefore remains unavailable for the tail arm. On the known subtotal, uncached input falls by **64.9%** (from 2,445,881 to 857,630). The primary-only subtotal falls by 68.2%; the difference is the compaction usage above.

## Results by role (primary completed)

| Role | Arm | Requests | Uncached input | Cache read | Prompt | Hit | Output | Reasoning | Uncached / request |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| coordinator | system | 29 | 548,996 | 628,864 | 1,177,860 | 53.4% | 10,471 | 7,481 | 18,931 |
| coordinator | tail | 191 | 483,401 | 10,663,296 | 11,146,697 | 95.7% | 20,873 | 34,297 | 2,531 |
| nodes | system | 170 | 1,657,271 | 8,002,176 | 9,659,447 | 82.8% | 95,821 | 34,755 | 9,749 |
| nodes | tail | 65 | 238,687 | 2,800,768 | 3,039,455 | 92.2% | 41,919 | 18,189 | 3,672 |
| specialists | system | 27 | 129,842 | 373,888 | 503,730 | 74.2% | 12,221 | 45,214 | 4,809 |
| specialists | tail | 19 | 20,550 | 136,448 | 156,998 | 86.9% | 6,207 | 10,487 | 1,082 |

On the whole-run known subtotal, uncached input per known request is **10,775 → 3,107 (−71.2%)**, and the uncached share of prompt tokens is **21.4% → 5.9%**. On the primary-only subset, the per-request fall is 73.9%.

Read the totals with the work difference in mind. On primary requests the tail arm made **49 more requests** and used **26.5% more prompt tokens**, but most of the extra prompt was cache reads. On the whole-run known totals it produced **41.5% fewer output tokens** and **28.0% less reasoning**, and it finished in **572 s instead of 1453 s**. It also did less work: 3 nodes (2 finished, 1 cancelled), 1 specialist and 4 publications, versus 3 finished nodes, 4 specialists and 6 publications in the system arm. The per-request and per-role figures are the meaningful comparison; the total uncached reduction is not a like-for-like cost claim.

## Repeated waiting in the tail coordinator

The tail coordinator called `research_wait` **104 times**, versus **3** in the system arm. The calls sit in 104 assistant messages: 102 messages contain only `research_wait`, and 2 also contain another tool:

| Item | System | Tail |
|---|---:|---:|
| Wait messages | 3 | 104 |
| Wait-only / mixed with another tool | 3 / 0 | 102 / 2 |
| Before / after first node finish | 1 / 2 | 38 / 66 |
| Gap between waits, min / median / max | 272.6 / 402.1 / 531.5 s | 0.848 / 2.139 / 57.472 s |
| Gaps under 1 s | 0 | 2 |
| Wait prompt tokens | 129,297 | **6,021,092** |
| Wait uncached input | 71,569 | 233,188 |
| Wait hit ratio | 44.6% | 96.1% |
| Wait output | 211 | 6,080 |
| Non-wait requests (with usage) | 26 | 87 (plus one unknown) |
| Non-wait prompt tokens | 1,048,563 | 5,125,605 |
| Non-wait uncached input | 477,427 | 250,213 |
| Non-wait hit ratio | 54.5% | 95.1% |

The wait requests are 54% of the tail coordinator's prompt tokens (6.0M of 11.1M) and 54% of its assistant messages. Their 96.1% hit ratio is slightly above the 95.1% of its non-wait requests, so the polling lifts the coordinator's aggregate ratio a little; the larger effect is the extra 104 requests and 6.0M mostly-cached prompt tokens. Excluding the waits, the observed reduction is **84.3% per non-wait request** (2,876 vs 18,363). That is an observed difference in uncached input per request, not proof that the non-wait work in the two arms was equivalent.

**Why it kept running.** Repeated waiting occurred in the tail arm. The retained trace shows consecutive wait-only assistant messages about 2 s apart with no synthetic notification between them. `research_wait` writes the wait control state and returns an ordinary tool result; the wait tool permits continued steps, so the host can start another step and the model can call it again before the execution ends. The history shows the tail coordinator repeatedly checking that node X-001 was still open; the coordinator's own reasoning at these steps says it is waiting again. X-001 stayed open for about four minutes. The system coordinator had non-wait work between its three waits (272–531 s apart). The shared wait mechanism explains how continued calls are possible, not why they were extensive in the tail arm. A temporary user message could interact with model behavior, and this pair cannot exclude a placement contribution, so **placement's contribution remains undetermined**. The wait/turn behavior needs its own change and test before another paid pair; it does not justify renderer or ledger redesign here.

## Behaviour and delivery

- Both runs ended `complete`; both accepted a conclusion (`C-001`, `outcome: answered`). The reports include the script, medians, raw timings and an independent review.
- No checkpoint or evidence-delivery rejection codes occurred in either arm (no `checkpoint_base_required`, `checkpoint_conflict`, or evidence receipt failures).
- No provider retries. Each arm had one compaction request. The recorded `context_requests` counts are 227 (system) vs 226 primary requests and 276 (tail) vs 276 primary requests. The system extra is consistent with the compaction request passing through the context hook; the tail also has one final assistant request without usage, so its extra record could be either. The captured body was not inspected, so auxiliary-hook behavior remains a verification limit.
- Tool-level failures were model corrections, not delivery rejections: system had 5 validation errors (`research_propose` / `research_publish` / `research_record`); tail had 12 `research_close_node` conflicts plus a few validation/close errors.
- Stable allowlisted blocks: the tail coordinator had 3 distinct stable digests over 192 recorded requests (2 changes: start transition and conclusion), with **185 consecutive requests byte-identical**. Its node sessions each had one digest. The system coordinator had 3 digests and 7 changes in 30 requests, driven by `project` wait-state toggles; its nodes also kept one stable digest each. No stable block shrank or disappeared under budget pressure in either arm; the largest tail volatile part was about 8.4k characters against a 2.6k stable part.

## Limits

- One pair, one goal, one model route and one machine. Not statistically significant.
- The arms did different amounts of work and took different times; totals are not a controlled cost comparison.
- `tail` ran first. The system arm's first request may have inherited a warm provider prefix from the tail arm; its overall hit ratio is far below the tail arm's, so the direction is clear, but the exact system figure could be slightly flattered.
- The route is the configured `gateway`, not the `opencode-go` route used by the current session. Only `volcengine/deepseek-v4.1-flash` was tested.
- The tail wait loop adds requests and prompt tokens; its relationship to placement remains undetermined, and a separate wait/turn change would be needed to remove it. The system arm also did more non-wait work, so the pair cannot separate cache placement from the two trajectories.
- No exploration-on arm, no repeats, and no per-model/provider certification. The default-change gate (both exploration settings, no rise in rejections/pauses/retries, quality within noise) is not met.
- `cache.write` is unreported by this gateway and recorded as 0; cost is unavailable. Cached tokens still count as prompt tokens.
- Report quality: both runs answered the task with evidence and independent review; the system arm's report was more extensive because it ran more nodes and specialists. This pair does not show that placement improves or degrades research quality.
- Auxiliary (compaction/title) hook behavior was not captured from the wire; the request counts alone do not settle it.
- An earlier pilot used a 1.5M observed-token cap; it hit the cap after about 8 minutes and paused mid-run. It was discarded and is not part of these numbers.

## Reproduction

Committed evidence tooling and sanitized source records:

- `scripts/context_placement_evidence.py` — offline extractor and report. It reads an isolated arm root (research ledger + OpenCode host DB) and writes sanitized records; it never copies prompt or report text.
- `records/system/`, `records/tail/` — sanitized per-arm records: session roles and host aggregates, per-request primary usage (JSONL), per-request coordinator message metadata (JSONL), per-request stable-part digests (JSONL), per-compaction message metadata and token fields, notification metadata, and the unknown-usage list.
- `corrected-report.json` — the machine-readable output of the `report` command.

```bash
# Re-extract from an isolated arm root (optional; the committed records already exist)
python3 scripts/context_placement_evidence.py extract \
  --root <isolated-arm-root> \
  --output docs/evidence/context-cache/step3/records/<arm>

# Recompute the corrected tables from the committed records
python3 scripts/context_placement_evidence.py report \
  docs/evidence/context-cache/step3/records/system \
  docs/evidence/context-cache/step3/records/tail \
  --output docs/evidence/context-cache/step3/corrected-report.json
```

The isolated roots contain `project/.research` and `data/opencode/opencode.db`. The temporary run harness itself is not committed; the extractor and records are sufficient to audit the token, compaction, wait and stable-block figures above.

The two earlier JSON summaries ([tail-run.json](tail-run.json), [system-run.json](system-run.json), [tail-analysis.json](tail-analysis.json), [system-analysis.json](system-analysis.json)) remain as run summaries and per-session aggregates; the `records/` files are the audit source.
