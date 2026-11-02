# PR #13 第二轮 Review 原文

来源：https://github.com/RinshannKaihou/auto-research-opencode/pull/13#pullrequestreview-5729318200

# PR #13 第二轮审阅：fix: prevent background research shells from stalling nodes

## 基本信息

- 仓库：RinshannKaihou/auto-research-opencode
- PR：#13，open，非 Draft，分支 `codex/fix-background-research-stall` → `main`
- **新 head SHA：`698722a322b58dcc44330ad64955dcff81d21c3c`**（一审时是 `d21ccd2de2d7ca8d0c01b996ddaf4b38b5676eed`）
- Base：`main` @ `b8cac7694d0a53b868d980dbf040a85fc6c86d72`（#12 的 merge 提交），与一审相同。没有 rebase，也没有 force-push：`d21ccd2` 原样保留，新提交直接叠在它上面。
- 一审之后新增 2 个提交：

  | 提交 | 时间（UTC+8） | 说明 |
  |---|---|---|
  | `acdaf72` | 2026-11-02 00:51 | fix: address background shell lifecycle review findings |
  | `698722a` | 2026-11-02 00:54 | docs: normalize review evidence whitespace |

  `698722a` 只改了 `docs/evidence/background-shell/` 下 3 个文件的空白（+1/−3），代码与 `acdaf72` 相同。
- 规模：
  - 总计 28 个文件，+1930 / −17；
  - 本轮增量（`d21ccd2...698722a`）24 个文件，其中：
    - 代码：`engine.ts` +117/−61、`index.ts` +44/−14、`runtime_budget.py` +58/−27，以及 `workflow_store.py`、`workbench_read.py`、`research.ts`、`board/model.ts`、`controller-lifecycle.ts` 的小改动；
    - 测试：`plugin.test.ts` +141、`test_background_shell.py` +88；
    - 证据：新增 `docs/evidence/background-shell/`（README、reviews.md、pytest/bun/typecheck 原始输出、4 份真实宿主 trace、打包汇总、validation.json）；
    - 文档：`CHANGELOG.md`、`plugin/README.md`。
- Mergeable：clean。新 head 上 **0 个 check run**，仓库没有 CI。
- 测试结果是**作者自述，未验证**：Python 797 passed，Bun 200 pass / 0 fail，TypeScript 通过。原始输出见 `docs/evidence/background-shell/pytest.txt:13`、`bun.txt`、`typecheck.txt`。
- 审阅方式：只通过 cursor-github 接口只读读取：
  - PR 描述、提交、评论和 review；
  - `d21ccd2...698722a` 的完整增量 diff；
  - 新 head 上的 `plugin/src/engine.ts` 全文；
  - 证据目录中 `host-foreground-interrupt.json`、`host-foreground-denied.json` 两份 trace 的事件顺序。

  没有 clone，没有运行代码，没有在 GitHub 上发布任何内容。
- 行号说明：`文件:行号` 都指新 head `698722a`。标「推断」的结论是读代码推出来的，没有经过运行验证。

## PR 上的回复与新增文档

- **一审 review 5728997873**：2026-11-02 00:14（UTC+8），COMMENT。下面没有 inline 评论，也没有逐条回复。
- **另一份英文 review**：对话评论 6560599483，00:21（UTC+8），由仓库所有者账号发布，结论 request changes，审阅基线同为 `d21ccd2`。它和我的条目有较多重合：
  - 英文 1 ≈ 我的 B2；
  - 英文 3 ≈ S2；
  - 英文 5 ≈ S3；
  - 英文的「存储与索引」建议 ≈ S6。

  另外它独立提出了几条：
  - 英文 2：前台错误被当作后台的不确定状态；
  - 英文 4：通知早于 `execute.after` 本身到达；
  - 「启动后台任务的回合也被冻结空转计数」；
  - `sessionState` 的错误处理。
- **作者的处置**：没有写对话回复，统一放在新增的 `docs/evidence/background-shell/README.md:9-31` 逐项表格里；两份 review 原文收在 `reviews.md`。PR 描述也已更新：列出修改项、保留的边界（不加隐藏超时、冷启动保守、只支持原生 shell），以及 4 个真实宿主场景。
- **新增的宿主证据**（OpenCode 2.2.5，本地脚本化 provider）：
  - `host-background.json`：后台 shell 返回 running/shellID，完成通知入队并投递；
  - `host-foreground-error.json`：前台 exit 7，`execute.after` 为 completed，metadata.exit=7；
  - `host-foreground-interrupt.json`：没有 `execute.after`；`session.tool.failed`（seq 10）先于 `session.execution.interrupted`（seq 12）；测试进程已退出；
  - `host-foreground-denied.json`：权限被拒时同样没有 `execute.after`，只有 `session.tool.failed`；没有启动测试进程。

  后两份我核对过事件顺序。这些 trace 是脚本输出的 JSON，内容仍属作者自述。

## 与其他 PR 的关系

与一审相同：#12 已经合入（`b8cac76`），#13 直接基于它，是仓库里唯一 open 的 PR，不存在冲突或合并顺序问题。

本轮的交汇点：
- `notify_in_transaction`（`workflow_store.py:272-276`，#12 之前就存在的公用函数）改为「按显式 task_id，或者取该会话最新的任务」。这会影响所有调用方，属于正向修正。
- 执行观测（#11 的 evidence-v1）对后台 shell 改为在终态通知时记录（`engine.ts:301-305`）。

## 结论：**可合并（附建议）**

- **两个 Blocking 都已修复**：
  - B1：before hook 现在传入当前执行令牌，并有暂停后再开 shell 的回归用例；
  - B2：真实宿主证实，前台中断和权限拒绝时**根本不会触发 `execute.after`**。作者订阅了 `session.tool.failed` 来结清记录；没有终态见证时，原子地把任务也标为待核实；resume 会给出具体的恢复命令。
- **S1–S6**：
  - 已修复：S1、S2、S5、S6；
  - 部分修复：S3。加了每 15 秒的补查和已等待时长显示；「不加隐藏超时」由作者保留，理由可以接受；
  - 已修复同进程的情形：S4。冷启动由作者保留，理由成立。
- **10 条 Nits**：8 条已修复；Nit 4、Nit 5 改为在文档中写明边界，可以接受。
- **新代码里没有发现 Blocking 级别的问题**，有一条 Should-fix 和三条 Nit：
  - N1（Should-fix，推断）：每 15 秒的补查与正常的通知处理之间存在竞态，有小概率把一次正常完成的后台任务判为待核实。修起来很小，建议合并前处理；
  - N2：`shellInboxes` 的条目可能残留，导致节点不再被续推；
  - N3：后台调用被拒绝时仍要求人工核实；
  - N4：每 15 秒读取完整 transcript 的开销。
- **剩下的主要风险**：没有 CI；所有宿主行为只在脚本化 provider 下验证过。

---

## 一审条目逐条核对

| 一审条目 | 状态 | 依据（新 head） |
|---|---|---|
| B1 暂停收尾轮不能开 shell | **已修复** | `plugin/index.ts:339-344`；`engine.ts:239`；测试 `plugin.test.ts:1337-1347` |
| B2 前台出错或中断后无出口 | **已修复** | 见下 |
| S1 inline 判定时机 | **已修复** | `engine.ts:241-250, 289-300, 485`；测试 `plugin.test.ts:1423-1439` |
| S2 未匹配或迟到的通知处理过重或抛错 | **已修复** | `runtime_budget.py:1231-1239`；`engine.ts:267-286, 291-297`；测试 `plugin.test.ts:1441-1451`、`test_background_shell.py:132-139, 181-188` |
| S3 等待无上界，事件缺口后不重扫 | **部分修复**（补查和时长显示已加；不加超时由作者保留，可以接受） | `engine.ts:311-334`；`index.ts:764-769`；`board/model.ts:267-270`；`README.md:263`。补查本身引入 N1 |
| S4 热重载把运行中的后台任务判为未核实 | **已修复（同进程）**；冷启动由**作者保留**（理由成立） | `engine.ts:994-1004`；测试 `plugin.test.ts:1292-1316` |
| S5 resume 清零范围、反馈与 revision | **已修复** | `runtime_budget.py:1297-1317`；`engine.ts:1231-1257`；测试 `test_background_shell.py:142-150` |
| S6 每次 shell 都写意图，查询全表扫描 | **已修复**（请求账本的增长仍在，作者已注明） | `runtime_budget.py:1204-1205, 1198-1199`；`workflow_store.py:73-76`；测试 `test_background_shell.py:163-172` |
| Nit 1 transcript 读取失败时一律按后台唤醒处理 | **已修复** | `index.ts:674-676`：读取失败只保留读工具；只取最近 64 条（宿主仍然返回完整 context） |
| Nit 2 暂停通知去重键可能误合并 | **已修复** | `engine.ts:520`，没有 turn 时用 `randomUUID()` |
| Nit 3 两份可恢复原因列表 | **已修复**（一致性测试） | `test_background_shell.py:191-197` |
| Nit 4 只跟踪 `shell` | **作者保留，并写入文档**；可以接受 | `README.md:263` |
| Nit 5 暂停期间宿主唤醒不计预算 | **已在文档中说明** | `README.md:259` |
| Nit 6 messageID 校验 | **已修复** | `engine.ts:291-292` |
| Nit 7 后台执行观测记在启动时 | **已修复** | `index.ts:375-376`；`engine.ts:301-305` |
| Nit 8 通知可能挂到旧任务，文案误导 | **已修复** | `workflow_store.py:273-276`；`runtime_budget.py:35-37` 改为 `paused` 类型并显式绑定 task |
| Nit 9 没有活动任务的 shell 记录 | **已修复** | `runtime_budget.py:1168-1169`；测试 `test_background_shell.py:175-178` |
| Nit 10 缺 CHANGELOG 和证据 | **已修复** | `CHANGELOG.md:3-8`；`docs/evidence/background-shell/` |

### B1：已修复

- `index.ts:339-341` 把 `engine.executionToken(sessionID)`（`engine.ts:239`）作为 `execution_token` 传给 `runtime_shell_begin`；失败时转换成带 `research_control_paused` metadata 的 ToolError（:342-344）。
- 测试 `plugin.test.ts:1337-1347` 覆盖了「暂停 → 收尾轮开 shell → resume 不误报」。
- 如果令牌已经过期（上一轮的令牌）：运行状态下 `require_model_drain` 直接放行；暂停状态下对应的 execution_start 不是 open，会被拒绝。语义正确（推断）。

### B2：已修复

- **前台的结清**：`index.ts:363-364` 对前台调用的非 completed 状态记为 `finished`；只有请求了后台、或者返回 running 却没有 shellID 时，才记为 `unverified`。
- **after hook 缺失**：`index.ts:790-801` 订阅 `session.tool.failed`，按 call id 结清 `shellCalls` 里登记的调用。真实 trace 证明它先于 `execution.interrupted` 到达，所以 afterTurn 看到的已经是结清后的状态。
- **原子化**：`mark_shell_unverified`（`runtime_budget.py:27-38`）在同一事务里把意图和任务都改为 unverified，并发出带恢复命令的通知；`runtime_shell_result(state='unverified')` 也走这条路径（:1206-1207）。
- **afterTurn**：在判断 outcome **之前**先调用 `reconcileShells`（`engine.ts:410-413`），所以 interrupted 和 failed 的轮次也会处理遗留的 starting/unverified 意图。
- **resume**：`runtime_budget.py:1300-1314` 对非 running、并且不属于仍然 open 的执行的 shell 调用 `mark_shell_unverified`，返回值带 `task_id`；引擎会在消息里列出 `/research recover task <id>`（`engine.ts:1243-1245, 1257`）。
- **回归用例**：`plugin.test.ts:1349-1358, 1372-1408`；`test_background_shell.py:114-129`。

### S3：部分修复

- **补查**：`pollShells`（`engine.ts:311-334`）每 15 秒检查一次不忙的节点。对于 running 状态的 job，如果在 transcript 里找到对应的完成通知，就补登终态，并以 fault 暂停。看板显示已等待的秒数（`board/model.ts:267-270`、`workbench_read.py:168-178`）。
- **作者保留**：没有配置 `seconds` 时不加隐藏超时，需要上界的用户显式配置墙钟预算（`README.md:263`）。这和 #12 S1 的预算取舍一致，可以接受。
- **残留**：补查发现遗漏的通知后，会把任务判为待核实，最终只能记为 failed。理由是「无法核对原生唤醒授权」，这是保守但成立的选择。不过补查本身和正常的事件处理之间有竞态，见 N1。

### S4：同进程已修复，冷启动由作者保留

- `recoverWarm` 对 running 状态、但没有终态见证的 job 不再判为未核实（`engine.ts:994-998`）；仍在 `shellCalls` 里登记的 starting 调用也会保留。测试的预期从「unverified」改成了「running」（`plugin.test.ts:1292-1316`）。
- 作者指出「没有终态通知，并不能单独证明进程跨宿主进程仍在运行」（`README.md:18`）。这一点成立，我一审的表述（「transcript 里没有终态通知本身就说明 shell 还没结束」）对冷启动的情形说得过头了，见下文。

### S6：已修复

- 前台调用得到 `finished` 后直接删除意图行（`runtime_budget.py:1204-1205`；前面那句 UPDATE 已经多余，见 Nit c）。
- 去重查询加了 `session_id`（:1198-1199）。
- 新增两个部分索引（`workflow_store.py:73-76`），用 `CREATE INDEX IF NOT EXISTS` 在可写初始化时补装，schema 仍是 16。查询计划测试 `test_background_shell.py:163-172` 只验证了简化后的查询（`state='running'`），`pending_shells` 实际使用的 `IN (...)` 加两个 `json_extract` 条件是否也命中索引，没有断言（推断应该会命中 `(session_id,state)` 前缀）。
- `requests` 账本里的回放记录仍会增长，作者在 README:61 中写明了。

---

## 关于我一审的错误

- **B2 的事实前提只对了一半**：我推测前台中断时 after hook「可能以 `status:'error'` 触发，也可能缺失」。真实宿主的结果是**缺失**，前台中断和权限拒绝都只有 `session.tool.failed`。所以我给的修复建议 3（看 status 区分前台错误）单独使用是不够的，还必须订阅 `session.tool.failed`。作者的做法比我的建议更完整。
- **S4 说得过头**：我写「transcript 里没有终态通知，本身就说明 shell 还没结束」。这只在同一个宿主进程内成立；跨进程（冷启动）时宿主的 shell 注册可能已经丢失，作者保留冷启动的保守处理是对的。
- 其余条目没有发现判断错误。

---

## 本轮新发现

### N1（Should-fix，推断）：每 15 秒的补查与正常完成通知之间存在竞态，可能把一次正常完成的后台任务判为待核实

- **补查的判定条件**：`pollShells`（`engine.ts:314-328`）只要同时满足下面三条，就认定通知「遗漏」：
  - 节点不忙（`!this.busy.has(sid)`）；
  - 数据库里 job 仍是 running；
  - transcript 里已经有这个 shellID 的完成消息。

  认定之后，它依次调用 `runtime_shell_unverified`（任务改为 unverified）、`runtime_shell_notice`，然后以 fault 暂停。
- **正常路径**：节点空闲时后台 shell 结束，宿主几乎同时入队和投递通知，并开始新的一轮，完成消息此时已经出现在 transcript 里。插件这边，事件在会话事件链上依次处理：
  - `inbox.enqueued` 的处理器会先等待最多 2 秒，让同会话在途的 shell 调用结清（`index.ts:786-787`）；
  - 之后才调用 `runtime_shell_notice`，把 job 改为 completed；
  - 再之后处理 `execution.started`，节点才变成 busy。
- **竞态窗口**：如果补查（在 `serial(root)` 内，与事件链不互斥）恰好落在这个窗口里，就会把一次正常、已经授权的完成判为「遗漏」：任务变成 unverified，只能由用户核实后记为 failed。之后事件链里的 `shellNotice` 去预约时，会撞上 `ResearchExecutionUnverified`，只能静默失败。
- **影响**：窗口通常只有几十毫秒，同会话有在途 shell 时最长约 2 秒；每次后台完成被误判的概率约等于「窗口 / 15 秒」。概率低，但后果是任务失败，而且用户会看到误导性的「已补登遗漏的后台完成通知」。
- **测试缺口**：现有测试 `plugin.test.ts:1382-1395` 只覆盖了真正丢失事件的情形。
- **建议**（任选一种）：
  1. 补查只处理连续两次扫描（≥15 秒）都看到的通知，或者通知消息的时间早于某个阈值的情形；
  2. 引擎可以看到插件的事件链状态（`waitEvents`、`failedEvents`、`eventGap`），只在没有积压的事件、或者确实发生过事件缺口时才补查；
  3. 补查发现通知时先走 `shellNotice` 的正常路径，只有当前执行确实无法与这条消息对应时，才判为待核实。

### N2（Nit，推断）：`shellInboxes` 的条目只在投递事件里删除，残留后节点不会再被续推

- `afterTurn` 在 `engine.ts:485` 里，只要同会话还有 `matched` 的 `shellInboxes` 条目，就不再 `continueSession`（这是为了避免和宿主的原生唤醒重复）。
- 条目只在 `inboxDelivered` 里删除（:243-245）。以下两种情况会留下残留条目：
  - `session.inbox.delivered` 事件丢失或被搁置；
  - `recoverWarm` 通过 `shellNotice`（:992）为一条**已经投递过**的通知重新登记。这时也会重新走 `reserveShellWake`：与原来同一个 operation id 时会回放；原先是 inline 投递的话，则会新增一次预约。
- 残留之后，这个节点每一轮结束都不会被续推，也不处于暂停状态，看板显示「活动」。
- **建议**：
  - 条目带上创建时间或执行令牌，在下一次 `started`/`ended` 时清理；
  - `recoverWarm` 只补登终态，不再登记 inbox 条目，也不再预约。

### N3（Nit）：请求了后台但被拒绝的调用，仍然要求人工核实

- `index.ts:797-798` 对 `background:true` 的调用，只要收到 `session.tool.failed`，一律记为 unverified；任务因此需要人工核实，最终记为 failed。
- 但作者自己的 denied trace 显示，权限被拒时连 `shell.created` 都没有，进程没有启动。作者在 README:13 中说明了「请求后台而缺少终态见证时保留核实要求」，这是有意的保守选择。
- **建议**：利用宿主的 `shell.created`/`shell.exited` 事件（probe trace 里已经能看到），或者 `error.type`（例如 `tool.execution` 表示拒绝），区分「从未启动」和「启动后结局不明」。

### N4（Nit）：补查每 15 秒读取一次完整 transcript

`pollShells` 对每个有 running 后台 job 的空闲节点，都调用一次 `recoveryMessages`（`engine.ts:319`），而宿主返回的是完整 context。长会话加上长时间运行的实验时，开销会一直累积。建议只在 running 的 job 存在超过一段时间后才扫描，或者让宿主只返回最近 N 条消息，如果它支持的话。

### 其他新代码的观察（无问题或已确认）

- **空转计数**：`runtime_turn_finished` 用事件 `runtime.shell_started` 区分「启动后台的回合」（计入空转）和「单纯等待的回合」（冻结）（`runtime_budget.py:1446-1453, 1506`）。`engine.ts:477-483` 的 `no_progress` 判断现在位于等待判断之前，所以「反复启动后台任务」最终会被 `no_progress` 暂停。这修复了英文 review 第 5 条，测试见 `test_background_shell.py:153-160`。
- **通知早于 `execute.after`**：`shellCalls` 在 before hook 中登记（`index.ts:334-337`），通知处理器会有界等待同会话的所有在途调用（:786-787）。测试 `plugin.test.ts:1410-1421` 把通知放在 after hook 之前。
- **未知 outcome**：规范为 failed，并保存原始值 `raw_outcome`（`runtime_budget.py:1216-1222`）；终态冲突、已核实退出、匹配不到时都返回 `ignored`，不再抛错。插件侧只接受 `sh_`/`msg_` 前缀（`engine.ts:291-292`）。
- **预约失败的区分**：预算被拒时暂停为 `budget`，其他错误暂停为 `fault`，任务状态不变（`engine.ts:272-285`）。afterTurn 不再因为 `matched:false` 把任务改为 unverified（:421）。
- **pause 的 best effort**：`pauseSession` 的辅助消息、通知和 deliver 失败都被吞掉（`engine.ts:515-522`），回归测试 `plugin.test.ts:1360-1370`。
- **resume**：不再要求会话有 `pause_reason`（`runtime_budget.py:1297`），所以项目级暂停时也会清零空转计数；revision 冲突时重读并重试一次（`engine.ts:1232-1242`）。属于仍然 open 的执行的 starting 调用不会被误判（:1300-1309），对应 B1 用例里「收尾中 resume」的情形。
- **`sessionState`**：`runtime_shell_status` 失败时降级为 null（`research.ts:476`）。

## Nits

a. **预算被拒时可能重复发同一条暂停消息（推断）**：`reserveShellWake` 在预算被拒时，入队阶段已经暂停过一次；投递时（`engine.ts:248`）满足 `matched && !authorized`，会用同一个 operation id 再预约，回放得到 `allowed:false`，于是再调用一次 `pauseSession('budget', ...)`，给节点重复发送同样的提示。建议入队阶段已明确被拒时，在条目里记下 `refused`，投递时不再重试。

b. **订阅范围偏宽**：`session.tool.failed` 加入了 `relevantEvents`（`index.ts:73`），所以**所有**会话、所有工具的失败都会进入有序事件链，并触发 `ensureController` 等开销，也占用 256 个事件的队列上限。建议在 `queueEvent` 里先判断这个 call 是否在 `shellCalls` 中，不在就直接交给 telemetry。

c. **多余的 UPDATE**：`runtime_shell_result` 在 `finished` 时先 UPDATE 再 DELETE（`runtime_budget.py:1202-1205`），前面那次 UPDATE 可以省掉。

d. **resume 可能误判仍在执行的 shell（推断）**：resume 判断 starting 调用是否「仍在执行」时，要求 execution_start 带 `message_id` 和 `reservation_id`，并且不是 ambiguous（`runtime_budget.py:1306-1308`）。推断出来的执行（ambiguous 或尚未绑定 message）里如果有在途的前台 shell，resume 会把它判为未核实，任务进入待核实。这个窗口很小，可以在 README 里注明，或者对 ambiguous 的执行只拒绝 resume、不改任务状态。

## 做得好的地方

- **用真实宿主证伪假设**：两份审阅都只能推测 OpenCode 的行为，作者用 4 个真实宿主场景确认了 `execute.after` 在中断和拒绝时缺失，并据此改用 `session.tool.failed`，而不是照搬审阅者的建议。
- **处置记录完整**：两份审阅逐项给出接受、部分接受或保留的理由（`docs/evidence/background-shell/README.md:9-31`）；原始测试输出和 trace 都附上了，validation.json 记录了受测源码的摘要。
- **不确定状态原子化**：意图、任务和恢复通知在同一事务里建立，消除了一审 B2 的「无出口」状态。
- **安全边界没有放宽**：无法核对的原生唤醒仍然不会获得工具权限，冷启动仍要求人工核实，没有为了「不卡住」而补造执行授权。
- **回归用例针对性强**：通知早于 after hook、入队和投递跨轮、迟到通知、未知状态、恢复列表一致性、索引查询计划，都有直接的测试。

## 本次审阅未覆盖的内容

- **没有运行任何代码**：测试数量和宿主场景都是作者自述；trace 只核对了中断和拒绝两份的事件顺序。
- **N1、N2 都是推断**：两者都依赖事件处理的时序，没有构造出可复现的用例。
- **其他全文没有重读**：新 head 上 `index.ts`、`runtime_budget.py` 的全文没有重新拉取，只读了增量 diff 加上一审时的全文；`engine.ts` 读了新 head 全文。
- **没有核对 `bun.txt` 的完整用例列表**：没有确认 200 个用例逐一包含 PR 声称的新回归。


来源：https://github.com/RinshannKaihou/auto-research-opencode/pull/13#issuecomment-6561275473

## Re-review: fix commits `acdaf72`, `698722a`

Reviewed at `698722a`. I read the code and the OpenCode traces in `docs/evidence/background-shell/`, but haven't run the test suites myself.

**Verdict:** all five earlier findings and the minor items are addressed, including the blocking one. The new 15-second poller adds a race that can fail a healthy node; I'd fix that before merging. One other small fix and a few nits below.

### Earlier findings

| # | Status | How it was fixed |
|---|---|---|
| 1. Interrupted node stuck | Resolved | `afterTurn` settles unfinished shells before any early return. `runtime_resume_session` marks leftover shells and their task unverified and returns the task ID. Resume now prints that ID with the recover command. |
| 2. Foreground errors | Resolved | Only calls that asked for `background` become `unverified`. Foreground aborts and denials are settled from OpenCode 2.2.5's `session.tool.failed`. The traces show it arrives before `session.execution.interrupted`. |
| 3. Strict validation | Resolved | Unknown outcomes become `failed`, and the original value is kept as `raw_outcome`. Conflicting, late and unmatched notices return `ignored`. The plugin skips invalid IDs, and recovery catches errors per job. |
| 4. Fast-shell race | Resolved | Shell calls are registered in `beforeTool`. The notice handler waits up to 2 s for that session's calls in progress. |
| 5. Idle freeze | Resolved | The turn that starts a job counts as idle; only pure waiting is frozen. The docs say to set `max_wall_seconds` if waits need a limit. |
| Minor items | Resolved | Context fallback, best-effort pause notices, separate `starting` count, no records without a task, CHANGELOG, indexes, deleting finished rows, `sessionState` catch, README. |

The warm-reload change is also good: a running shell in the same host process keeps being waited on, so a reload no longer fails long experiments.

### New: should fix

**A. The poller can mistake a normal completion for a lost one** (`engine.ts:311–328`)

`pollShells` runs under `serial(root)`, while `shellNotice` runs on the session's event chain, so the two can interleave. For a waiting node, the shell message can appear in the transcript before the plugin has finished handling `session.inbox.enqueued`. `busy` isn't set until the plugin processes `session.execution.started`. The poller also fetches the whole transcript between reading the status and acting on it, so a long transcript widens the window.

If a scan lands in that window, there are two possible outcomes:
- **The poller acts first:** `runtime_shell_unverified` marks the shell and the task unverified. A healthy completion then ends in operator recovery, which fails the task.
- **The handler records the notice first:** `runtime_shell_unverified` does nothing, because the record is already `completed` (`runtime_budget.py:1271`). But `pauseSession('fault', …)` still runs, so the node is paused with a "lost notice" message, possibly in the middle of its authorized wake turn.

**Suggested fix:**
- Skip notice IDs the event handler has already seen or is still processing, for example with a set of seen inbox IDs, or by skipping sessions that have queued events.
- Only treat a notice as lost once it is older than a grace period.
- Re-read the status after fetching the transcript.
- Only pause when `runtime_shell_unverified` reports that it actually changed something.

**B. The `session.tool.failed` handler doesn't catch errors** (`index.ts:790–800`)

If its `runtime_shell_result` call hits a temporary storage error, the event fails three times and the session goes into `failedEvents`. That holds back the `session.execution.interrupted` event that follows and blocks the node's tools. `execute.after` handles the same failure by recording a coverage gap instead. Do the same here; the shell record then stays `starting`, and `reconcileShells` already handles that at the end of the turn.

### Nits

- The poller fetches the full transcript of every waiting node every 15 s, inside `serial(root)`. Turn-end handling, `resume` and scheduling all wait behind it. Consider polling less often for long-running jobs.
- `beforeTool` reports every `runtime_shell_begin` failure as `research_control_paused` with `String(error)` (`index.ts:344`). That includes "A shell requires an active node task" and storage errors. Pass through the error's own code instead.
- `pollShells` calls `pauseSession` once per job, and also does so when the project is paused.

I found no other new issues in the Python changes, the indexes, or the resume rework.
