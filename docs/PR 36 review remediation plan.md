# PR #36 review remediation plan

2026-11-05。状态：已实施并通过本地检查；修复 B 仍为缓解，节点正常轮次交接继续被宿主契约阻塞；基线 `9424fb0`，分支 `cursor/prompt-cache-handoff-fixes`。

本计划承接 [terminal handoff repair plan](<./Prompt cache terminal handoff repair plan.md>)。仅修复两份 review 的运行时问题与验收缺口；不把 B 的正常轮次交接标为完成。

来源：

- [Opus review](https://github.com/RinshannKaihou/auto-research-opencode/pull/36#issuecomment-6637350561)。
- [合并门审查](https://github.com/RinshannKaihou/auto-research-opencode/pull/36#issuecomment-6637660678)，正文注明交叉审阅模型为 volc/kimi-k3。

当前 head 的 CI 已通过。源码和指纹核对不能代替新实现的测试；不得预填新的通过数量。

## 1. 修复范围与决定

| 意见 | 决定 | 本轮验收 |
|---|---|---|
| 登记终结调用前的新输入仍被旧指令压住 | 必须修；模型生成期也要覆盖 | wait / finish 的生成期、ensureContract、runTool 前置等待、服务接受期都覆盖 |
| 不稳定边界将 system 和普通 tail 请求裁为只读 | 必须收窄到 tail 终结上下文 | 非终结请求工具与基线相同；原有授权防线不变 |
| post-terminal 状态跟随整个 execution | 与输入来源关联一起修 | 同执行内再一次 wait 不重复处理已经可见的旧输入 |
| 三处 kind 断言不能验证 marker 存活 | 合并前补强 | 删掉 marker 的受控变异使相关用例失败 |
| 取样早于协调唤醒、阻塞索引会串会话 | 修复探针的完成条件和请求关联 | 人为延迟唤醒、插入并发请求仍给出正确结论 |
| queue/steer 缺少对照 | 补本地对照后再决定是否简化 | 不凭“都在同执行”就删 delivery 改写 |
| guidance best-effort、等待延迟、投递标记异常 | 明确边界，补必要反例 | 不将记账失败变成授权成功或自动重发 |
| probe docstring 与实测矛盾、用户操作建议缺失 | 本轮修正文档 | 明确 same_execution、B blocked 与可用的暂停/协调入口 |

用户既定选择不变：保留 accepted terminal action，不撤销 pending finish，不自行重发用户消息。

默认 system 不变；不新增 Python RPC、schema、消息队列、后台重试 worker、provider registry 或付费验证框架。节点既有总结提交时点、完整 tool-result 配对、checkpoint/evidence 原文匹配、盲评隔离均保持。

## 2. 第一阶段：先证明“输入—模型步骤—工具调用”的关联

### 目标

终结调用入口的登记无法覆盖模型尚在生成 wait / finish 的阶段。需要知道：**某条真实用户输入是否在生成这次终结调用的模型请求之后才到达，而尚未被后续正常模型步骤处理。**

不能仅比较 execution，也不能把终结 RPC 返回时看到的最新输入当作生成期快照。

### 最小宿主契约验证

扩展现有私有 XDG / loopback provider 探针，记录以下元数据与顺序：

- 真实 prompt 的 messageID、delivery；对应 inbox.delivered 的 ID / durable sequence。
- session.step.started 的 assistantMessageID；context、model.request、最终 provider body 的顺序。
- 工具执行的 call.messageID、call.id，及其是否与上述生成步骤严格对应。
- 纯附件输入；resume:false 报告、wake 等 synthetic 输入；同一会话的重复准备与失败重试。

探针分别在模型生成期间和终结工具开始前阻塞，再发送真实用户输入。用最终 wire body 确认该输入究竟在哪个模型步骤首次可见，而不只看 hook 到达时间。

类型里存在 assistantMessageID 不代表关联已经成立。尤其要验证异步 event 消费晚于 context 时，是否仍能可靠关联。生产 context 没有 assistant ID 时，只能在探针证明主请求严格串行、步骤关联唯一的条件下做小型关联；不允许直接拿“最后一次 context / lastKind / agent 名称”猜测。若契约不够，记录缺口并停止这部分实现，不在生产 HTTP body 上注入 ID 或添加宿主协议。

### 通过后的最小数据原则

- 使用原生 ID 和经验证的顺序，不使用墙钟排序、不扫描任意 user 角色文本判定来源。
- 记录真实输入的来源不能以 terminalWindow 已存在为前提，否则前置窗口依旧不可见。
- 为生成终结调用的模型步骤保存其已经可见的输入边界；marker 绑定生成步骤/终结调用，而不是只有 execution。
- pending、已投递、模型步骤已包含该输入不是同一状态；排队输入不能提前算已处理。
- 仅保存必要 ID / 边界，复用现有用户原文与 guidance 记录；不复制整份 transcript。
- 状态随 warm rebind 保留，按既有 forget 清理；旧调用不能修改新执行或新 attachment 的关联。
- 只清理已证明不再被活动步骤、在途调用引用的边界记录。不能借 guidance 的 best-effort 容量裁剪，静默丢掉仍需要的控制关联。

**交付：** 一份简短契约表、可复跑的本地命令和可控屏障 fixture。无真实凭据、模型请求或用户数据库访问；精简证据只保存 ID/顺序/判断，不公开真实提示词。

## 3. 修复 A：只读兜底只用于 tail 终结准备

文件优先为 plugin/index.ts；保持现有工具执行前的事件故障、节点授权和 controller 检查。

1. 显式计算 terminalContext：tail 模式，且准备开始或结束存在终结 marker，或存在当前执行的相关终结调用。保留准备起点的信息，不能只看最后一刻的 marker。
2. 只有 terminalContext 的边界不稳定时，应用新增的 full-system / read-only fallback，并移除旧 finishing 指令。
3. system 模式、无终结动作的普通 tail 请求不因这段新增逻辑被裁工具。原有节点未授权、blind、事件故障等限制仍然独立有效；不能把“工具不变”误做成绕过原有授权。
4. 安全降级应提供准确原因说明；不要承诺宿主必然自动重试下一步，也不要让模型把状态未知误当成功提交。
5. 一旦要应用终结 overlay，仍在全部异步准备后核对 attachment、execution、marker 和事件链，并在无 await 的尾段提交。保留 none→installed 的检查和 compare-and-delete。
6. 本轮不顺手重构通用事件处理。准确记录等待延迟；没有足够证据时不修改 specialist busy/wait 的接线。

验收：system 和 markerless tail 分别在“无控制含义的报告投递事件追加、但事件处理未及时完成”时保留基线工具；同样时序下 tail 终结请求仍安全降级。再测试事件 gap / failed event 时，既有执行前写权限检查继续拒绝。

## 4. 修复 B：前置输入窗口与 post-terminal 的生命周期

这是 B 的进一步缓解，**不是**解除节点正常轮次交接的宿主阻塞。

1. 根据第一阶段证明的模型步骤关联，捕获所有相关真实用户输入，包含 terminalCalls 登记之前的输入。
2. accepted wait / finish 后准备请求时，判断相对于该终结调用的生成步骤，是否出现了新的真实用户输入。
3. 新输入存在时选择 post-terminal 文本响应，保持 0 工具、完整系统上下文、无临时 tail / 新 placement hint。新输入不是撤销总结或重新授权工作的依据。
4. 没有新输入时才使用正常 wait / finish 收尾指令。不能把 synthetic 报告、wake、research_state 或旧历史里的用户文本误认成新输入。
5. 同一 execution 的后续模型步骤若已经包含并处理该输入，再生成一次 wait / finish，不重复要求回答旧输入。优先通过新的生成步骤边界自然区分，而非增加一个全局 delivered 布尔标志。
6. context 准备、重复准备、超时重试都不等于模型已回答。不能在第一次准备时消费输入标记。
7. 仍保留每次调用独立 token 的在途登记；仅提前 beginTerminalCall 不算完成本问题。不得改成整轮繁忙时强制所有用户消息 queue。
8. 纯附件与文本使用同一来源/步骤判断；只有文本参与 guidance note，不能再依赖 queuedGuidance 是否存在来决定 post-terminal。

新的 full-hook fixture 必须明确给出实际的 step.started / assistantMessageID 与工具 call.messageID 关系，不能依赖现有 host.call 自动递增的 msg_N 恰好代表同一生成步骤。

## 5. 修复 C：让测试和探针能证伪

### Marker 断言

逐一替换基线 plugin.test.ts 的 5312、5403、5494 行附近断言（实施时按测试名定位，不依赖旧行号）：

- 成功等待与失败兄弟调用：marker 必须存在，execution 正确，waiting 是该任务集合。
- 准备中安装的新 marker：安全降级后应保留刚安装的同一个对象。
- post-terminal：保留原 accepted marker 的身份和内容。

不得用 `currentMarker()?.kind === undefined` 区分有效 wait 与无 marker。临时源码副本中删除 marker，以上用例应失败；不要在正式工作树来回修改，也不要把变异做成新的常驻验证框架。

### 探针完成条件

- 不再以 task finished 作为协调输入交接试验的结束条件。release 后，等待目标会话出现带相应输入、非 finishing / post-terminal、具有当前允许工具的正常请求，并等待所需 note / 执行事件收敛。
- 使用单个有界 deadline；不增加固定 1–10 秒 sleep，不无限轮询，也不为每个子条件累加一份超时预算。
- 结果区分 passed、已知宿主结构性 blocked、超时/状态未收敛。一次采样没看到 wake，不能直接归因为 no_later_execution 的宿主限制。
- 节点场景在现有协议下继续 expected blocked，不主动构造后续用户消息让它“通过”。`--expect-blocked` 不能掩盖配对、marker、未知异常或其他验收失败。

### 请求关联

阻塞目标 provider 请求时就保存该具体请求的对象身份/索引，并关联目标 session 与 overlay 类型。释放前的全局 `len(bodies)-1` 不是目标请求身份。

在有其他会话请求插队的测试里，finishing_had_text、首次新输入请求、post-terminal、后续正常请求必须归属于同一目标会话的正确阶段。复用已有本地 instrumentation；不创建通用 wire-accounting 系统，不修改真实出站请求。

### queue / steer 对照

相同脚本动作、同一模型生成屏障、同一发送时点，分别使用 steer 与 queue；比较当前请求中断行为、输入首次可见步骤、execution 边界、附件和消息身份保留。

若没有观测到差别，只能说明该场景无差别，不能外推所有输入语义相同。简化 delivery 改写需单独说明证据和差异；不能以此代替前置窗口修复，也不强行把节点 gate 改为 passed。

## 6. 回归矩阵

| 场景 | 必须验证 |
|---|---|
| system / markerless tail 中途追加报告事件 | 工具与基线相同，无新增终结降级；原有安全拒绝独立有效 |
| tail 终结准备中执行 / marker 改变 | full-system 安全降级、不提交旧指令；新 marker 未被误删 |
| wait / finish 的模型生成期输入 | 真实输入不再被旧收尾指令压住，工具仍为 0 |
| ensureContract / runTool 前置等待输入 | 和模型生成期相同，不能只测 node_finish_request |
| 服务接受期输入 | 原有 queue / 在途回归仍成立；失败兄弟不抹掉成功屏障 |
| 新输入早于工具结果，但晚于生成请求 | 按来源/生成步骤识别，不依赖 history 的工具结果后扫描 |
| 文本 / 纯附件 / synthetic / 临时 tail | 前两者识别正确，后两者不误判真实新用户输入 |
| 输入已由正常步骤处理，同执行再次 wait | 不再给出回答旧输入的 post-terminal 指令 |
| 重复 context 准备 / 失败重试 | 不提前消费输入，marker 生命周期与回执规则不放宽 |
| marker 被删除的受控变异 | 加强后的相关用例失败 |
| 协调 wake 延迟 + 跨会话请求插队 | 在 deadline 内可靠通过或明确超时，不误报宿主 blocked、目标请求不串位 |
| 节点 finish，无后续正常执行 | 继续 expected blocked，节点原总结与既有工作段限制不变 |

用可控 Promise / 本地 provider 屏障安排时序；不把随机长 sleep 作为回归条件。

## 7. 文档、形式化范围与非目标

- 更新 plugin/README.md：收尾窗口发给节点的消息只能在无工具步骤得到文本回应，不能保证进入正常轮次；重定向工作可等待协调会话稳定后联系协调者，或使用已有暂停入口。普通消息不自动恢复已结束节点。
- 修正 probe_prompt_handoff.py docstring 的“执行结束后投递/新 execution”措辞，和实际 same_execution 证据一致。
- guidance 仍为 best-effort；原生输入持久化与 note 成功是两件事。记账失败不伪装为成功，也不扩大为持久重试队列。
- 保留 B blocked 和真实 provider 对“工具历史 + 无工具定义”的接受性门槛。不宣称科学质量、成本收益或默认启用条件已通过。
- TLA+ 仅覆盖 specialist child lifecycle 的已建模属性。它没有建模 waitFinishes、queuedInputs、用户交接或终结请求权限，因此不为本次缺陷提供形式化证明。
- 本轮保留 fingerprint 检查；若实际触及 specialist 的覆盖函数、摘要/busy 或相关观察接线，按已有 skill 执行 drift/TLC 流程。不要为本次修复新建另一套 TLA+ 工程。

## 8. 提交与验证顺序

1. **默认行为与测试纠正**：落地只读兜底 scope、解释文字和三处 marker 断言；运行 focused tests 与受控变异。
2. **步骤关联与输入窗口**：先提交私有宿主契约证据，再实现最小来源边界关联及 wait/finish 生成期、工具入口前回归；一并消除 execution 级 post-terminal 粘滞。
3. **探针与记录**：修复有界等待、目标请求关联；补 queue/steer 对照、文档及 review-response 逐项映射。稳定的探针是最终验收前置条件。
4. **最终检查**：`PYTHONDONTWRITEBYTECODE=1 make test`，两种 placement、early-input wait/finish、协调正常交接、节点 expected blocked、compaction/pairing 与 fingerprint；记录本机/CI环境、命令和实际结果。

生产代码以 plugin/index.ts、controller-lifecycle.ts 为主；必要时一个小型纯辅助函数。仅在已证明必须接入现有步骤观察点时修改 native-observations.ts，不改变它的 usage / checkpoint / evidence 行为。无须修改 Engine 的 nodeTurnAllowed API 或 Python 服务。

预估净新增/重写：生产 80–150 行，测试 200–350 行，既有 probe 80–160 行，文档 30–60 行；约 1.5–3 个工作日，另留一次审阅。若关联证据要求新协议、通用状态机或大量历史缓存，停止并说明具体宿主缺口，而不是扩大实现。

仅本次制定计划。后续实施、提交/推送、更新 PR 或本地安装按用户另行指令执行；本计划不授权合并、发布、共享服务重启或付费模型实验。

## 9. 实施记录（2026-11-05）

- 宿主契约：新增 `scripts/probe_step_association.py`。在 2.2.5 上确认 context 请求的 user/assistant 消息带原生 ID，工具 `call.messageID` 等于生成步骤的 `assistantMessageID`，工具执行时 transcript 已含该 assistant 消息，且真实 prompt 为 `user` 类型；hook 级计数不能证明请求生命周期串行，因此实现不使用串行假设；模型生成期与工具执行期输入都在下一个 provider 请求首次可见。queue/steer 对照：`steer` 把两条待处理输入并入下一步，`queue` 一条一步，两者都不产生新执行边界。证据见 [handoff](../docs/evidence/context-cache/handoff/README.md#model-step---tool-call---input-association)。
- 生产：终结调用只把生成它的 assistant message ID 写入 marker；收尾请求读取原生 transcript，找出该 assistant 消息之后到达的 `type: "user"` 消息，用这个生成步骤关联选择 post-terminal。不保存会话级输入边界，不依赖请求串行或 ID 列表，纯附件与文本同一判断。
- 旧 `9424fb0` warm marker 没有生成步骤：transcript 无法证明边界时保留 0 工具屏障，但改用保守的文本响应，不再断言“没有新输入”；无用户消息时仍用正常收尾指令。新增跨版本 warm-state 回归。
- 只读兜底：`terminalContext` 显式计算（tail 且准备开始/结束存在 marker，或当前执行有在途终结调用）；只有该上下文边界不稳定时才 full-system / read-only，并给出准确原因说明。system 和 markerless tail 保持基线工具与 placement；原有授权防线独立有效。
- 测试：`plugin/test/plugin.test.ts` 新增 10 个用例（wait/finish 生成期输入、旧 marker 保守处理、长队列不遗忘、较新准备不改旧步骤边界、已处理输入不重复回答、synthetic 不算输入、system/markerless 事件追加不裁工具，以及事件故障时既有写权限继续拒绝）；三处 marker 断言改为对象身份 + execution + waiting，受控删除 marker 的临时副本使三条用例全部失败。
- 探针：`probe_research_run.py` 记录被阻塞请求自身的索引，协调场景在单个 45 秒 deadline 内等待“正常轮次”，结果区分 passed / blocked / timeout；`--expect-blocked` 不能掩盖 timeout 或其他失败。
- 文档：修正 `probe_prompt_handoff.py` docstring 的 same-execution 措辞，plugin README 增加节点收尾窗口的使用建议；B 仍标阻塞。
- 实跑：`PYTHONDONTWRITEBYTECODE=1 make test` 见 review response；专家生命周期指纹无 drift。
