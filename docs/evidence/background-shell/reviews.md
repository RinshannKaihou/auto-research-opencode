# PR #13 首轮 Review 原文

## Review 1

来源：https://github.com/RinshannKaihou/auto-research-opencode/pull/13#pullrequestreview-5728997873

# PR #13 审阅：fix: prevent background research shells from stalling nodes

- 仓库：RinshannKaihou/auto-research-opencode
- PR：#13，状态 open，不是 draft；GitHub 显示 mergeable = clean
- Head：`d21ccd2de2d7ca8d0c01b996ddaf4b38b5676eed`（分支 `codex/fix-background-research-stall`）
- Base：`main` @ `b8cac7694d0a53b868d980dbf040a85fc6c86d72`，即 PR #12 的 merge commit
- 提交：只有 1 个，`d21ccd2 fix: prevent background research shells from stalling nodes`，2026-11-01 23:34（UTC+8），父提交就是 b8cac76
- 规模：15 个文件，+881 / −11
  - 产品代码约 400 行：
    - Python 后端约 +220，集中在 `runtime_budget.py`（+196）；
    - 插件约 +185，集中在 `engine.ts`（+125）和 `index.ts`（+48）。
  - 测试约 280 行：新增 `tests/test_background_shell.py`（7 个用例），`plugin/test/plugin.test.ts` 新增 6 个用例。
  - 脚本约 190 行：新增 `scripts/probe_background_wait.py`；`probe_packed_release.py` 和 `probe_research_run.py` 增加 `--background-node`。
  - 没有 schema 变更：新状态存放在已有的 `workflow_intents` 表里，用新的 `kind='native_shell'` 区分。
- Checks：0 个 check run，combined status 里没有任何 status，没有 CI 结果。
- 已有审阅：没有 review，没有会话评论，也没有行内评论。
- 测试数量：PR 描述写的是 Python 788 passed、Bun 191 passed、typecheck 通过，`probe_packed_release.py --background-node` 通过。这些都是**作者自述，未验证**。
  - 与 #11/#12 不同，这次仓库里没有附原始测试输出，也没有附宿主 probe 的汇总或 trace。
- 审阅方式：只通过 cursor-github 接口只读获取信息，包括：
  - PR 元数据、提交和完整文件 diff；
  - head 上的全文：`plugin/src/engine.ts`、`plugin/index.ts`、`plugin/src/research.ts`、`src/auto_research/runtime_budget.py`、`native_store.py`、`workflow_store.py`；
  - `schema11/12/13.py`，用于确认表和索引。

  没有 clone，没有运行代码，没有在 GitHub 上发帖。
- 行号说明：全部按 head（d21ccd2）的行号引用。凡是从读代码推出、没有经过实际运行验证的判断，都标「推断」。

## 与 #11、#12 及其他 PR 的关系

- **#12 已经合入，不再是 open 状态**。
  - main 的 tip 是 `b8cac76 Merge pull request #12 from RinshannKaihou/codex/e3-multiroute`，两个父提交是 `31fd90d`（#11 的 merge）和 `41c244c`（#12 的最终 head，就是我们第二轮审阅的那个 head）。
  - GitHub 记录的 merged_at 是 2026-11-01 19:18（UTC+8）。
  - #13 直接基于这个 merge commit，所以**和 #12 没有冲突，也不会有合并顺序问题**。目前仓库里唯一 open 的 PR 是 #13。
- **与 #12 的交汇点**：
  - `recovery_confirm_exit`（`runtime_budget.py:573-605`）。#12 在这里加了 `_release_task_resources`，#13 在 :597-599 把该任务的 `native_shell` 意图标为 `verified_exit`。两者互不干扰，方向一致：用户核实外部退出后，同时释放资源租约和后台 shell 记录。
  - 节点因等待后台 shell 而保持 `running` 时，#12 的资源租约会继续 held。这符合「外部进程仍在运行」的语义（推断）。
- **与 #11 的交汇点**：
  - evidence-v1 的执行观测（`index.ts:366-376`）对后台 shell 仍在**启动时**记录 `runtime_tool_observed`，不是完成时，见 Nit 7。
  - 节点结束时，`node_finish_request` 和 `node_finish_commit` 增加了「没有未结束或未核实的后台 shell」这一前置条件（`runtime_budget.py:918-919, 969-970`）。
- **与 #9/#10 的控制语义**：本 PR 改动了暂停期间的收尾授权路径（drain）和 `/research resume`，这是 B1、B2 的来源。

## 结论：需修改

- **方向是对的，设计总体审慎**：
  - 用持久意图跟踪原生 shell 的 begin → result → terminal notice；
  - 等待期间冻结空转计数；
  - 节点不能在后台执行未结束时结项；
  - 宿主生成的完成通知要经过预算预留，才能成为受管续轮；
  - 暂停状态下的完成通知不会让节点自行恢复。

  这些都和 #9/#10 以来「宁可停下等核实，也不假装成功」的原则一致。
- **两处阻塞问题**：
  1. **B1**：后台跟踪的入口没有传执行令牌，导致**暂停期间正在收尾的那一轮无法再调用任何 shell**。这直接违背 `/research pause` 承诺的「正在进行的这一轮会做完」。
  2. **B2**：前台 shell 出错或被中断时，会留下一条 `unverified`/`starting` 意图，但任务本身不会被标为 unverified。会话因此卡在一个**没有出口的状态**：resume 静默跳过，`/research recover task` 拒绝，节点也不能结项，只有重载插件才能脱困（推断）。
- **另有 6 条 Should-fix**，主要是：
  - 通知竞态与分类：inline 判定时机、未匹配或迟到的通知；
  - 等待没有上界；
  - 热重载时过于保守；
  - resume 的反馈与空转清零范围；
  - 每次 shell 调用都写入意图行，带来的存储和查询成本。

## Blocking

### B1. `runtime_shell_begin` 不带执行令牌，暂停期间的收尾轮不能再调用 shell（代码路径已核实，宿主表现为推断）

- **现象**：
  - `plugin/index.ts:333-336` 对每一次节点 shell 调用 `research.call('runtime_shell_begin', sessionID, { call_id })`，**没有传 `execution_token`**。
  - 后端 `runtime_budget.py:1137` 调用 `require_model_drain(db, host, session, execution_token=None)`。
  - `require_model_drain`（`runtime_budget.py:47-67`）在 `run.state == 'paused'` 时，只有带 `execution_token`，并且它对应一个 open 的 `execution_start` 意图，才会放行（:60-66）；否则抛出 `ResearchControlError`。
  - 这个调用来自插件宿主，不是模型调用，所以 `native_store.py:456-459` 的 `CURRENT_MODEL_CALL` 补充校验也带不进令牌。
- **对照**：
  - 同一个 before hook 前面的 `engine.nodeTurnAllowed`（`engine.ts:255-259`）是**带着** `this.turns.get(sessionID)` 去做 `runtime_drain_check` 的，所以暂停后正在收尾的轮次能通过 :327-332 的检查。
  - 但随后 `runtime_shell_begin` 会失败，before hook 抛出一个普通 Error（不是带 `research_control_paused` metadata 的 ToolError），shell 因此被拒绝。
  - 在 base 上，这一轮是可以继续用 shell 收尾的。
- **后果**：
  - `/research pause` 会立即告诉用户「正在进行的这一轮会做完」（`engine.ts:1150`），但节点在这一轮里任何需要 shell 的收尾（保存结果、停止实验、写日志）都会失败；
  - 模型拿到的是原始错误文本，可能据此给出错误的结论或失败记录。
- **测试缺口**：
  - 新增测试都在 running 状态下调用 `runtime_shell_begin`（`tests/test_background_shell.py:8-15`），或者在 shell 开始**之后**才暂停（`plugin/test/plugin.test.ts:1200-1234`）；
  - 没有「暂停后，收尾轮再开一个 shell」的用例。
- **建议**：
  - Engine 增加 `executionToken(sessionID)`，返回 `this.turns.get(sessionID)`；`index.ts:334` 把它作为 `execution_token` 传入，`service.py:1366-1367` 已经支持这个字段。
  - begin 失败时，转换成带 `research_control_paused` metadata 的 ToolError，与 :329-331 保持一致。
  - 补一个用例：节点轮次进行中执行 `/research pause`，之后 before hook 调 shell 应当成功，并生成 `starting` 意图。

### B2. 前台 shell 出错或中断后，会话会卡在一个没有出口的状态（推断）

- **成因**：
  - `index.ts:353` 只要 `request.status !== 'completed'` 就记为 `unverified`，不区分前台还是后台，也不区分是权限被拒、参数错误、被中断还是超时。
  - 如果 after hook 根本没有触发，或者 `runtime_shell_result` 的 RPC 失败（:356-359 只记一条 coverage_gap），意图会一直停在 `starting`。
  - 这两种状态都会被 `pending_shells`（`runtime_budget.py:20-24`）计入。
- **能脱困的唯一路径**：只有 `afterTurn` 走到 `engine.ts:412-417`，才会调用 `runtime_shell_unverified`，把**任务**改成 unverified（`runtime_budget.py:1252-1255`），用户也才能用 `/research recover task` 脱困。但下面几种常见情况都在这之前就返回了：
  - 用户按 Esc 中断这一轮：outcome 是 `interrupted`，`engine.ts:357-361` 直接把会话暂停为 `native_stop` 并返回。这正是「前台 shell 被中断」最常见的场景。
  - 这一轮失败：`engine.ts:392-399` 重试或进入 fault。
  - 项目已暂停：:391 返回。
  - 会话已经有 pause_reason：:411 返回。
- **此后的状态**：
  - `/research resume` 会调用 `runtime_resume_session`，它发现非 running 的 shell 后返回 `{'resumed': False, 'reason': 'execution_unverified'}`（`runtime_budget.py:1284-1286`）。引擎在 `engine.ts:1185` 遇到这个结果直接 `continue`，**不告诉用户原因**，返回的消息只是「已恢复自主研究（N 个会话继续）」。
  - `/research recover task` 走 `recovery_confirm_exit`，要求 `task.state == 'unverified'`（`runtime_budget.py:580-581`），而任务仍是 `running`，所以会被拒绝。
  - 节点结项被 `runtime_budget.py:918-919` 拒绝。
  - 只有重载插件后 `recoverWarm` 走到 `engine.ts:946-950`，才会把任务标为 unverified。
- **推断的边界**：OpenCode 2.2.5 对被中断或被拒绝的 shell 是否会触发 `execute.after`、status 是什么，我没有运行验证。但只要出现 `status !== 'completed'`，或者 after hook 缺失，上面的链条就成立。
- **建议**（任选其一或组合）：
  1. **后端**：`runtime_resume_session` 遇到非 running 的 shell 时，在同一事务里执行与 `runtime_shell_unverified` 相同的处理：意图改为 unverified，任务改为 unverified，并发出 notice。返回值里带上 `task_id`，让 `/research recover task` 立即可用。
  2. **引擎**：`engine.ts:1185` 不要静默 `continue`，要在返回消息里列出因「后台执行待核实」而没能恢复的会话，以及对应的恢复命令。
  3. **插件**：`index.ts:353` 对前台 shell（结果里没有 `shellID`、也不是 `status: 'running'`）的出错，记为 `finished`/`failed`，而不是 `unverified`。宿主在前台 shell 中断时会终止子进程，这一点需要作者用 probe 确认后再采纳；如果不能确认，至少要保证有第 1、2 条。
  4. 在 `afterTurn` 的 interrupted、failed、paused 分支里也检查 `runtime_shell_status`，对非 running 的意图调用 `runtime_shell_unverified`。
  5. 补用例：前台 shell 的 after hook 以 `status:'error'` 结束 → 这一轮被中断 → `/research resume` 应当给出明确提示，并且 `/research recover task` 可用。

## Should-fix

### S1. 用「入队时是否 busy」决定 inline，入队到投递之间轮次结束时，会出现未预约、未追踪的唤醒轮（推断）

- **现象**：
  - `engine.ts:269` 在 `session.inbox.enqueued` 时就用 `this.busy.has(sessionID)` 判定 inline；inline 的情况下不做 `runtime_reserve`（:275）。
  - 到 `session.inbox.delivered` 时，`engine.ts:242-245` 看到 `shell.inline` 就直接返回，不再调用 `started(...)`。
  - 如果完成通知在节点轮次的最后一步入队，而这一轮在投递前就结束了，宿主会用这条通知开启一个**新**的轮次。插件仍把它当作 inline：既不预约 turn 预算，也不登记执行；`shellWake` 被设为 `matched: true`。
- **后果**：
  - 这一轮绕过了 `max_session_turns`；
  - 它的 `ended` 事件会配上旧的或缺失的执行令牌，`runtime_execution_end` 的核对结果难以预料（推断）。
- **测试缺口**：`plugin/test/plugin.test.ts:1236-1255` 只覆盖了「入队和投递时都是 busy」的情况。
- **建议**：
  - 在 `inboxDelivered` 时重新判断 busy：如果 `shell.inline` 但此时已经不 busy，就按非 inline 处理，先补做 `runtime_reserve`/`runtime_submitted` 再登记执行；预约失败则按 `matched:false` 处理。
  - 补一个「入队时 busy、投递时 idle」的用例。

### S2. 未匹配或迟到的通知，处理得过重或会抛错（推断）

- **未匹配的通知按故障处理**：
  - 通知与任何意图都对不上，或者预约过程中出现任何异常（`engine.ts:286-288` 的 catch-all，包括暂时性的存储错误），都会落到 `engine.ts:364-372`：只要项目在运行、会话没有暂停，就把**任务**标为 unverified，并以 fault 暂停。
  - 可能触发的情况：上一代或上一次 attempt 遗留的后台 shell（`runtime_shell_notice` 按 generation 过滤，见 `runtime_budget.py:1204-1207`）；`pendingShellResults` 没有覆盖到的竞态。
  - 但这些情况下，shell 本身已经结束，通知也已经持久化，真正「未核实」的只是这一次唤醒的授权。
  - 结果是节点任务只能由用户「核实退出」，随后被记为 failed（`runtime_budget.py:593-594`），已经完成的工作会丢失。
- **迟到的通知会抛错**：
  - `runtime_shell_notice` 对 `verified_exit` 等非等待状态抛出 `ConflictError('Shell is not awaiting a terminal notice')`（`runtime_budget.py:1216-1217`）。
  - 典型场景：用户已经执行了 `recover task`，之后后台进程才真正结束。
  - 这个异常会让事件处理器重试 3 次后，把整个会话放进 `failedEvents`（`index.ts:97-104`）；此后该会话的事件都被搁置，`beforeTool` 也会拒绝非读工具（:327-328）。
- **建议**：
  - `runtime_shell_notice` 对已经 `verified_exit` 的行，或者匹配不到的行，返回 `{'matched': False, 'ignored': True}`，并写一条事件，不要抛错。
  - 引擎区分「通知未匹配」和「预约被拒或出错」：
    - 前者：项目在运行时，可以忽略这次唤醒，不改任务状态；
    - 后者：如果是暂时性错误，按 `fault` 暂停，但不要把任务标为 unverified，因为 shell 已有终态见证。
  - 不要用 catch-all 吞掉所有异常。

### S3. 后台等待没有上界，事件缺口后不会重新扫描（推断）

- **现象**：
  - 节点带着 running 状态的后台 shell 结束这一轮后，`engine.ts:419-422` 只刷新看板然后返回。
  - 空转计数被冻结（`runtime_budget.py:1475`），所以永远不会进入 `no_progress`。
  - 继续推进完全依赖宿主的 `session.inbox.enqueued` 事件。
- **问题**：
  - 事件订阅出现缺口（`markGap`，`index.ts:75, 85, 89, 95, 104, 126-127`），或者这个事件在 `failedEvents` 里被搁置时，节点会**无限期**显示「等待后台执行」。
  - 只有重载插件时，`recoverWarm` 才会去 transcript 里补找通知（`engine.ts:926-953`）。
  - 未配置 `max_wall_seconds` 时，没有任何东西能把它拉回来。
- **建议**：
  - 事件缺口恢复，或者看板刷新时，对处于 `waiting_external` 的会话复用 `recoverWarm` 里的 transcript 扫描逻辑，按 shellID 补登通知。
  - 看板显示等待了多长时间。
  - 可以选配一个最长等待时间，超时后转为 unverified，并通知用户。

### S4. 热重载会把仍在运行的后台 shell 一律判为未核实，任务最终只能记为 failed（推断，属设计取舍）

- **现象**：
  - `engine.ts:946-950` 在 transcript 里找不到终态通知时，直接调用 `runtime_shell_unverified`，任务因此变为 unverified。
  - 但热重载时 OpenCode 宿主进程没有变，shell 进程和宿主的 shell 注册都还在；之后终态通知照样会入队。
  - 而此时任务已经是 unverified，`runtime_reserve` 会抛出 `ResearchExecutionUnverified`（`runtime_budget.py:1303-1308`）。用户只能走 `recover task`，任务被记为 failed（:593-594）。
- **后果**：一次热重载（例如更新插件）就会让所有正在跑后台实验的节点失败。
- **作者的态度与仍可改进之处**：作者在 README（`plugin/README.md:259`）里写明了这是有意的保守选择，测试 `plugin/test/plugin.test.ts:1292-1316` 也把它固定了下来。不过，「transcript 里没有终态通知」本身就说明 shell 还没结束（宿主会在退出时入队通知）。
- **建议**：
  - 能确认是同一个宿主进程时（`lifecycle.binding`/pid 未变），让意图保持 `running`，继续等待；
  - 只在冷启动，或者确认宿主进程已经变化时，才判为 unverified。
  - 如果作者坚持保守，请在 `/research reload`、插件更新的文档中明确警告「会令运行中的后台节点失败」。

### S5. `/research resume` 的空转清零范围和反馈，与 README 的说法不一致（已核实代码）

- **清零范围**：
  - README 写的是「`/research resume` 会清除该会话的空转计数」（`plugin/README.md:259`），PR 描述也写「显式恢复时原子清零持久空转计数」。
  - 但只有带 `pause_reason` 的会话才走 `runtime_resume_session`（`engine.ts:1180-1186`），也只有它会清零（`runtime_budget.py:1289-1290`）。
  - 通过 `/research pause` 暂停的是项目，节点会话的 `pause_reason` 仍为 NULL，恢复时**不会清零**，暂停前积累的空转次数会带到恢复之后。
- **revision 冲突会中断循环**：
  - `runtime_resume_session` 用的是全局 `control_revision`（:1277-1278）；任何会话的 `pause_reason` 变化都会让它加一（schema12 的触发器）。
  - 恢复循环中途如果有其他会话并发改变暂停状态，这里会抛出 ConflictError，整个 resume 中断，前面的会话已经恢复，后面的没有。
- **建议**：
  - 对 `pause_reason` 为空的会话，也调用一次只清零、不改暂停状态的 RPC；或者让 `runtime_resume_session` 在 `not_paused` 时同样清零，并返回 `resumed:false`。
  - ConflictError 时用新的 revision 重试一次。
  - 与 B2 的建议 2 一起，在返回消息里列出没能恢复的会话及原因。

### S6. 每次节点 shell 调用都会持久化一条意图，查询依赖 `json_extract` 全表扫描（推断）

- **写入开销**：
  - 每次节点 shell 调用，不论前台还是后台，都会：
    - 在 begin 时插入一条 `workflow_intents`（`runtime_budget.py:1157-1158`）；
    - 在 result 时更新这条意图，并写一条事件（:1185-1188）；
    - 再加上两条 `requests` 回放记录。
  - 前台 shell 结束后，这条意图永久保留为 `finished`。
- **查询开销**：
  - `workflow_intents` 只有主键（`workflow_store.py:69-72`）。我在已读的 schema11–13 中也没有看到 `(kind, session_id)` 索引；schema5–10 和 14–16 没有逐一核对。
  - `pending_shells`（`runtime_budget.py:20-24`）每轮结束、每次结项、每次看板读取都会执行（`workbench_read.py:167-172`、`research.ts:474-475`）。
  - `runtime_shell_result` 的 shellID 去重查询（`runtime_budget.py:1180-1183`）会扫描该宿主所有的 `native_shell` 行，并且没有按 session 过滤。
  - 长时间运行、shell 调用频繁的节点会让这些查询线性变慢。
- **建议**：
  - 前台 shell 的 result 为 `finished` 时，可以直接删除这条意图（事件里已有记录）；或者把 `native_shell` 拆到独立的表，加上 `(session_id, state)` 和 `(host, shell_id)` 索引（需要 schema 17）。
  - 去重查询至少要加 `session_id=?` 条件。

## Nits

1. **transcript 读取失败时一律按「后台唤醒」处理**：`index.ts:662-667` 在读取失败或 1 秒超时时（`!transcript`），会把这一轮当作后台唤醒，移除**全部**工具（包括只读工具和 `research_query`），并提示「A background completion arrived」。用户在暂停期间向节点提问时，如果恰好读取超时，会得到误导性的提示。此外，每次未授权的请求都会读取完整的会话 context。建议读取失败时只保留读工具，提示语改为中性表述，并只读取最近 N 条消息。
2. **暂停通知的去重键可能误合并**：`engine.ts:472` 的键是 `paused:${reason}:${turn ?? 'unknown'}`。没有 turn 时，同一原因的多次暂停共用 `unknown`，`INSERT OR IGNORE` 之后第二次暂停不会再通知。建议在没有 turn 时用 `control_revision` 或时间戳代替。
3. **可恢复暂停原因的集合有两处来源**：`runtime_budget.py:13` 的 `RESUMABLE_SESSION_PAUSES` 与 `engine.ts:74` 的 `RESUMABLE` 是两份手抄的列表，以后改一处很容易漏另一处。建议由后端在 status 里返回可恢复标记，或者加一条测试断言两者相同。
4. **工具名判断不一致**：只有 `request.tool === 'shell'` 会被跟踪（`index.ts:333, 349`），而 :367 的执行观测用的是 `['bash','shell','python','terminal']`。如果宿主或其他插件提供能后台运行的同类工具，它们不会被跟踪。建议共用一个常量，并在 README 写明只跟踪 `shell`。
5. **暂停期间的宿主唤醒轮不计预算**：项目暂停时，宿主仍会用完成通知自动唤醒模型。插件只能移除工具并要求模型停止（`index.ts:668-673`），这一轮不会被计入 turn 预算。建议在 README 中说明。
6. **shellNotice 的入参校验**：`engine.ts:268` 的 `typeof messageID !== 'string'` 永远为假，因为调用方 `index.ts:771` 已经用 `String(...)` 转换过。inboxID 缺失时传入的是空串，后端会报 ValidationError，导致事件进入失败重试。建议在插件侧直接判断 `messageID.startsWith('msg_')`。
7. **evidence-v1 的执行观测记在后台启动时**：`index.ts:366-376` 在后台 shell 启动并返回 running 时，就记录了 `runtime_tool_observed`。这样「已执行」的观测早于实际结果。建议后台 shell 在收到终态通知时再记录，或者在观测里标明 `background:true`。
8. **`runtime_shell_unverified` 的通知可能挂到旧任务上**：它通过 `notify_in_transaction` 发通知（`runtime_budget.py:1258-1259`），而后者按 `session_id` 取「任意一条」任务（`workflow_store.py:268-271`，没有状态过滤，也没有 ORDER BY，这是已有问题），会话跑过多个任务时可能挂到旧任务上。另外，通知类型 `unknown` 会以「结束（状态未核实）」的文案唤醒主协调（`engine.ts:69`），但任务其实并没有结束。
9. **task_id 为空时没有恢复入口**：`runtime_shell_begin` 找不到活动任务时，记录的 `task_id` 为 None（`runtime_budget.py:1151-1156`）。这样的意图一旦变成 unverified，`recovery_confirm_exit` 按 task_id 清理时清不到（:597-599），本代内也没有其他入口能清理。建议 begin 时要求必须有活动任务，或者提供按 `call_id` 核实的入口。
10. **缺少 CHANGELOG 和证据**：这次没有更新 CHANGELOG，也没有像 #11/#12 那样附 probe 的汇总和原始测试输出。建议补上 `--background-node` 的宿主汇总，以及 pytest、bun 的输出。

## 做得好的地方

- **状态机清晰**：`starting → running/finished/unverified → completed/failed/verified_exit`，每一步都有幂等的 operation id；shellID 和 session 绑定，不同会话之间不能互相结清（`tests/test_background_shell.py:44-52`）。
- **快速完成的竞态有专门处理和测试**：`pendingShellResults` 让完成通知等待 shellID 先落库（`index.ts:276-278, 360-362, 767-771`），用例 `plugin/test/plugin.test.ts:1257-1290` 用 spy 人为卡住 `runtime_shell_result` 来验证。
- **暂停语义没有被后台通知绕开**：暂停期间到达的完成通知不会让节点恢复，测试也覆盖了重载后不会重新执行（`plugin/test/plugin.test.ts:1200-1234`）。
- **节点结项前置条件补得完整**：request 和 commit 两处都检查后台 shell（`runtime_budget.py:918-919, 969-970`），避免「实验还在跑，节点已结项」。
- **resume 改为单一后端事务**：清除暂停原因和清零空转计数在同一事务里完成，带 revision 检查，并且模型调用不能触发（`runtime_budget.py:1263-1293`，测试见 `tests/test_background_shell.py:55-79`）。
- **真实宿主 probe**：`scripts/probe_background_wait.py` 用真实的 `opencode serve` 加本地假模型，核对了 `execute.after` 的 metadata 和 `inbox.enqueued`/`delivered` 的字段形状。这是判断 shellID 等字段名是否正确的关键依据（运行结果为作者自述）。

## 本次审阅未覆盖的内容

- **没有运行任何代码**：所有测试、probe 的通过结果都是作者自述。宿主行为（被中断或被拒绝的 shell 是否触发 `execute.after`、status 取值、热重载时 shell 是否存活）都没有验证，B2、S1、S4 的成立程度依赖这些行为。
- **没有核对 OpenCode 2.2.5 的事件与 metadata 字段**：`metadata.status/shellID/source/state/exit`、`inboxID` 等字段只能从作者的 probe 脚本里间接看到形状。
- **只读了部分 schema**：`workflow_intents` 的索引只核对了 `workflow_store.py`、`schema11/12/13.py`，其余 schema 文件没有逐一阅读（影响 S6）。
- **没有逐行阅读部分代码**：`probe_research_run.py`、`probe_packed_release.py` 的改动只扫读了参数和分支；`plugin/test/plugin.test.ts` 之外的已有测试没有读，不确定是否已经覆盖了 drain 期间调用 shell 的场景（影响 B1 是否会被现有测试暴露，但不影响代码路径的判断）。
- **看板 UI 只看了数据**：`board/model.ts` 的展示文案只看了数据来源，没有看实际渲染效果。
- **没有评估多宿主、多插件实例**：`pending_shells` 按 host 过滤，同一项目被多个宿主挂载时的行为没有分析。


## Review 2

来源：https://github.com/RinshannKaihou/auto-research-opencode/pull/13#issuecomment-6560599483

## Review — fix: prevent background research shells from stalling nodes

Reviewed at `d21ccd2`; line numbers refer to that commit. These findings come from reading the code. I haven't run the test suites.

**Verdict: request changes.** The approach is sound. The shell record IDs are deterministic, every call is idempotent through its operation ID, records are tied to the current generation, notices are isolated per session, and resume runs in one transaction with a revision check. One blocking bug: an interrupted turn can leave a node that can neither resume nor be recovered. A few other paths turn ordinary shell errors, or host event timing, into `fault` pauses.

### Blocking

**1. Interrupting a node during a shell call leaves it unable to resume or recover**

`afterTurn` returns before the new `external_unverified` handling (`engine.ts:412`) in three cases:
- on `interrupted` (`:357`),
- on a second `failed` (`:392`),
- whenever the session already has a `pause_reason` (`:411`).

If the user interrupts during a shell call, the shell record stays `starting` (if `execute.after` never fires) or `unverified` (if it reports a status other than `completed`). The task stays `running`. Then:

- `/research resume` gets `execution_unverified` (`runtime_budget.py:1286`), and the engine skips the node without saying why (`engine.ts:1185`).
- `/research recover task` fails, because `recovery_confirm_exit` requires the task to be `unverified` (`runtime_budget.py:581`).
- `node_finish_request` is blocked by the pending shell.

Only a plugin reload (`recoverWarm`) clears it, and that path fails the task.

**Suggested fix:** when resume finds `starting`/`unverified` shells, send them through `runtime_shell_unverified`. That marks the task unverified and notifies the main session, so `recover task` works. Also say in the resume message why a session was skipped.

### Should fix

**2. A foreground shell that doesn't return `completed` is treated as an unverified background job** (`index.ts:353`)

`request.status !== 'completed'` maps to `unverified`. At the end of the turn the node is paused with `fault` and its task is marked unverified; the only recovery then fails the task. If the host reports permission denials, timeouts or aborts as non-`completed`, ordinary shell errors will stall nodes — the problem this PR is meant to fix.

**Suggested fix:** mark a call `unverified` only if it asked for `background` or its metadata said `running`. If the host never moves foreground commands into the background, consider tracking only background calls.

**3. Strict validation of completion notices can stall a session's event queue** (`runtime_budget.py:1192–1197`)

`runtime_shell_notice` raises on:
- an `outcome` outside its five allowed values,
- a `message_id` without the `msg_` prefix (the TypeScript side passes `''` when `inboxID` is missing, `index.ts:771`),
- an exit code that isn't an integer.

In the event handler, three failures put the session into `failedEvents`, which blocks the node's tools. In `recoverWarm`, the error is thrown out of the loop, so the remaining tasks and projects are never reconciled.

**Suggested fix:** map unknown outcomes to `failed` and keep the raw value. Skip notices that have no inbox ID. Catch errors per job during recovery.

**4. The fix for fast-finishing shells only covers part of the race**

`pendingShellResults` is registered inside `execute.after`. If `session.inbox.enqueued` is handled before that hook is called at all, the notice matches no shell. The turn is still running, so the notice is treated as delivered mid-turn. As a result:
- the model loses all its tools mid-turn,
- the node is paused with `fault` and its task marked unverified,
- the shell record stays `running` after its notice has been used up.

**Suggested fix:** create a promise in `beforeTool` for each shell call in progress, and have the notice handler wait for those calls in that session (with a time limit) when it doesn't recognize the shell ID. Alternatively, hold unmatched notices by shell ID and match them again once `runtime_shell_result` has saved the ID.

**5. Freezing the idle count lets a node avoid the `no_progress` pause entirely** (`runtime_budget.py:1475`)

`(0 if shells else 1)` also applies to the turn that started the background job. A node that keeps doing "start a background job → wake up → start another" never builds up idle turns, and since there is no default turn budget, it can run forever. This is also inconsistent with the existing rule that running a shell isn't progress, and nothing limits how long a node waits on a job that hangs.

**Suggestion:** freeze only turns that end while waiting on a shell started in an earlier turn, or add a separate cap.

### Minor

- **Misleading fallback in the context hook** (`index.ts:664–666`): `host.messages` runs `ensureController` and `controller_check` within a 1-second limit. If that fails, every tool is removed and the model is told a background completion arrived, even when it's just a user question to a paused node. Use `recoveryMessages` here, and give a neutral message when the transcript is unknown.
- **Notice and `deliver` calls when pausing a node** (`engine.ts:470–475`): if either throws, `ended` marks the pausing node's task `unverified`. `deliver`'s wake path rethrows errors from notices unrelated to this node. The pause is already saved, so make these calls best-effort.
- **Shells that are still starting are counted as unverified** (`runtime_budget.py:1233`, `:1409`, and in workbench): the board shows "后台执行待核实" during every normal foreground command, and resume skips a busy node. Report these as a separate in-progress count.
- **A shell recorded without a task can never be cleared**: `runtime_shell_begin` allows `task_id=None`, but `recovery_confirm_exit` only clears shells by `task_id` (`:599`). Reject the call when there is no active task.
- `CHANGELOG.md` isn't updated.
- Every shell call now adds a permanent `workflow_intents` row and two database writes before and after the call. The table has no index besides its primary key, and status polling scans it with `json_extract`.
- `sessionState` doesn't catch errors from `runtime_shell_status`; the `project_read` call next to it does.
- The README says resume clears the idle count, but that only happens for sessions with their own pause, not when the whole project was paused.

### Tests

The new tests cover the paths the PR description claims, but not the interrupt and error paths in items 1–2, or the case in item 4 where the notice is handled before `execute.after` runs.
