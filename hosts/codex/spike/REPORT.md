# Phase-0 spike: Codex app-server + OpenCode Go (`deepseek-v4.1-flash`)

Date: 2026-11-02 (Asia/Shanghai, UTC+8) · Box: Linux 6.12, Node v24.22.1, Python 3.13.5
**Codex CLI: `codex-cli 0.166.0`** (`npm i --prefix npm @openai/codex@0.166.0`; bin `npm/node_modules/.bin/codex`, app-server reports `appServerVersion 0.166.0`)
Paths below are relative to the spike root (the directory that contained `scripts/`, `npm/`, `home/`, `work/`, and `logs/`).
Protocol schema generated with `codex app-server generate-json-schema --out schema/` (v1 + v2, 39 top-level + 282 v2 files).
No git repo cloned, GitHub not touched. Model calls: ~157 through the logging proxy + 2 direct + 5 raw header probes ≈ 164 total.

## Recommendation: **GO** (with the caveats under Top risks)

All three go/no-go checks (S1–S3) pass cleanly. A daemon speaking JSON-RPC to `codex app-server` over stdio can drive OpenCode Go's undocumented `/responses` endpoint, including tool loops (shell + stdio MCP), multi-turn continuity, concurrent threads, interrupt, resume after restart, hooks, per-thread config, and accurate token accounting.
The biggest open items: idempotency has to live in the orchestrator, the `/responses` endpoint isn't documented, Codex has no metadata for this model, and the API key leaks into hook processes.

## Results

| # | Check | Result | One-line evidence |
|---|---|---|---|
| S1 | app-server starts, initialize → thread/start → turn, streaming | **PASS** | turn `completed`, final `PONG-S1`, streamed `item/agentMessage/delta`, `thread/tokenUsage/updated`. Also works with no proxy on both base URLs (`/zen/go/v1` and `/inference/go/openai/v1`). |
| S2 | Tool calling via Responses (shell + local stdio MCP), ~20-step loop | **PASS** | 3 runs × (1 shell + 1 `lookup` + 18 `step`) = 20 tool calls / 21 model calls each. 0 malformed or failed calls, args followed `next` correctly, all 3 runs matched the expected 18-token chain. Reasoning items went back every request (no dropped reasoning). Garble probe (EN/ZH/code/list/after-tools, 5 turns): 0 U+FFFD, 0 control chars, 0 mojibake, deltas == final text. |
| S3 | Multi-turn continuity; 2 concurrent threads in 1 process | **PASS** | 3-turn thread recalled `CODE=ZEBRA-4417 LABEL=BETA-0042`. Two concurrent threads (APPLE/BANANA) each finished 3 MCP calls with correct tokens and no cross-talk in 4.4 s wall, with 7 overlapping HTTP requests. |
| S4 | Per-thread/per-turn differences | **PASS** | Per-thread `developerInstructions` were honored (tags `[ALPHA-THREAD]` / `[BETA-THREAD]`). Per-thread MCP allowlist through `thread/start.config` worked: A saw only `lookup`, B only `step`, C (`enabled=false`) saw NONE and its proxy tool list dropped the MCP namespace. Per-turn `effort` reached the wire as `reasoning.effort=low/high`. Per-turn `model` override reached the wire (bogus model → provider 400 → turn `failed`). Note: turn overrides stick for later turns too. |
| S5 | Interrupt running turn; resume after restart | **PASS** | `turn/interrupt` during a 40 s MCP tool and during text streaming: `turn/completed status=interrupted` within 0.01 s. The next turn on the same thread worked. After killing app-server, a new process ran `thread/resume`: history kept 3 turns (`interrupted, interrupted, completed`) and the model recalled `CODE=KIWI-9031`. |
| S6 | `clientUserMessageId` / idempotency | **PARTIAL** | The id is echoed as `item.clientId` on the `userMessage` item (notifications and `thread/read`), so you can correlate. It does **not** dedupe. A sequential duplicate created a new turn. A concurrent duplicate returned the *same* turnId but appended a 2nd userMessage (steer) and caused an extra model call. Retries must be deduped by the orchestrator (check `thread/read` for the `clientId` first). |
| S7 | Hooks: UserPromptSubmit, SessionEnd, PreToolUse blocking; trust | **PASS** | In app-server: SessionStart, UserPromptSubmit (its `additionalContext` reached the model: `MAGIC=8128`), PreToolUse deny on `mcp__fixture__lookup` (model saw `BLOCKED: lookup is blocked by spike policy hook`, the tool never ran), PostModelRequest (includes token_usage), and SessionEnd (on `thread/archive` and on process exit) all fired. The client also gets `hook/started` / `hook/completed` notifications (`status: blocked`). **Trust is required:** untrusted user hooks are skipped *silently* (exec run: 0 hook invocations, no warning). Ways to trust: write `[hooks.state."<key>"] trusted_hash = "<currentHash>"` from `hooks/list`, or pass `codex exec --dangerously-bypass-hook-trust`. Both verified. |
| S8 | Per-turn token usage vs provider | **PASS** | `thread/tokenUsage/updated` fires once per model call (`last` + cumulative `total`). Over 21 calls: 0 mismatches against provider `response.completed.usage`. Totals: input 153993 / cached 150656 / output 1317, identical on both sides. Provider-only extras are not surfaced by Codex: `usage.opencode.list_cost` (sum $0.001743) and `cost` (0, `billing_source: go-plus`). `reasoning_tokens` is always 0 even though reasoning items are returned. |
| S9 | Compaction/truncation observable? | **PARTIAL** | Compaction is observable as a `contextCompaction` item (`item/started` / `item/completed`) for both manual `thread/compact/start` and auto (per-thread `model_auto_compact_token_limit`). It runs locally via a 0-tool `/responses` call, and recall survived (`MANGO-5522`, `PAPAYA-7788`). There is no `thread/compacted` notification. Tool-output truncation is *not* observable: the client got all 468 KB from `seq 1 80000` while the model got a head/tail cut (~5.6k tokens, `Total output lines: 80000` marker in the rollout), and no event was emitted. |
| S10 | MCP tool timeout with slow tool | **PASS** (behavior characterized) | Default 60 s: `slow(75)` → mcpToolCall `failed` at 60002 ms with "timed out awaiting tools/call after 60s"; the turn continued and the model reported it. Per-thread `config: {"mcp_servers.fixture.tool_timeout_sec": 5}` → failed at 5002 ms. The MCP server process keeps running the call after timeout (`slow(10)` logged `finished` at +10 s). |
| S11 | Static `x-opencode-session` across threads; 500s / rate limits | **PASS** | 157 proxied requests: 156 × 200 and 1 × 400 (the deliberate bogus model). 0 × 5xx, 0 × 429. 6 threads × 2 turns concurrently on one static session: max concurrency 6, all correct. **Important:** the static header is not needed. Codex already sends `session-id: <threadId>` on every request, and OpenCode accepts `session-id` as the session key (raw probe: none → 400 MissingSessionID; `x-opencode-session` → 200; `session-id` → 200; `thread-id` / `x-codex-window-id` → 400). With `http_headers` removed, Codex turns still return 200. A per-thread override also works: `thread/start.config` `{"model_providers.opencode_go.http_headers": {...}}` → seen on the wire. |

## Exact config (`home/config.toml`; the key is only referenced through `env_key`, never stored)

Spike only: `sandbox_mode = "danger-full-access"`, `approval_policy = "never"`, and `scripts/appclient.py` auto-accepts every approval and elicitation. Do not copy this into production.

```toml
model = "deepseek-v4.1-flash"
model_provider = "opencode_go"
model_context_window = 128000
model_reasoning_summary = "none"
approval_policy = "never"
sandbox_mode = "danger-full-access"
check_for_update_on_startup = false
web_search = "disabled"

[analytics]
enabled = false
[feedback]
enabled = false
[otel]
exporter = "none"
metrics_exporter = "none"
trace_exporter = "none"
[history]
persistence = "none"

[features]
enable_request_compression = false   # so the logging proxy can read bodies
memories = false
multi_agent = false
goals = false
apps = false
remote_plugin = false
plugins = false                      # stops the chatgpt.com featured-plugins call (401 warning)

[model_providers.opencode_go]
name = "OpenCode Go"
base_url = "http://127.0.0.1:18080/v1"     # logging proxy -> https://opencode.ai/zen/go/v1 (direct URL verified too)
env_key = "OPENCODE_API_KEY"              # value: <REDACTED>, never written
wire_api = "responses"
http_headers = { "x-opencode-session" = "codex-spike-static-0001" }   # redundant, see S11
request_max_retries = 2
stream_max_retries = 2

[shell_environment_policy]
inherit = "core"
ignore_default_excludes = false
filters = { "OPENCODE*" = "exclude" }

[mcp_servers.fixture]
command = "python3"
args = ["scripts/mcp_fixture.py"]
env = { MCP_LOG = "logs/mcp_calls.jsonl" }
tool_timeout_sec = 60

[projects."work"]
trust_level = "trusted"
```

The hooks variant (`home_hooks/config.toml`) is the same file plus inline `[[hooks.UserPromptSubmit]]`, `SessionStart`, `SessionEnd` (timeout 3), `PreToolUse` (matcher `^mcp__fixture__lookup$` → `scripts/hooks/log_hook.py deny_lookup`), `PostModelRequest`, and `Interrupt`, plus the `[hooks.state."<key>"] trusted_hash = "sha256:…"` entries written by `scripts/s7_trust.py`.
`model_catalog_json` was not used. Codex falls back to generic metadata (`warning: Model metadata for deepseek-v4.1-flash not found…`), sends 8 tools (`exec_command`, `write_stdin`, MCP resource tools, `request_user_input`, `view_image`, MCP namespace `mcp__fixture`), sends no `apply_patch` tool, and reports `modelContextWindow` = 121600 (95 % of 128000).

## Raw evidence snippets

```
# S1 proxy record (Authorization never logged)
{"path":"/v1/responses","session":"codex-spike-static-0001","model":"deepseek-v4.1-flash","stream":true,"include":["reasoning.encrypted_content"],
 "store":false,"status":200,"out_types":["message"],"usage":{"input_tokens":6112,"input_tokens_details":{"cached_tokens":3584},"output_tokens":5,...}}
# S2 last request of loop: reasoning round-tripped
22 200 1.3s in: {'message': 4, 'reasoning': 20, 'function_call': 20, 'function_call_output': 20} out: ['reasoning', 'message']
final: ENV=0 LABEL=ALPHA-7731 TOKENS=T00,T07,...,T15,T22   tokens match: True     (ENV=0 → OPENCODE* hidden from shell tool)
# Reasoning item as stored: raw CoT arrives as summary_text; encrypted_content=None
{'type':'reasoning','id':'rs_dce7…','summary':[{type:'summary_text',...}],'content':None,'encrypted_content':None}
# S5
5a turn status after interrupt: interrupted latency s: 0.01
resume: turns in history: 3 statuses: ['interrupted','interrupted','completed'] / after restart: completed CODE=KIWI-9031
# S6 concurrent duplicate clientUserMessageId=cumid-002 → same turn id, two userMessage items, 2 model calls
# S7
hook notes: sessionStart completed, userPromptSubmit completed, postModelRequest completed, preToolUse blocked, ...
final: LOOKUP=BLOCKED: lookup is blocked by spike policy hook. MAGIC=8128
post-archive hook notes: [('hook/started','sessionEnd'),('hook/completed','sessionEnd')]
# S8: model calls: proxy=21 tokenUsage events=21 per-call mismatches=0
# S10: default60_slow75 → failed 60002ms "timed out awaiting tools/call after 60s"; timeout5_slow10 → failed 5002ms
# S11 headers Codex sends: originator, session-id, thread-id, x-client-request-id, x-codex-beta-features, x-codex-turn-metadata, x-codex-window-id
# S4: thread/start with modelProvider≠process provider → error modelProviderMismatch ("This chat uses nohdr, but your current provider is opencode_go")
```

## Top risks

1. **The `/responses` endpoint is undocumented** for this model (`/models` lists chat-completions only), and Codex supports only `wire_api="responses"`. If OpenCode changes or removes it, the whole stack breaks; there is no chat-completions fallback in Codex. Pin the Codex version and add a canary turn.
2. **API key leaks into hook processes.** Hook commands inherit the full app-server env (`env_has_opencode=True` in every hook). `shell_environment_policy` covers only shell tools (verified `env | grep -c OPENCODE` → 0). MCP stdio servers did *not* get it (0 `OPENCODE*` vars in `/proc/<pid>/environ`). Mitigation: wrap hooks with `env -u OPENCODE_API_KEY`, or use the provider `auth.command` instead of the env var.
3. **No server-side idempotency (S6).** A retried `turn/start` creates a duplicate turn, or steers into the active turn. The daemon has to dedupe on `clientId` through `thread/read` before retrying.
4. **No model metadata.** Fallback metadata gives a warning on every turn, a guessed context window (must set `model_context_window`), no `apply_patch` tool, and a ~6.1k-token base prompt on every call. `reasoning_tokens` is always 0, so reasoning cost hides inside output tokens. A `model_catalog_json` entry is worth a follow-up spike; copying an OpenAI entry would bring `apply_patch_tool_type="freeform"` (custom tools), which may not work on this provider.
5. **Hook trust is silent.** Untrusted hooks just don't run, with no warning in exec output. Any edit to a hook changes its hash and silently disables it. The daemon must manage `hooks.state` trusted hashes (or pass the bypass flag on `codex exec`) and confirm through `hooks/list` → `trustStatus`.
6. **Truncation is invisible, and MCP timeouts don't cancel work server-side.** Tool output is cut for the model with no event, and timed-out MCP calls keep running in the server. Research tools need their own budgets and must handle cancellation.
7. **Process-level provider lock.** Every thread in one app-server must use the process's `model_provider`, or you get `modelProviderMismatch`. Model, effort, instructions, MCP servers, headers, and timeouts can vary per thread or turn. Turn-level overrides persist into later turns, so a bad model override sticks.
8. **Static session header is unnecessary and may hurt cache routing.** Codex already sends a per-thread `session-id`, which OpenCode accepts. Recommend dropping the static `x-opencode-session`, or setting it per thread. Precedence when both headers are present was not tested.
9. **Experimental surface.** The app-server is labeled `[experimental]` and its protocol is large and changing (v2 schema, `experimentalApi` opt-in). Regenerate the schema whenever the version changes.

## Untested or not fully tested

- **Long-run stability and rate limits under heavy load.** Max concurrency was only 6 and the total was ~160 calls (budget). No 429/5xx seen, so retry/backoff behavior on provider 5xx was not exercised.
- **Precedence when both `session-id` and `x-opencode-session` are sent** (all S1–S10 runs sent both). Cache hit rates were similar either way, but routing wasn't measured.
- **SessionEnd after the 30-minute idle unload** (only archive and process exit were tested). **The `Interrupt` hook** was configured but not exercised in S7.
- **PreCompact/PostCompact hooks** and `remote_compaction` (provider capability unknown, local compaction was used).
- **`model_catalog_json`** with a custom entry (not needed for PASS; see risk 4).
- **Whether `turn/steer` / queued submissions dedupe on `clientUserMessageId`** beyond what is described in S6.

## Files

- `scripts/appclient.py`: minimal JSON-RPC stdio client (logs every message to `logs/<name>.jsonl` with key redaction).
- `scripts/proxy.py`: logging reverse proxy (status, usage, input item types, header names; never logs Authorization).
- `scripts/mcp_fixture.py`: dependency-free stdio MCP server (`lookup`, `step`, `slow`).
- `scripts/s1_basic.py`, `s1b_direct.py`, `s2_tools.py`, `s2b_text_probe.py`, `s3_continuity.py`, `s4_perthread.py`, `s5_interrupt_resume.py`, `s6_idempotency.py`, `s7_hooks_list.py`, `s7_trust.py`, `s7_hooks.py`, `s8_usage_compare.py`, `s9_compaction.py`, `s9b_autocompact.py`, `s9c_truncation.py`, `s10_mcp_timeout.py`, `s11_session_stress.py`, `s11b_perthread_header.py`, `s11c_no_header.py`, `probe_session_header.py`, `hooks/log_hook.py`.
- `schema/`: generated app-server JSON schema. `logs/`: raw traces. `home/config.toml`: committed key-free config. Other `home*/` files: throwaway CODEX_HOME state.
