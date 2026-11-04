# System vs tail placement: real-API pair

2026-11-04 · plugin code `a990fc5` (worktree `d4ba19d`) · OpenCode 2.2.5 · one paired run, exploration off.

This is the first real-model comparison of `contextPlacement: "system"` and `"tail"`. It is descriptive, not statistically significant. The default remains `"system"`.

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

## Results

Uncached input is the host's `input` field. "Requests" are native completed model requests. `hit` is `cache.read / prompt`.

| Role | Arm | Requests | Uncached input | Cache read | Prompt | Hit | Output | Reasoning | Uncached / request |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| coordinator | system | 29 | 548,996 | 628,864 | 1,177,860 | 53.4% | 10,471 | 7,481 | 18,931 |
| coordinator | tail | 191 | 483,401 | 10,663,296 | 11,146,697 | 95.7% | 20,873 | 34,297 | 2,531 |
| nodes | system | 170 | 1,657,271 | 8,002,176 | 9,659,447 | 82.8% | 95,821 | 34,755 | 9,749 |
| nodes | tail | 65 | 238,687 | 2,800,768 | 3,039,455 | 92.2% | 41,919 | 18,189 | 3,672 |
| specialists | system | 27 | 129,842 | 373,888 | 503,730 | 74.2% | 12,221 | 45,214 | 4,809 |
| specialists | tail | 19 | 20,550 | 136,448 | 156,998 | 86.9% | 6,207 | 10,487 | 1,082 |
| **total** | **system** | **226** | **2,336,109** | **9,004,928** | **11,341,037** | **79.4%** | **118,513** | **87,450** | **10,337** |
| **total** | **tail** | **275** | **742,638** | **13,600,512** | **14,343,150** | **94.8%** | **68,999** | **62,973** | **2,700** |

Per-request uncached input fell by **86.6%** for the coordinator, **62.3%** for nodes, **77.5%** for specialists, and **73.9%** overall. The uncached share of prompt tokens fell from **20.6% to 5.2%**.

Read the totals with the work difference in mind. The tail arm made **49 more requests** and used **26.5% more prompt tokens**, but most of the extra prompt was cache reads. It produced **41.8% fewer output tokens**, **28.0% less reasoning**, and finished in **572 s instead of 1453 s**. It also did less work: 3 nodes (2 finished, 1 cancelled), 1 specialist and 4 publications, versus 3 finished nodes, 4 specialists and 6 publications in the system arm. The per-request and per-role figures are the more meaningful comparison; the total uncached reduction is not a like-for-like cost claim.

## Behaviour and delivery

- Both runs ended `complete`; both accepted a conclusion (`C-001`, `outcome: answered`). The reports include the script, medians, raw timings and an independent review.
- No checkpoint or evidence-delivery rejection codes occurred in either arm (no `checkpoint_base_required`, `checkpoint_conflict`, or evidence receipt failures).
- No provider retries. Each arm had one compaction request. The recorded `context_requests` counts are 227 (system) vs 226 primary requests and 276 (tail) vs 276 primary requests, so this pair does not settle whether the compaction request passed through the primary context hook; auxiliary-hook behavior remains a verification limit.
- Tool-level failures were model corrections, not delivery rejections: system had `research_propose`/`research_publish`/`research_record` validation errors (5 total); tail had 12 `research_close_node` conflicts plus a few validation/close errors.
- Stable allowlisted blocks: the tail coordinator had 3 distinct stable digests over 192 recorded requests (2 changes: start transition and conclusion), with **185 consecutive requests byte-identical**. Its node sessions each had one digest. The system coordinator had 3 digests and 7 changes in 30 requests, driven by `project` wait-state toggles; its nodes also kept one stable digest each. No stable block shrank or disappeared under budget pressure in either arm; the largest tail volatile part was about 8.4k characters against a 2.6k stable part.

## Limits

- One pair, one goal, one model route and one machine. Not statistically significant.
- The arms did different amounts of work and took different times; totals are not a controlled cost comparison.
- `tail` ran first. The system arm's first request may have inherited a warm provider prefix from the tail arm; its overall 79.4% hit ratio is far below the tail arm's 94.8%, so the direction is clear, but the exact system figure could be slightly flattered.
- The route is the configured `gateway`, not the `opencode-go` route used by the current session. Only `volcengine/deepseek-v4.1-flash` was tested.
- No exploration-on arm, no repeats, and no per-model/provider certification. The default-change gate (both exploration settings, no rise in rejections/pauses/retries, quality within noise) is not met.
- Auxiliary (compaction/title) hook behavior was not captured from the wire; the request counts alone do not settle it.
- An earlier pilot used a 1.5M observed-token cap; it hit the cap after about 8 minutes and paused mid-run. It was discarded and is not part of these numbers.
- `cache.write` is unreported by this gateway and recorded as 0; cost is unavailable. Cached tokens still count as prompt tokens.
- Report quality: both runs answered the task with evidence and independent review; the system arm's report was more extensive because it ran more nodes and specialists. This pair does not show that placement improves or degrades research quality.

## Files and commands

- [tail-run.json](tail-run.json), [system-run.json](system-run.json): runner summaries.
- [tail-analysis.json](tail-analysis.json), [system-analysis.json](system-analysis.json): per-session usage, stable-block digests and publications.

A temporary runner (not committed; it is an experiment harness, not plugin or test code) ran each arm as:

```bash
python3 compare_placement.py --placement <tail|system> \
  --plugin <worktree>/plugin --wall-seconds 1800 --max-nodes 3 \
  --max-turns 24 --max-tokens 20000000 --goal "<goal>" --output <arm>-run.json
```

It starts a private `opencode serve` with empty XDG directories, one project, the worktree plugin and the real gateway provider, drives `init / context-policy r1 / exploration off / budget / auto` through the plugin RPC, and stops at the terminal project state. A temporary analyzer read `.research/observations/*.jsonl`, the ledger and `context_requests` read-only to produce the two analysis JSON files. The raw records and the description above are the reproducible evidence; the harness itself is not part of the patch.
