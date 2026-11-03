# P0.B 补齐验证（S2 / S3 / S4 / S11 / S12）

这些是一次性验证脚本，需要本地 codex-cli 和 OpenCode Go key，不进 CI；已测试版本 codex-cli 0.166.0（只是记录，不固定版本）。

入库路径：本目录即当时的 `/workspace/codex-spike/backfill/`。下文 `scripts/`、`logs/`、`run/` 相对本目录（`hosts/codex/evidence/phase0-backfill/`）。日志和脚本里仍保留运行时的 `/workspace/codex-spike/...` 绝对路径，没有改写。`home/`（CODEX_HOME）和 `work/` 没有入库。

日期：2026-11-03（Asia/Shanghai，UTC+8）· Box：Linux · **已测试版本：codex-cli 0.166.0**（仅记录，不作为版本锁定）· Provider：OpenCode Go `deepseek-v4.1-flash`，`/responses`
没有 clone 仓库，没有访问 GitHub。模型调用共 **14 次**（6 个 turn，proxy 记录 14 行，全部 HTTP 200）。

## 结论

| # | 结论 | 一句话证据 |
|---|---|---|
| **S2（硬门槛）** | **PASS**（web 子项 PARTIAL） | 每 thread `config` 关掉 shell/subagent 后，发给模型的工具列表里没有 `exec_command`/`multi_agent_v1`，模型尝试后回答 `UNAVAILABLE`；MCP stub 在 A/B 两个 thread 各收到不同的 `RESEARCH_TOKEN`（sha8 `ba1bfaec` / `3efc6d33`，与本地计算一致）；`web_search` 能按 thread 开或关，但 OpenCode Go 不执行 hosted web search |
| **S3（硬门槛）** | **PASS** | 带 `clientUserMessageId=bf-s3-57fd835c40ce` 的 turn 完成 → `SIGKILL` app-server → 新进程直接 `thread/read`（不 resume）和 `thread/turns/list` 都返回 `clientId=bf-s3-57fd835c40ce` |
| S4 | **PASS**（仅 Linux） | `default_permissions="ro_env"` 的 thread：`cat .env` → `Permission denied`，`echo x > …` → `Read-only file system`，shell 里的 `apply_patch` → `patch rejected: writing is blocked by read-only sandbox`，文件都没有被创建 |
| S11 | **PASS**（hook 泄漏已复现，`env -u` 能修） | backfill 全部文件（rollout、sqlite、日志、work、node shell 的 `env` 输出）key 命中 0；app-server stdout 在脱敏前统计 key 命中 0；hook：裸跑 6/6 带 key，`env -u OPENCODE_API_KEY` 包装后 0/6 |
| S12 | **PASS**（只针对本配置，限制见下） | 隔离 netns 下跑完全部 run：0 次 DNS 查询，0 次 OpenAI/ChatGPT 主机访问，14 个 TCP SYN 全部发往 provider bridge `127.0.0.1:18080`（正好对应 14 次模型调用） |

**S2、S3 两个硬门槛都通过，计划可以继续。**

## 运行方式

- 独立的 `CODEX_HOME`：当时的 `backfill/home/config.toml`（未入库；基于 spike 配置；基线是 `multi_agent=true`、`shell_tool=true`、`web_search="disabled"`；加了 `[mcp_servers.tokstub]`、`[permissions.ro_env]` 和两个 UserPromptSubmit 探针 hook）。
- 所有 app-server 运行都放在 netns `cxbf` 里（`scripts/run_netns.sh`）：只有 `lo`，路由是 `local default dev lo`，所以任何 IPv4 目标都落到本地；`resolv.conf` 的 `10.0.0.2` 绑在 lo 上，由观测器回答。到 provider 的路径：Codex → `127.0.0.1:18080`（netns 内）→ unix socket（`scripts/bridge.py`）→ 宿主机 `hosts/codex/spike/scripts/proxy.py`（spike 的日志代理，端口 18091）→ `opencode.ai/zen/go/v1`。Codex 以 uid box（`setpriv`）运行。
- 顺序：`bf_trust.py`（hooks/list → 写 trusted_hash，0 次模型调用）→ 观测器正向对照 → `bf_main.py`（S2/S4/S3 第一阶段，最后 SIGKILL）→ `bf_s3_after.py` → `bf_web_retry.py` → `s4_sandbox_cli.sh` → `s12_analyze.py` → `s11_scan.py`。
- 汇总输出：`logs/bf_main.summary.txt`。

## S2：每 thread config（硬门槛）

`thread/start.config`（`scripts/bf_main.py`）：
- A（类 coordinator）：`features.shell_tool=false`、`features.unified_exec=false`、`features.multi_agent=false`、`web_search="disabled"`、`mcp_servers.tokstub.env.RESEARCH_TOKEN="test-token-thread-A"`
- B（类 node）：`features.multi_agent=true`、shell 用基线（开）、`RESEARCH_TOKEN="test-token-thread-B"`
- W：`web_search="live"`

同一个 app-server 进程里的证据（工具列表来自 `logs/proxy.jsonl`，即真正发给模型的请求）：
```
S2-A tools sent: ['list_mcp_resource_templates','list_mcp_resources','mcp__tokstub','read_mcp_resource','request_user_input','view_image']
     final: "STEP 1: UNAVAILABLE\nSTEP 2: UNAVAILABLE\nSTEP 3: UNAVAILABLE\nSTEP 4: DONE token present, sha ba1bfaec"
S2-B tools sent: ['exec_command',...,'mcp__tokstub','multi_agent_v1',...,'write_stdin']   cmd "env | grep -c OPENCODE" -> "0"
S2-W tools sent: [..., 'web_search', ...]   (A/B 里没有 web_search)
```
MCP stub（`logs/tokstub.jsonl`，只记录是否存在和 sha256 前 8 位，不记录值）：
```
start pid 944696 token_present true sha8 ba1bfaec opencode_vars 0   (thread A)
start pid 944718 token_present true sha8 3efc6d33 opencode_vars 0   (thread B)
start pid 944742 token_present false                                (thread W，未传 token)
```
每个 thread 各起一个 MCP 进程，token 互不相同；MCP 进程里也没有任何 `OPENCODE*` 变量。

**web search（PARTIAL）**：Codex 这一侧开关有效（只有 `live` 的 thread 请求里有 `web_search` 工具）。但两次 turn（W 和明确要求调用的 W2，`scripts/bf_web_retry.py`）里，provider 都只返回 `['reasoning','message']`，模型回答 `NO_WEB_TOOL`，没有 `webSearch` item。所以 **OpenCode Go 不执行 hosted `web_search`**（多半是没有转给模型）。按 PLAN 的预案：node 改用 shell 网络或 relay 的 fetch 工具。
**subagent**：关掉时证据是“工具不存在 + 模型回答不可用”；开着时只确认了 `multi_agent_v1` 工具存在，**没有真正 spawn 子 agent**（为了节省调用次数）。

## S3：clientUserMessageId 跨重启（硬门槛）

```
S3-C turn (clientUserMessageId=bf-s3-57fd835c40ce) status=completed final "S3-ACK"
before kill: thread/read clientIds ['bf-s3-57fd835c40ce']
app-server pid 944644 SIGKILLed, returncode -9
-- 新进程 pid 945735 --
thread/read userMessages: [('01a19f80-d693-…','completed','bf-s3-57fd835c40ce',['Reply with exactly: S3-ACK'])]
S3 MATCH (thread/read): True
thread/turns/list clientIds: ['bf-s3-57fd835c40ce']
```
用的是 SIGKILL（不是正常退出），并且在 turn 完成之后才 kill。**turn 进行中被 kill 的情况没有测**。

## S4：只读 profile + `.env` deny

Profile（`home/config.toml`）：`[permissions.ro_env] extends=":read-only"`，`":workspace_roots"` 下面 `"."="read"`、`"**/*.env"="deny"`、`"**/.env"="deny"`、`glob_scan_max_depth=4`。thread：`config.default_permissions="ro_env"`、`approvalPolicy="never"`，cwd=`work/s4`（里面有假 `.env` canary）。
rollout 里的工具结果（`logs/s4_rollout_toolcalls.txt`）：
```
cat .env                 -> Process exited with code 1  cat: .env: Permission denied
echo x > shell_write.txt -> exited 1  /bin/bash: line 1: shell_write.txt: Read-only file system
apply_patch <<'EOF' ...  -> patch rejected: writing is blocked by read-only sandbox; rejected by user approval settings
cat readme.txt           -> exited 0  hello
fs after: shell_write.txt exists=False patched.txt exists=False
```
stderr：`recorded sandbox violation: … reason=permission_denied` / `reason=read_only_file_system`。
不调模型的复核（`scripts/s4_sandbox_cli.sh` → `logs/s4_sandbox_cli.txt`）：`codex sandbox -c default_permissions="ro_env"` 下 `cat .env` exit 1；对照组只用 `sandbox_mode="read-only"`（没有 deny 规则）时 **能读到 `.env`**，所以 deny 必须写在 permissions profile 里。
备注：
- 没有 model catalog，所以没有独立的 `apply_patch` 工具。patch 是通过 shell 里的 `apply_patch` 被拦截后拒绝的。
- **被沙箱拒绝的 3 条命令没有发出 `commandExecution` item 通知**，只有成功的 `cat readme.txt` 发了。拒绝只能从 rollout 和 stderr 看到。daemon 不能只靠 item 通知来统计越权。
- 只在 Linux 上验证，macOS 未验证。基线 `sandbox_mode="danger-full-access"` 被 thread 级 `default_permissions` 覆盖了。

## S11：key 泄漏扫描

方法：`scripts/s11_scan.py` 在内存里把每个文件的字节和 `$OPENCODE_API_KEY`（原文 + base64）比较，只输出计数和路径。另外，`bf_client.py` 在写脱敏日志之前统计 app-server stdout 里的 key 命中（phase1 是 0，phase2 是 0，web_retry 是 0）；所有日志里 `<REDACTED>` 出现 0 次（只有脚本源码里有 2 次），说明脱敏从来没被触发过。
```
backfill/  logs 17 files 0 | sessions(rollout) 6 0 | sqlite(含 -wal) 16 0 | shell_snapshots 3 0 | work 2 0 | run 1 0 | scripts 24 0 | other 42 0
spike/ home, home_hooks, home_hooks_untrusted, home_nohdr, logs, work: 全部 0（logs/s11_scan_spike_dirs.txt）
```
node shell 的 `env` 输出只有 23 个变量名（`CODEX_*`、`PATH`、`HOME` 等），`env | grep -c OPENCODE` → 0。
Hook（`logs/hook_probe.jsonl`，6 个 turn，每个 turn 同时跑两个 UserPromptSubmit hook）：`raw` 6/6 `OPENCODE_API_KEY_present=true`；`env -u OPENCODE_API_KEY …` 包装后 6/6 `false`（`opencode_var_count=0`）。说明 hook 继承 key 的泄漏是真的，用 `env -u` 包装能去掉。
没有覆盖：`.research/` 和 ledger（这些还不存在）；hook 自己的 stdout/日志（这里探针只写布尔值）。

## S12：没有 OpenAI 流量

方法：Codex 跑在隔离 netns 里，`scripts/netns_sniffer.py` 以 root 身份在该 netns 里运行，做三件事：(1) 用 AF_PACKET 在 lo 上记录**所有** TCP SYN 和 UDP 包（netns 里只有 lo，任何目标地址都经过它）；(2) 在 `10.0.0.2:53` 记录所有 DNS 查询；(3) 在 :80/:443 记录 Host 和 TLS SNI。
正向对照（运行前，`logs/s12_calibration.json` 之前的行）：curl `api.openai.com`、`chatgpt.com`、`1.2.3.4:8443`，以及 UDP `9.9.9.9:5555`，全部被记录到（DNS 名、SNI/Host、SYN、UDP）。
运行窗口（`logs/s12_summary.txt`）：
```
('sniff-tcp-syn','127.0.0.1:18080') 14     # = proxy.jsonl 的 14 条请求
OpenAI/ChatGPT host hits: 0   其他目标的连接尝试: 0   DNS 查询: 0
```
限制（照实写）：
- 只覆盖这份配置：`plugins/apps/remote_plugin=false`、`analytics/feedback/otel` 关、`check_for_update_on_startup=false`。spike 记录过开着 `plugins` 时会访问 chatgpt.com，默认配置下**会**有 OpenAI 流量。
- netns 里没有外网，所以“连接成功后才会触发”的后续流量看不到。不过任何连接尝试（DNS 或 SYN）都会被记录。
- 宿主机上的 proxy 只连 `opencode.ai`（我们自己的代码，不在观测范围内）。
- 没有 strace/tcpdump/iptables（apt 不可用）。unix socket 和进程间通信不在观测范围内。
- 没有测 `codex login`、更新检查、remote compaction 这些路径。node 开网以后自己产生的流量本来就不计入。

## 未验证

真正 spawn 子 agent；hosted web search 的实际执行（provider 不支持）；node 通过 shell 联网；turn 进行中被 kill 后的 S3；macOS 的 S4；hook 3 秒时限；默认配置（plugins 开）下的 S12。

## P1.4 能力探测清单（S2/S3 依赖的 app-server 能力）

`thread/start.config` 里的点路径覆盖：`features.shell_tool`、`features.unified_exec`、`features.multi_agent`、`web_search`、`mcp_servers.<name>.env`、`default_permissions`；`[permissions.<name>]` profile，包括 `extends` 和 `:workspace_roots` glob deny；`turn/start.clientUserMessageId` → `userMessage.clientId`；跨进程的 `thread/read includeTurns` 和 `thread/turns/list`；`hooks/list` 的 `currentHash`/`trustStatus`。

## 文件

`scripts/`：`bf_client.py`、`bf_trust.py`、`bf_main.py`、`bf_s3_after.py`、`bf_web_retry.py`、`tok_stub_mcp.py`、`hook_probe.py`、`bridge.py`、`netns_sniffer.py`、`run_netns.sh`、`s4_sandbox_cli.sh`、`s11_scan.py`、`s12_analyze.py`
`logs/`：`bf_main.summary.txt`、`bf_main_p1.jsonl`、`bf_s3_p2.jsonl`、`bf_web_retry.jsonl`、`trust.jsonl`（JSON-RPC 全量记录，经过脱敏）、`*.stderr.raw`（app-server stderr，已扫描，0 命中）、`proxy.jsonl`、`tokstub.jsonl`、`hook_probe.jsonl`、`s4_sandbox_cli.txt`、`s4_rollout_toolcalls.txt`、`s11_scan_backfill.txt`、`s11_scan_spike_dirs.txt`、`s12_netns.jsonl`、`s12_calibration.json`、`s12_summary.txt`
`run/s3_state.json`：S3 的 thread id 和 `clientUserMessageId`（`scripts/bf_main.py` 写入，`scripts/bf_s3_after.py` 读取）。
`home/`（CODEX_HOME：rollout、sqlite）和 `work/`（各 thread 的 cwd，`work/s4/.env` 是假 canary）没有入库。

## 收尾扫描

清理掉 netns 和后台进程之后，对整个 `backfill/` 重新跑了 `s11_scan.py`（`logs/s11_scan_backfill_final.txt`）：121 个文件，key 原文命中 0，base64 命中 0。`<REDACTED>` 只出现在 REPORT.md 和脚本源码里。
