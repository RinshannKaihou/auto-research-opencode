# PR #13 第三轮 Review 原文

来源：https://github.com/RinshannKaihou/auto-research-opencode/pull/13#pullrequestreview-5731189596

# PR #13 第三轮审阅：fix: prevent background research shells from stalling nodes

## 基本信息

- 仓库：RinshannKaihou/auto-research-opencode
- PR：#13，open，非 Draft，分支 `codex/fix-background-research-stall` → `main`
- **新 head SHA：`a6e48976d6063aa144ada5c037961e4b66acd369`**（二审时是 `698722a322b58dcc44330ad64955dcff81d21c3c`）
- Base：`main` @ `b8cac7694d0a53b868d980dbf040a85fc6c86d72`（#12 的 merge 提交），与前两轮相同。没有 rebase，也没有 force-push：`d21ccd2 → acdaf72 → 698722a` 原样保留，新提交直接叠在上面。
- 二审之后新增 1 个提交：

  | 提交 | 时间（UTC+8） | 说明 |
  |---|---|---|
  | `a6e4897` | 2026-11-02 01:42 | fix: reconcile background notices without racing native events |

- 规模：
  - 总计 38 个文件，+2984 / −23；
  - 本轮增量（`698722a...a6e4897`）19 个文件。代码改动：
    - `engine.ts`（+150/−45）、`index.ts`（+22/−5）、`runtime_budget.py`（+11/−8）、`controller-lifecycle.ts`（+5/−1）；
    - 新增测试 `plugin/test/background-reconcile.test.ts`（186 行）；
    - `plugin.test.ts` +41/−14，`test_background_shell.py` +24/−2；
    - 其余是 `docs/evidence/background-shell/round2/`。
- Mergeable：clean。新 head 上 **0 个 check run**，仓库没有 CI。
- 测试结果是**作者自述，未验证**：Python 799 passed，Bun 219 pass / 0 fail，TypeScript 通过。出处：PR 描述、`round2/README.md:26`、`round2/pytest.txt`、`bun.txt`、`typecheck.txt`。
- **新的「修复前失败」证据**：作者把本轮新增的定向用例放到二审版本上运行，并贴出了失败输出：
  - `round2/baseline-reproduction.txt`：竞态和队列阻塞，3 项失败；
  - `round2/baseline-storage-failure.txt`：失败事件写库，1 项失败。

  这说明新用例确实能抓住二审指出的问题（作者自述）。
- 审阅方式：只通过 cursor-github 接口只读读取：
  - PR 描述、提交、评论和 review；
  - `698722a...a6e4897` 的完整增量 diff；
  - 新 head 上的 `plugin/src/engine.ts`、`plugin/index.ts` 全文。

  另外回看了首轮 trace 中 `session.tool.failed` 的原始字段。没有 clone，没有运行代码，没有在 GitHub 上发布任何内容。
- 行号说明：`文件:行号` 都指新 head `a6e4897`。标「推断」的结论是读代码推出来的，没有经过运行验证。

## PR 上的回复与新增文档

- **我的二审 review 5729318200**：2026-11-02 01:19（UTC+8），COMMENT，下面没有 inline 评论。
- **另一份英文复审**：对话评论 6561275473，01:19（UTC+8），基线 `698722a`，由仓库所有者账号发布。它和我的意见相互印证：
  - 它的 A 就是我的 N1，还补充了一条我漏掉的路径：如果正常处理先把终态写进去，补查虽然不会再改任务状态，但仍会无条件执行 fault 暂停；
  - 它的 B：`session.tool.failed` 处理器没有捕获写库错误；
  - 三条 nit：补查期间占着项目队列、beforeTool 的错误码一律写成 paused、按 job 逐个暂停。
- **作者的处置**：没有写对话回复，统一放在新增的 `docs/evidence/background-shell/round2/README.md:7-19` 逐项表里；两条 review 原文收在 `round2/reviews.md`。首轮的 `README.md:3` 加了「本页为历史记录」的指引。PR 描述也同步更新了。

## 与其他 PR 的关系

与前两轮相同：#12 已经合入，#13 是仓库里唯一 open 的 PR，不存在冲突。本轮只改动了 #13 自己新增的代码，没有触及 #11/#12 的逻辑。

## 结论：**可合并**

- **二审唯一的 Should-fix N1 已修复，修法正确**。补查现在必须同时满足以下条件才会认定通知「遗漏」：
  - 这条通知的 inbox ID 从未被插件收到过（入队时同步登记到 `shellSeen`）；
  - 会话没有积压的事件，事件流也没有缺口；
  - 相隔至少 15 秒的两次扫描都看到它；
  - 在项目队列内重新读取状态之后，job 仍是 running；
  - 后端的条件更新确实改变了记录（`changed`）。

  正常通知和补查的写操作共用同一个项目队列，读取 transcript 放在队列外面，并有 1 秒上限。这样，我二审描述的竞态和英文复审补充的「先写终态、仍被暂停」路径都被堵住了（推断，作者有对应的时序用例和修复前失败输出）。
- **N2、N4 和 nit a、b、c、d 都已修复；N3 由作者保留**。保留理由有 trace 证据支持，可以接受，我二审的建议本身站不住，见下文。
- **英文复审的 B 和三条 nit 也都已处理**。
- **新代码没有发现 Should-fix 级别的问题**，只有 4 条 Nit（M1–M4），都可以在合并后再处理。
- **剩下的主要风险**：没有 CI；时序用例依赖可控的 fake host；真实宿主本轮只跑了打包后的后台完成场景，4 个原生协议 trace 沿用首轮。

---

## 二审条目逐条核对

| 二审条目 | 状态 | 依据（新 head） |
|---|---|---|
| N1 补查与正常完成通知的竞态（Should-fix） | **已修复** | 见下 |
| N2 `shellInboxes` 残留导致不再续推 | **已修复** | `engine.ts:1094-1097`（warm 恢复不再登记 inbox，也不预约）；`engine.ts:392-393, 420-426`（超过 30 秒未投递时转为显式暂停）；`engine.ts:283-292`（凭 transcript 恢复投递绑定）；`engine.ts:247, 320-324`（投递去重）；`engine.ts:276-279`（绑定失败可重试） |
| N3 被拒绝的后台调用仍要求人工核实 | **作者保留**，理由可以接受 | `round2/README.md:12`；见下文「关于我前几轮的错误」 |
| N4 每 15 秒读取完整 transcript | **已修复**（退避） | `engine.ts:389-400`：按会话指数退避，最长 60 秒；读取放在项目队列外（:400），并有 1 秒上限（:372-378） |
| a 预算被拒时重复提示 | **已修复** | `engine.ts:361` 记下 `refused`，:259 投递时不再重试 |
| b `session.tool.failed` 订阅过宽 | **已修复** | `index.ts:80-82`：不在 `shellCalls` 里的失败直接交给 telemetry |
| c 多余的 UPDATE | **已修复** | `runtime_budget.py:1202-1208` |
| d resume 误判仍在执行的 shell | **已修复** | `runtime_budget.py:1308-1312` 返回 `execution_binding_pending`，不改任务和 shell 状态；`engine.ts:1349` 给出相应提示；测试 `test_background_shell.py:210-216` |
| 英文 B：失败事件写库报错堵住事件队列 | **已修复** | `index.ts:808-816`：catch 后记 coverage_gap，finally 中清理；遗留的 starting 由 afterTurn 的 `reconcileShells` 接手 |
| 英文 nit：beforeTool 错误码 | **已修复** | `index.ts:354-355` 保留底层错误码 |
| 英文 nit：按 job 逐个暂停，已暂停的项目再加 fault | **已修复** | `engine.ts:427-431`：每个会话汇总后最多暂停一次，项目非 running 时不再暂停 |

**一审中仍部分保留的条目，本轮状态没有变化，判断不变**：
- S3：不加隐藏超时，可以接受；
- S4：冷启动保持保守，可以接受；
- S6：请求账本的增长，作者已注明；
- Nit 4、Nit 5：已写进文档。

### N1：已修复

逐层核对 `pollShells`（`engine.ts:381-436`）：
1. **入队即登记**：`index.ts:83-87` 在 `queueEvent` **同步**把 shell 通知的 `sessionID:inboxID` 放进 `shellSeen`（放在 lifecycle 状态里，跨 warm 重载保留，上限 1024）。所以只要插件收到过这个事件，不管处理到哪一步，补查都会跳过（`engine.ts:411`）。这是关键的一层，直接消除了二审描述的窗口。
2. **事件积压**：`eventsPending`（`index.ts:271`）在会话有事件链、有失败事件或有全局缺口时返回 true；补查在队列外（:387）和队列内（:403）各检查一次。
3. **二次观察**：第一次看到只记录时间（:412-413），间隔 15 秒以上再看到才处理（:414）。
4. **重新读取并条件更新**：在 `serial(root)` 内重新读取 `runtime_shell_status`（:404）；`runtime_shell_unverified` 只有在真正把状态改为 unverified 时才返回 `changed:true`（`runtime_budget.py:1273-1278`），否则跳过（`engine.ts:416`）；只有 `changed` 时才暂停（:427-431）。这同时解决了英文复审补充的「终态已写入仍被暂停」路径。测试：`test_background_shell.py:200-207`、`background-reconcile.test.ts:63-69`。
5. **共用队列**：`shellNotice` 的写操作移进了 `serial(root)`（`engine.ts:355-369`），与补查的处理阶段互斥。我核对了：项目队列内部没有任何地方等待会话事件链，事件链进入项目队列是单向的（`ended` 原本就是这样），不会形成死锁（推断）。
6. **慢读取不再占用队列**：transcript 在队列外读取（:400），有 1 秒上限，测试见 `background-reconcile.test.ts:88-99`。

**一个可以接受的边界**：事件流出现全局缺口时，`eventsPending` 对所有会话都返回 true，补查完全停止。缺口场景由原有机制处理：项目暂停，`/research resume` 时 `repairEvents` 调用 `recoverWarm` 从 transcript 补登（`index.ts:455-486`）。所以补查现在只覆盖「没有触发缺口标记、却静默丢失的单个事件」。这个分工合理，但 README:263 没有写明，见 M4。

### N2：已修复

- **warm 恢复**：只调用 `recordShellNotice` 补登终态，先删除可能存在的 inbox 条目，并标记为已投递（`engine.ts:1094-1097`），不再预约，也不登记 live inbox。测试见 `background-reconcile.test.ts:133-140`。
- **投递事件丢失**：
  - `nodeTurnAllowed` 发现有未投递的条目时，会读取 transcript，看最后一条输入是不是这条通知；如果是，就补做投递绑定（`engine.ts:283-292`）；
  - 补查对超过 30 秒仍未投递的条目，改为 `matched=false` 并显式暂停（:392-393, 420-426），不再隐蔽地阻止续推。
- **重复入队、绑定失败**：已投递的通知记入 `shellDelivered` 去重（:247, 320-324）；绑定抛错时把条目放回去，可以重试（:276-279）。绑定被拒时不给工具，并以 fault 暂停（:267-273）。

### N3：作者保留（可以接受）

作者的理由是：首轮中断 trace 里，进程确实已经启动，`session.tool.failed` 却同样带着 `executed:false`；所以失败事件和缺少 `shell.created` 都不能证明「没有启动」（`round2/README.md:12`）。我回看了 trace 原文：
- 中断：`error.type:'aborted'`，`metadata.shellID` 存在，`executed:false`；
- 「拒绝」场景：`error.type:'tool.execution'`，消息是 "No tool named \"shell\" is currently available"，`executed:false`，没有 shellID。

`executed` 字段确实不可靠，作者的保守处理成立。

---

## 关于我前几轮的错误

- **二审 N3 的建议站不住**：我建议用 `error.type`（例如 `tool.execution` 表示拒绝）或 `shell.created` 事件来区分「从未启动」。但 trace 表明 `executed:false` 在进程已经启动时也会出现，`error.type` 也不是「确定未执行」的专门信号。唯一看起来有区分度的是 `metadata.shellID` 是否存在，但只有两个样本，不能当作协议保证。作者不采纳是对的。
- **二审 N1 漏了一条路径**：我只描述了「补查先写，任务被改成 unverified」这一种结果，漏掉了英文复审指出的另一种：正常处理先把终态写进去，补查的 `runtime_shell_unverified` 什么也没改，但 `pauseSession('fault')` 仍会无条件执行。本轮的 `changed` 条件把两者一起修好了。
- **首轮证据标签不准确**：首轮 `host-foreground-denied.json` 被标为「权限拒绝」，实际错误是「当前没有可用的 shell 工具」。可能是通过权限配置把工具移除了。我二审照搬了「权限拒绝」的说法，没有核对原文。这不影响任何结论，但说法应当更准确。

---

## 本轮新发现

### M1（Nit，推断）：有未投递条目时，`nodeTurnAllowed` 每次都会读完整 transcript

- `engine.ts:283-292`：只要会话有任何 matched 的未投递条目，就先读取 transcript（最长 1 秒），然后才判断条目是否属于当前执行。
- `nodeTurnAllowed` 在 beforeTool（每次非只读工具调用，`index.ts:339`）和每次模型请求时都会被调用。所以「通知在本轮入队、等待下一个 step 边界才 inline 投递」的那段时间里，每次工具调用都会多读一次完整 context。
- **建议**：先判断 `waiting.some(item => !item.execution || item.execution !== this.turns.get(sessionID))`，只有存在不属于当前执行的条目时才读取 transcript。

### M2（Nit）：resume 在循环中途返回 `execution_binding_pending`，会把前面已改的状态一起提交

- `runtime_budget.py:1300-1314` 在遍历 shell 时，前面的迭代可能已经对其他 shell 调用了 `mark_shell_unverified`（任务改为 unverified）。后面遇到未绑定的在途执行时直接 `return`，事务照常提交，但返回值里只有 `execution_binding_pending`。
- 用户会看到「请核对宿主后重试 resume」，而任务其实已经是待核实状态了。再 resume 一次才能看到 `execution_unverified` 和恢复命令。
- **建议**：先完整遍历并分类，再统一决定；或者遇到 binding_pending 时，不处理其他 shell。

### M3（Nit，推断）：30 秒过期门闩在极窄窗口内可能误暂停

- 补查处理过期条目时，只检查会话不忙、没有积压事件、条目还在（`engine.ts:392-393, 403, 421-426`）。
- 场景：节点在一个长 step（例如长时间的前台命令）里时，后台通知已经入队；条目带着本轮的执行令牌，等这一轮结束后宿主才投递，条目早已超过 30 秒。如果补查恰好落在「`ended` 处理完、busy 已清除」和「宿主发出 `inbox.delivered`」之间，会被误判为投递丢失，会话以 fault 暂停。
- 后果只是可以 resume 的暂停，不改任务状态，而且窗口通常是毫秒级。
- **建议**：对带有 `execution` 的条目，从这一轮结束时开始计算 30 秒，而不是从入队时开始。

### M4（Nit）：README 没有说明补查与事件缺口修复的分工

`plugin/README.md:263` 描述了补查的条件，但没有说明：事件流出现缺口时补查会完全停止，要由 `/research resume` 的事件修复来补登。建议补一句，避免用户以为补查能覆盖所有缺口。

### 其他新代码的观察（无问题或已确认）

- **`shellNotice` 移入项目队列之后**（`engine.ts:355`）：通知的处理会排在 resume、调度等项目操作后面。好处是和补查、afterTurn 互斥；代价是项目队列繁忙时通知会延迟。没有发现死锁路径（推断，见 N1 第 5 点）。
- **inline 判定扩展到「用户 resume 的已授权轮」**：`engine.ts:253-257` 在会话忙、当前执行是 open、已绑定其他消息、有预约并且不是 ambiguous 时，按 inline 处理，不再预约。测试见 `background-reconcile.test.ts:150-158`。
- **`setInterval` 可能并发补查**：上一次补查未结束时，下一次照样启动（`index.ts:775-779`）。处理阶段在队列内，并有 `changed` 和二次观察保护，不会重复改状态（推断）。
- **beforeTool 的错误码**：底层错误没有 code 时用 `research_storage_unavailable`（`index.ts:354-355`），测试见 `plugin.test.ts:1401-1414`。

## Nits

只有上面的 M1–M4，没有其他新的 Nit。

## 做得好的地方

- **先证明再修复**：本轮新增的时序用例先在二审版本上运行并失败，输出也贴了出来，然后才修复。这是这个仓库里第一次有「修复前失败」的证据。
- **防护层层叠加，不依赖单一时间阈值**：入队同步登记、事件积压检查、二次观察、队列内重新读取、后端条件更新，每一层都能独立挡住一类误判。
- **对不采纳的建议给出事实依据**：N3 用 trace 的 `executed:false` 反驳了我的建议，而不是简单地拒绝。
- **过期状态显式化**：投递丢失由「隐蔽地阻止续推」改为「30 秒后显式暂停并说明原因」，用户能看到，也能处理。

## 本次审阅未覆盖的内容

- **没有运行任何代码**：799/219 和「修复前失败」都是作者自述。
- **时序推断没有验证**：M1、M3 和「没有死锁」的判断都是读代码得出的，没有在真实宿主上复现。
- **其余全文没有重读**：`runtime_budget.py`、`workflow_store.py` 等只读了增量 diff；`background-reconcile.test.ts` 只看了用例名和部分断言，没有逐行核对 fake host 是否忠实模拟了真实事件顺序。
- **宿主场景只核对了文本**：本轮的真实宿主只跑了打包后的后台完成场景（`round2/packed-host.txt`），中断和拒绝的原生协议没有重新运行，我也只核对了文本。


来源：https://github.com/RinshannKaihou/auto-research-opencode/pull/13#issuecomment-6567193819

## Re-review: fix commit `a6e4897`

Reviewed at `a6e4897`. I read the code and the round-2 evidence, but haven't run the test suites. The baseline reproductions of the earlier race are useful.

**Verdict:** both earlier issues and the nits are resolved. One new bug: the fallback that handles a missing delivery event can't bind a new turn against the real backend, so it pauses a healthy node instead of recovering. I'd fix that before merging.

### Previous round

| Item | Status | How it was fixed |
|---|---|---|
| A. Poller mistakes a normal completion for a lost one | Resolved | Notices are marked as seen when they're queued. Sessions with queued events are skipped. A notice counts as lost only if two scans at least 15 s apart both find it. Status is re-read under `serial(root)`, and the node is paused only when `runtime_shell_unverified` reports `changed`. `shellNotice` now takes the same lock, so the poller and the event handler can no longer interleave. |
| B. `session.tool.failed` uncaught | Resolved | Errors are recorded as a coverage gap and the in-flight call is cleared in `finally`. Failures from calls that aren't tracked go to telemetry instead of the event queue. |
| Nit: full transcript reads under `serial(root)` | Resolved | Reads happen outside the lock, time out after 1 s, and back off to once every 60 s. |
| Nit: `research_control_paused` used for every begin failure | Resolved | The error's own code is passed through. |
| Nit: one pause per job, even when the project is paused | Resolved | At most one pause per scan, and none if the project is already paused. |

Also good: warm recovery no longer creates a live inbox entry or reserves a turn; the resume message distinguishes `execution_binding_pending` from cases that need recovery; and an undelivered notice now expires after 30 s into a visible pause instead of blocking the node silently.

### New: should fix

**C. The fallback for a missing delivery event can't bind a new turn** (`engine.ts:283–291` → `:267`)

When a notice is waiting for delivery, `nodeTurnAllowed` reads the transcript. If the last input is that notice, it calls `inboxDelivered(sessionID, input.id)` with no `eventSeq`. When the turn is new rather than the turn the notice was queued in, the code then calls `runtime_execution_bind` with `event_seq: undefined`.

The backend rejects any bind without an `event_seq` unless the turn is already bound to that same message (`runtime_budget.py:480–485`). The turn has just started and isn't bound yet, so the bind comes back `matched: false`. The plugin then sets `shellWake` to `matched: false`, and `pauseSession('fault', '后台通知的执行绑定未完成…')` pauses the node. If the project is paused, the backend also revokes the turn's execution record.

Two consequences:
- **The case the fallback was written for never recovers.** A delivery event that really is missing always ends in a fault pause. The round-2 README says this path recovers the binding.
- **It can pause a healthy wake.** If the model request reaches the context hook before the plugin has received `session.inbox.delivered`, `waitEvents` has nothing to wait for, so the fallback runs. A normal idle-node wake then gets paused with `fault`; before this commit it would simply have bound when the event arrived.

`background-reconcile.test.ts:28` mocks `runtime_execution_bind` to always return `{matched: true}`. That's why the "transcript delivery witness clears a missing delivery event (new turn)" test passes even though the real backend rejects the bind.

**Suggested fix:**
- Use the transcript fallback only for the inline case. For a new turn, keep the read-only gate and let the real `session.inbox.delivered` event do the bind, waiting for it with a time limit if needed.
- Alternatively, add a backend bind path that accepts a transcript witness when the turn already has an `event_seq` newer than the last terminal event and there's an issued `turn_authorization` for that exact message.
- Either way, add an integration test in `plugin.test.ts` that uses the real backend: push `session.inbox.enqueued` and `session.execution.started`, run the context hook before pushing `session.inbox.delivered`, and assert the node keeps its tools and isn't paused.

I found no other new issues in the Python changes, the poller, or the `inboxDelivered` retry handling.
