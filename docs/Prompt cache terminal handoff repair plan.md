# Prompt cache: terminal handoff repair plan

2026-11-04。状态：修复 A、C 已实现并通过本地检查；修复 B 只做到部分缓解，**仍被宿主契约阻塞**，不能按“已修复”记录（见 §11 与 [handoff 记录](./evidence/context-cache/handoff/README.md)）。基线：`555b449`，分支 `cursor/prompt-cache-stabilization`，PR #35。

本计划只覆盖本次复现的用户输入交接、收尾判定过期、临时授权失败丢标记。它优先于原 [PR #35 remediation plan](<./Prompt cache PR 35 review remediation plan.md>) 中与这三处冲突的实现细节，其他约束与真实提供方验证门槛继续有效。

## 1. 已确认的问题与用户选择

基线的 5 个临时复现均通过了“缺陷确实出现”的断言，不是修复后的回归结果：

| 复现 | 观察 |
|---|---|
| 主协调 wait 后收到同一执行中的用户输入 | 用户文本和 guidance 都存在，但请求仍为 0 工具，并带旧的 wait 收尾指令 |
| 节点 finish 后收到同一执行中的用户输入 | 同样保留文本，却被旧 finish 收尾约束覆盖 |
| 主协调等待 memory 时处理了旧轮结束、新轮开始 | hook 返回前新执行已被处理，当前请求仍带旧 wait 指令 |
| 节点等待 memory 时处理了旧轮结束、新轮开始 | 当前请求仍带旧 finish 指令；提前 waitEvents 不能覆盖后面的异步窗口 |
| 一次 runtime_drain_check 临时异常 | 首次只读并删标记；恢复后拿回 19 个工具，包括 shell、research_note；后续写入成功，pending finish 仍保存原总结 |

代码位置（基线）：`plugin/index.ts` 的 injectContext、runTool、prompt hook 与终态事件处理；`plugin/src/engine.ts` 的 nodeTurnAllowed。节点总结由 `node_finish_request` 保存，真实终态后才由 afterTurn 提交。

**用户已选择：保留已经接受的收尾，新消息排到下一轮处理。** 本次不实现“立即接管并撤销 pending finish”。主协调 wait 采用相同的输入交接规则。

因此，修复不能仅清空本地 marker 并恢复写工具，也不能把节点下一轮解释为重新打开已经完成的工作段。

## 2. 目标行为

| 状态 / 输入 | 应有行为 |
|---|---|
| 同一执行已经接受 wait / finish，收到普通用户消息 | 使用宿主原生 queue，保留原消息 ID、文本、附件和原有次序；当前执行继续收尾 |
| 终结工具正在等待服务返回，用户消息到达 | 同样排队，防止用户消息落入“服务已接受、标记尚未安装”的间隙 |
| wait / finish 调用失败 | 不创建 accepted marker；清除本次在途记录。已排队的消息仍由宿主处理，不撤回或复制 |
| 正常收尾，无新消息 | 原有 full-system、无临时 tail、无新 placement hint、无工具的收尾路径保持 |
| 节点授权临时未知 / 暂时不允许 | 保留 accepted marker，保持只读安全约束；恢复后重新核实 pending finish，继续收尾 |
| hook 异步期间执行、attachment 或事件边界改变 | 不提交旧收尾指令，不用旧判断恢复写权限；本次走完整系统上下文的保守只读路径 |
| 队列消息在真实下一轮被投递 | 正常处理其原始用户输入，不带前轮 wait / finish 指令；权限由新一轮当前角色和后端状态决定 |
| 已完成节点收到队列消息 | 可以解释 / 读取现有结果；不能因此重新获得 shell 或写权限。新工作仍需原有调度 / 恢复规则 |
| 系统模式、无关会话、盲评专家 | 不增加收尾排队规则，不放宽既有隔离或授权 |

原生中断、暂停、停止及恢复命令保留原有入口和优先级；本计划不把它们改成普通聊天队列项。queue 是明确的延迟处理，不是丢弃消息，也不承诺立刻终止正在进行的收尾。

## 3. 范围与限制

- 保持 `contextPlacement: "system"` 默认；tail 仍为实验选项。
- 生产修改优先限制在 `plugin/index.ts`、`plugin/src/controller-lifecycle.ts`；仅在确有必要时加小型纯辅助函数。
- 不增加 Python RPC、schema、迁移或 pending finish 撤销接口；不改 renderer、12,000 字符预算、工具能力定义、成本计量或通用执行步数上限。
- 不新增消息投递队列、后台投递 worker、重发 synthetic 消息、TTL、定时轮询或 provider registry。
- 继续使用原始完整上下文和原始持久历史做 checkpoint / evidence 校验；不把临时 tail 或队列消息伪装为已投递历史。
- 不改变 specialist 的 stop / summary / busy 行为或事件观察接线。
- 真实提供方对“有工具历史但无工具定义”的接受性仍未验证。此修复不解除已有 native gate。

## 4. 第一步：锁定宿主 queue 契约和复现

先扩展现有隔离宿主 / 本地脚本 provider fixture，不先修改生产路由。不要恢复已归档的大型 WP probe。

已从安装的 2.2.5 类型确认：SessionPrompt 有 `messageID`、`prompt`、`metadata` 和可修改的 `delivery`；Delivery 为 `steer | queue`。这只是类型证据，以下行为必须以实际宿主证明：

1. 一次真实用户 prompt 到达繁忙会话时，将 delivery 从 steer 改成 queue，消息在当前执行结束后才投递，并产生新的执行边界。
2. prompt hook 是在 enqueue 还是 delivery 运行，是否会运行两次；`messageID` 如何对应 inbox enqueued / delivered 和最终历史。
3. messageID、原文本、附件、顺序不变；多个队列输入按宿主语义处理，每个逻辑输入只投递一次，不要求“一条输入必有一次模型请求”。
4. research report 的 `resume:false` synthetic 通知、自动 wake 和临时 research_state 不被误当成真实用户 prompt；不因它们触发用户排队规则。
5. 队列输入随后到达已完成节点时，宿主能开始用户交互，插件保持现有只读 / 已结束工作段限制。
6. 中断 / 停止入口不会被此 prompt 路由改写；停止后的队列行为沿用宿主规则，不擅自重新启动研究。
7. 所有模型请求只到 loopback provider，使用临时 XDG 目录和假 key，不访问共享服务或用户研究数据库。

将前次临时测试移入正式测试时保留缺陷含义，改为修复期望：同执行新用户消息的两个用例要模拟 delivery 的实际效果，不能忽略 hook 把 steer 改成 queue 后，仍人为把它追加进旧请求历史。两个晚到终态 / 新开始事件的用例继续直接检查旧指令不会泄漏。

**停止条件：** 若宿主不遵守修改后的 queue，或不能区分真实用户输入与通知，记录实际缺口，不自行实现消息队列、取消执行或新 RPC。本次选择依赖该契约，不能用假宿主的字段赋值作为证明。

## 5. 修复 A：授权失败不等于收尾失效

先做最小、独立可审阅的正确性修复。

1. 删除 nodeTurnAllowed 返回 false 时无条件删除 accepted marker 的分支。
2. 保留现有 fail-closed 权限：异常时不开放 shell、研究写入或新的节点工作。错误路径不生成“节点已经成功结束”的断言。
3. 对一个已有 accepted finish marker 的执行，暂时无法确认授权时使用 full-system 安全上下文，不加 tail / placement hint。保持只读或更严格的限制；不能回到普通可工作的 tail 请求。
4. 后续请求恢复时，仍核对现有节点授权、当前 execution 和 `node_finish_pending.turn_token`。匹配就继续正常收尾，不重发 research_finish、不重写总结。
5. marker 只因已验证的终态、新执行、session cleanup、system 回滚，或成功的权威查询明确证明它不再适用而失效。异常、超时和查询不可用不是失效证据。
6. 清除时 compare-and-delete 同一个 marker；旧请求不能误删新的 marker。

优先不改变 nodeTurnAllowed 的 boolean API：本问题首先是错误的 marker 删除策略，不必为此改动它的所有调用者。若需要区分错误文案，再局部处理；不要把 unknown 当作 allowed。

验收：注入一次临时 drain 错误后，不能重现“恢复后拿回 19 个工作工具”；pending 总结未变化，下一次稳定准备仍收尾。重复故障、暂停 / 预算拒绝同样不能解除该屏障。

## 6. 修复 B：真实用户输入由宿主排队

### 6.1 同时覆盖 accepted 与在途窗口

- 保留现有 waitFinishes accepted marker 及其 warm-rebind 语义。
- 在 await `engine.wait` / `engine.finish` 之前登记“本 execution 有终结工具在途”，在 finally 中只移除本 call 的登记；成功后再形成 accepted marker。
- 在途登记使用现有 runtime state 的可选小字段。按 execution 和每次实际调用的身份区分，支持同一工具组内的并行 / 重复调用；可用每次调用独立的对象 token 或引用计数，不能仅用可能重复的 operationID 做 Set 并在首个 finally 时清掉全部登记。一个失败调用不得删除另一个成功调用留下的 accepted marker。
- 该登记仅用于输入路由，不表示工具已成功，不用于结束节点、授权写入或伪造成功收尾。
- 旧 execution 的迟到返回、旧 attachment、重复终态，不得覆盖或清掉新 execution 的状态。复用现有 session cleanup 与新执行清理入口。

### 6.2 prompt hook

对已确认的真实用户 prompt，只在 tail 模式、主协调 / node_core、当前 execution 存在 accepted marker 或终结工具在途时，将 `input.delivery` 设为 `queue`。用户本来选择 queue 时保持 queue。

- 不改 messageID、文本、附件、会话、原生入队次序。
- 不创建另一条用户 / synthetic 消息，不手动触发另一个 execution。
- 在 hook 交还宿主前同步决定最终 delivery；不要在这次决定后再 await 可能改变终结状态的操作。
- 无在途 / accepted 终结动作时保持正常输入行为；失败工具不留下永久排队标记。
- 暖重绑继续看到同一个在途 / accepted 状态；system 回滚不继承 tail 专属路由。

### 6.3 guidance 的时机

当前 prompt hook 会立刻把新文本写为 note。排队后仍这样做，会通过 memory 把待处理指令提前泄漏到旧收尾请求。

- tail 的用户 guidance 改为在**实际投递**后才尝试写入正确会话 / 工作段，不能在入队时写进旧轮。
- 第一步若证明 prompt hook 在实际投递时重跑，可直接在该阶段写 note；否则使用原生 messageID 关联已接收 prompt 与 inbox.delivered，在既有事件路径中做一次延后写 note。
- 所需进程内记录只承载尚未尝试记账的 guidance 信息，复用 GUIDANCE_LIMIT；暖重绑共享，投递 / 清理后移除。它不负责投递用户消息，不安排重试轮，不成为新的消息系统。
- note 使用稳定的 session + 原生 messageID operation ID，防止重复事件产生重复 note。写 note 失败不能阻止原生用户消息投递，也不能回写已结束的旧工作段。
- 已结束节点可能没有可写工作段；维持现有 guidance 的 best-effort 边界。用户原文仍由宿主持久化并在下一轮提供，不能因为无法记 note 就丢掉用户输入。
- system 模式和无关会话保持原有行为。若无必要，不改它们的 note 时机。

验收必须检查旧轮最终 wire body 的全部 system / messages，而不只是 messages：排队文本既不能作为 user 消息进入旧轮，也不能从 recent notes 等上下文提前进入。

## 7. 修复 C：收尾改动在最后一次 await 后提交

问题是本地 finishing 变量在异步等待后仍被当作事实。只在节点前面补 waitEvents，或者只比 execution token，都不够。

1. 主协调和节点使用同样的执行边界检查，初始先处理已到达事件。
2. 记录本次准备所属的 attachment、execution、marker 对象身份以及相关 session 的事件链状态。
3. 保持原有 fresh memory、observeEvidence、delivered 调用与原始历史契约；不要在这些 await 之前清空工具或添加强制收尾指令。
4. 所有异步准备完成后，再等待已排队事件，并同步检查：attachment 仍有效、execution 未变、marker 仍为同一对象、没有追加的未处理 session 事件，也没有事件 gap / failed event 导致边界未知。
5. waitEvents 可能只等待调用时那条 Promise。返回后须检查事件链身份，不能默认等待期间新追加的事件已经处理。不要靠 sleep 或无界 drain 循环解决。
6. 边界稳定，且权威状态仍匹配：在一个没有 await 的同步尾段中应用 no-tools、完整系统收尾指令、tail / cache hint 决策并返回。
7. 边界改变 / 未知：不提交旧 wait / finish 指令，不按旧授权恢复写权限；本次使用完整系统安全上下文、无临时 tail / 新 placement hint，并限制为现有安全只读交集。下一次稳定请求再按新 execution 的真实状态准备。
8. 不在同一次 hook 中递归重新准备、复制整个 transcript、恢复一份未授权的原始工具集，或重放 delivery RPC。canonical 全文和原始 history 的校验语义保持。

保证范围是：hook 返回前已经可观测到的执行 / 事件变化不会被旧收尾决策覆盖。hook 返回后发生的取消和新输入仍由宿主执行模型处理；不能宣称这个插件提供了跨进程原子发送。第一步与最后宿主 smoke 要覆盖这个边界。

## 8. 回归与验收表

| 类别 | 必测结果 |
|---|---|
| 主协调 wait + 用户文本 | 当前执行收尾，原消息原生 queue，下一轮收到原文，没有旧 wait 指令 |
| 节点 finish + 用户文本 / 附件 | 原节点只提交原总结；新消息下一轮可回应，不能自动重新打开旧工作段 |
| 排队 guidance | 旧 wire body 中无待处理文本；实际投递后若工作段可写，note 恰好一份 |
| 临时 drain 错误一次 / 重复错误 | accepted marker 保留；故障时无工作权限，恢复后继续收尾 |
| 权威状态明确不匹配 | 只清理被核实过期的那个 marker，不删除较新 marker |
| memory / evidence / delivered 中晚到结束、开始或 rebind | 不提交旧 finishing 指令、不错误授予工具；完整全文与回执规则不放宽 |
| 等待事件期间又追加事件 | 检测到未稳定边界，走安全路径，不假称事件已 drain 完 |
| 接受前窗口 | wait / finish RPC 被可控阻塞时，普通用户输入也不会进入旧轮；失败后不留永久在途状态 |
| 并行 / 混合工具组 | query / wait / finish 的实际合法组合保持完整 call-result 配对；一个失败调用不抹掉成功屏障 |
| 通知与 wake | resume:false 报告不进入用户排队逻辑，wake 不重复、不抢跑提交、不清掉当前仍有效的屏障 |
| warm / system rollback / cleanup | 按既有约定继承或清理，不残留 guidance / 在途登记，不复活旧 marker |
| system / blind / unrelated | 权限和隔离不变；无 tail 专属 queue，无广泛授权 API 修改 |
| 已有 placement 约束 | 不修改宿主消息数组；tail 请求仍最多四标记；收尾无临时 tail / placement hint；所有请求不持久化 tail |

测试使用可控 Promise / 屏障安排时序，不用概率性长 sleep。旧 bug 断言应在基线失败、修复后通过。必要的反例检查用临时源码副本，不反复改动正式工作树。

## 9. 提交顺序与工作量

按依赖顺序开发，不并行改同一个 injectContext：

1. **契约与回归**：给现有本地宿主 probe 增加 queue 情景，落地上述缺陷回归；确认 user / synthetic、enqueue / delivery 顺序。
2. **授权与异步提交**：修复 A + C，保留 accepted marker，将终结 overlay 移到稳定检查后的同步尾段；运行 focused 回归。
3. **输入交接**：修复 B（含在途窗口及延后 guidance），完成真实隔离宿主的 wait / node finish / 后续用户交互验证。
4. **验证与记录**：更新 review-response、用户说明和 probe 摘要；说明“收尾期间普通消息排到下一轮”，保留 native gate。最终运行一次完整检查。

估算（以第一步验证原生 queue 可用为前提）：

| 内容 | 预计新增 / 修改量 | 工作量 |
|---|---:|---|
| TS 生产逻辑 | 约 100–180 LOC | 0.5–1 个工作日 |
| Bun 回归、Python probe/checker | 约 180–320 LOC | 0.5–1 个工作日 |
| 文档 / 精简证据 | 约 40–80 LOC | 0.2–0.5 个工作日 |
| 合计 | 约 320–580 LOC | 约 1.5–2.5 个工作日，另留一轮审阅 |

生产量略高于原计划约 150 行的审阅提醒时，必须单列原因：在途窗口和 guidance 延后不能只靠删除一行 marker 清理解决。不因估算删掉必要竞态测试；也不引入 Python 控制协议来省 TS 设计。若出现新控制类、持久消息队列或超过约 200 行生产新增，先解释具体 host 缺口并重新审查范围。

## 10. 最终交付门槛

- 定向回归通过；两种 placement 的隔离宿主 smoke 通过；tail 覆盖 wait、node finish、队列新输入、真实终态与下一轮。
- `PYTHONDONTWRITEBYTECODE=1 make test` 通过，记录实跑结果，不预填数量。
- 保留 specialist 指纹检查。若实际 diff 触及 specialist stop / summary / busy 或其观察接线，按仓库 skill 执行对应 drift / TLC 流程；不能用 make test 替代。
- 默认 system，正常研究仍使用 system；不运行付费模型，不自动更新本地已安装快照，不重启共享服务，不合并 / 发布。
- 本次计划只准备本地文档。实施、提交代码及更新 PR 应另按用户后续指令执行；不要把“计划完成”写成“缺陷已修复”。

## 11. 实施记录（2026-11-04，2026-11-05 复核）

- `plugin/index.ts`：主协调与节点统一先处理已到事件，记录附件、执行 token、marker 对象和事件链版本；`memory` / `delivered` / `observeEvidence` 全部完成后，在无 `await` 的同步尾段检查边界并提交工具集、收尾指令和 tail 决策。边界改变或未知时改用完整系统的只读请求。
- marker 比较覆盖“准备前后”两个时点，并在准备结束时**重新读取共享 map**：包括“开始时没有 marker、等待期间新装了 marker”的变化。删除只针对本次检查过的对象，删除后若有更新的 marker 出现，同样按边界改变处理。
- 节点授权临时失败不再删除 accepted marker；该请求保持完整系统上下文、无 tail、只读，下一次稳定准备重新核对 `node_finish_pending.turn_token` 并继续原收尾。重复失败、暂停／预算拒绝不解除屏障。
- `plugin/src/controller-lifecycle.ts`：新增两个可选运行时字段：按调用登记的 `terminalCalls` 和待记账的 `queuedGuidance`；都随现有 session cleanup 清理，并随 warm 重绑共享。
- **修复 B 只做到部分缓解，仍被阻塞。** prompt hook 在 tail 模式的主协调 / 节点、当前执行存在在途调用或 accepted marker 时把 `delivery` 设为 `queue`；在途调用只匹配当前执行，纯附件消息同样排队，guidance 只在有文本时记录、并在投递后且执行切换时写入一次。但宿主在**当前执行的下一个步骤**投递 queue：这一步仍属于收尾执行，节点结束后也没有后续执行。因此排队消息没有保证得到正常处理，只有“不并入收尾请求”这一半。
- `plugin/index.ts`：携带已投递排队消息的那一步不再沿用旧收尾指令，改为“本次执行正在收尾、无工具、用文本回答该消息”的指令；工具仍然不暴露，收尾结果仍在该执行结束时提交。这不等于正常处理，只避免旧指令继续覆盖该请求。
- “已投递的用户输入”与“需要记账的 guidance 文本”分开跟踪（`queuedInputs` 与 `queuedGuidance`）：纯附件消息没有文本、不写 note，但投递后同样进入 post-terminal 指令；warm 重绑共享，session cleanup 清理。
- `scripts/probe_research_run.py` 的 placement 检查把 post-terminal 请求显式算作 overlay：参与请求覆盖统计、完整 tool 配对和“无工具、无 tail”检查。缺少 tool result 的 post-terminal 请求现在会让 `tool_pairing_ok` 失败（`tests/test_context_placement_probe.py` 覆盖）。
- 验收改为“排队输入最终进入正常轮次”：同一会话中出现一个仍带该文本、暴露工具、且既非收尾请求也非 post-terminal 步骤的请求。协调 wait 经唤醒满足；节点 finish 不满足（`no_later_execution`），因此 `--queued-input-node` 必须用 `--expect-blocked` 记录阻塞，不能算通过。
- 回归：`plugin/test/plugin.test.ts` 新增 15 个 full-hook 用例并更新节点拒绝用例；覆盖删除标记、晚到边界、晚装 marker、在途窗口、旧执行迟到调用、纯附件消息、并行终态、已结束节点、post-terminal 步骤和 system 模式。在 `555b449` 的临时源码副本上缺陷用例失败，修复后全部通过。
- 宿主契约：`scripts/probe_prompt_handoff.py` 与 `tests/test_prompt_handoff_probe.py` 记录 queue 的投递点、ID／文本／附件保留、synthetic 不路由，以及 `handoff_gate` 的 blocked 判定。
- 隔离冒烟：协调 wait + `--queued-input` 通过（收尾请求无排队文本、下一步为 post-terminal、唤醒后进入正常轮次、note 一份）；节点 finish + `--queued-input-node --expect-blocked` 记录 `gate: blocked`、`reason: no_later_execution`、`guidance_notes: 0`。system 对照、普通 tail 与 compaction 边界冒烟不变。
- `PYTHONDONTWRITEBYTECODE=1 make test` 与专家生命周期指纹检查见 review response；未触及 specialist 收尾 / 摘要 / busy 接线，未重跑 TLC。

## 12. 阻塞项（修复 B）

- 缺口：OpenCode 2.2.5 的 `queue` 在当前执行的下一个步骤投递，不产生新的执行边界；插件不能撤销 pending finish，也不能自建队列、取消执行或重发用户消息。
- 后果：协调 wait 下，排队消息会在旧收尾执行里得到一个无工具步骤，随后由唤醒的正常轮次处理；节点 finish 下没有后续执行，消息只停在历史里。
- 重新设计前必须满足：宿主能区分真实用户输入并在**新的执行**里投递（或提供等价的“延后到下一轮”契约）；验收必须检查该输入进入一个暴露工具、无收尾指令的请求，而不是只检查文本存在。
- 未验证的替代方案（不得默默采用）：在节点结束后由插件重发一条用户消息（当前计划明确禁止重发 synthetic）；由插件接管 pending finish（用户已选择保留已接受的收尾）。
