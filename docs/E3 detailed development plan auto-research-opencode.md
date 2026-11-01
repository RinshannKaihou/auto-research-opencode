# E3 详细开发计划

日期：2026-11-01（Asia/Shanghai）。状态：本地实现及内部工程验证已完成，真实模型配对效果未验证，尚未发布。完成情况以文末台账和 `docs/evidence/e3-multiroute/` 的本轮记录为准。

## 1. 核验后的起点与边界

本次工作区为 `codex/e1-e2-exploration`，HEAD `84262f6`，源码 schema 15。该提交包含 E1/E2 三轮评审后的材料确认恢复、精确回执、包体去重、代际契约、无节点结果通知和恢复修复。交接中的 695 Python / 172 Bun 是历史记录，本轮不直接沿用为通过结果。

开始时已有主设计、路线图、实施计划及归档的未提交修改，另有 `.review/`、`.runtime-dispatch-repair/`、评审文档和证据等未跟踪文件；全部保留，不清理、不回退、不覆盖其既有内容。使用当前 checkout，不另建 worktree，不改共享 OpenCode 或真实研究库。

实际源码优先于宏观文档中的过期实施状态：E1/E2 已完成开发；旧 R1–R4 完整路线图废止。严格确认性验证、MCTS、固定分支数、强执行来源、通用存储优化和第二套调度器均不在 E3 范围。

复用入口：`exploration_store.py` 的追加式 scope/route/decision/result/binding；`workflow_intents` 的执行意图；`runtime_budget.py` 的预算、启动认领和退出核实；既有普通节点、固定输入/发布、通知/唤醒；`evidence_read.py`、`evidence_conclusion.py` 的同源依据、持久草案、材料及决策包交付；现有三入口看板。

真实缺口及不变量：

| 实际单范围假设 | E3 修改 | 必须保留 |
|---|---|---|
| 每代一个 `_current_scope` | 多个本代 scope，保留旧字段作为兼容投影 | 不伪造历史探索，不覆盖 R2 审阅 scope |
| 一个 decision 的一个 next_work，范围内串行 | 一个 decision 可提交有限工作批；候选与已分配工作分别呈现 | 所有工作仍经普通 task/claim/runtime 授权 |
| 工作指纹包含全局路线状态 | 选路显式绑定本 scope；执行绑定所选路线及固定证据 | 相关撤回使工作失效，无关 scope 不取消工作 |
| 结果只向最近 scope 传播 | 同一事实向各受影响 scope 投影，仍是同一 source identity | 不复制计费、不把投影当独立证据 |
| 全局上限主要检查实际观测，缺少跨路线分批预留 | 扩展既有预算预留账本并关联工作；公共工作只预留一次 | 估计、实际观测、未知分列，unknown 不计零 |
| 组合只是建议 | 显式关系、组合新实验、固定回退输入、恢复理由 | 不相加收益，不对共享目录 reset |
| 最终依据只列最近 scope | 项目结项覆盖本代所有 scope、处置与遗漏 | 缩小范围不清除已知风险，旧草案不能提交 |

## 2. 领域身份、版本与状态

仍使用 `exploration-v1` 和按 generation 生效的 `evidence-v1`；E3 能力另在 capabilities 声明 `multiroute-v1`。新库/写迁移到 schema 16；版本号不代替协议能力检查。

scope 保留追加版本，增加 `parent_scope_ref`（可空、本代固定引用）、`state=active|deferred|closed`、原因及恢复条件。父范围是上下文关系，不是执行 barrier；不允许循环或跨代冒认。状态变化不删除路线、结果、风险，也不证明进程退出。关闭/暂缓后重新激活须给出条件变化与依据；目标/约束/比较口径改变推进 goal_version，继承路线必须重新确认适用性。

route 继续归属一个 scope，可跨多个节点；binding 保持多对多。增加固定引用的关系 `competes_with|depends_on|complements|combines`，关系随 route revision 保存，必须引用存在的精确路线，不当成知识证据。组合 route 保存 component refs 与冻结 baseline/inputs；不得把未发布 workspace 路径当固定输入。跨 scope 关系用于解释，不赋予跨 scope 修改权限或隐式执行依赖。

路线处置与执行状态、假设评价继续分离。`untried` 是待选候选；`investigating` 表示已有投入历史，不等于当前正在运行。当前有预算的工作由 intent/task/reservation 推导。暂缓/排除路线恢复时须显式 decision，提交新条件、理由和 evidence refs；不由程序判断科学有效性。

main 唯一管理范围、关系、全局决策及预算分配；node_core 发布本节点证据和建议；specialist 维持只读及盲评边界。现有请求幂等、controller fence、expected revisions、固定发布、知识追加版本不变。

## 3. 选择、异步结果和执行

`research_decide` 保留单 `next_work`，新增互斥的 `work_batch`；一批可以包含共享探针和各自深化工作，每项都有唯一 work key、route refs、固定输入、分配和资源要求。后端生成稳定 `intent_ids`，旧 `intent_id` 返回首项以兼容单工作调用。一次 decision、路线新版本、消费结果、预算预留及新 intents 在同一 `_mutate` 事务提交；任何校验失败全部回滚。

选择记录增加 `provisional`、`expected_results`（稳定 intent/task 身份）、received result IDs、missing results、`revises_decision_id`、比较声明。后端核对身份、scope、版本和结果是否实际到达；不根据第一条返回结果自动选赢家。缺失/取消/failed/unverified 单独表示，不能转为假设反证。临时选择可以继续有限低风险工作，迟到 publication/finish 仍保存为 pending；修订追加新 decision，不改旧选择。

选路快照使用显式 scope identity；单 scope 旧调用可以自动确定，多个 scope 时必须明确。项目结项使用全代聚合视图。执行意图保存本 scope revision、所选 route revisions、固定 evidence roots 和相关风险/当前版本指纹；同范围新增未选候选不使已批准工作失效，所选路线或相关科学依据变化仍需重选。执行依据不包含通知、usage 或草案自己的报告。

意图仍经 `exploration_materialize → propose/task → node_launch_claim → runtime_reserve → host submission`。共享探针是一项工作绑定多条路线，不为每条路线建重复节点。相同 operation 请求重放返回原意图；本代同范围重复 work key 显式冲突并返回原 intent 身份，复用其结果；追加投入须使用新尝试 key。

## 4. 共同预算、资源与公平性

不建立第二套费用账本。新增元数据关联现有 `runtime_budget_reservations`，表达一次 exploration work 的计划额度、持有/消耗/释放状态、intent/task 与资源请求；token/cost 实际用量仍来自 `usage_observations`。

首版硬约束采用用户明确配置的共同工作额度，单位与上限固定保存，可执行工作必须给出正数有限额度（默认 1），零额度不得绕过共同上限。它是投入上限，不伪装成精确 token、GPU 时间或货币硬上限；各自已有 runtime node/turn/deadline/observed token/cost 门禁同时生效。没有配置的维度显示未配置。相同共享探针只预留一次；追加投入产生新工作和新预留，不能通过 route 改名退款。

预留在选择事务完成；明确未启动、stale/off/superseded 可以释放。已发送、未知提交或退出未核实保留占用。完成后的实际观测与估计分列；无法归属的实际用量标 unknown，不按零释放。若实际计量不能支持某维度，只能报告计划投入约束，不能宣称费用硬封顶。

资源使用命名容量（如 `gpu:0`、`cpu:measurement`、`service:baseline`），启动认领事务原子占用，容量不足保留 queued/waiting_resource；不把争用当实验失败。只约束声明的受管测量，目录隔离不等于设备隔离。未核实退出不释放资源；明确未发送可释放。资源释放与已确认的执行退出绑定，外部进程无法确认时保留核对入口。

沿用队列顺序并按 scope 轮换选择可启动工作；被资源阻塞的项不阻塞无关资源。保留等待顺序/原因及候选索引，提供每范围投入与待处理结果计数。科学优先级由模型给理由，不设置自动分叉、反证或探索比例配额；有限预算下不承诺每个候选都获得投入。

## 5. 比较、组合与恢复

比较声明保存固定基线、workload/data、metric、unit、environment、repeats 和 uncertainty。程序只核对已声明条件及结果身份，任一关键字段未知/不同则返回 `incomparable` 和差异，允许校准/暂缓，但不能保存“可直接排名”的选择。多目标可以保留适用范围，不强制唯一赢家。

组合工作必须选择组合 route、引用组件精确路线及已发布固定 inputs，并实际新建普通节点重新测量。记录组合实验结果后可恢复 A 或 B；恢复工作以所选有效冻结 baseline/产物作为输入，不修改其他路线 workspace。退化结果保留，不能以 supersede 或 scope close 隐去。条件变化后的重新激活保留原负结果及旧 goal_version，说明新条件如何改变原判断。

## 6. 多范围证据、停止与大包

项目当前核心仍由显式 claim refs/roots 定义，不把所有路线说明自动升级为科学依据；最终 view 另外汇入本代所有 scope、路线、pending results、缺失结果和处置历史。active/deferred/closed 均在可穷尽索引和附录中保留。新 scope、结果、关系或处置影响最终 basis，旧 draft 失效；无关 scope 的变化只不影响局部执行，不能因此绕过项目最终综合。

最终 stop 须逐 scope 说明当前回答/停止范围、未做工作、残余不确定性与恢复条件；原单 scope stop 兼容。pending results、活跃路线、未物化意图继续使用原后端门禁。缩小核心集合沿用 reason/excluded_impact，再把本代排除历史纳入冻结依据和附录；撤回与挑战不因 scope 关闭而消失。

默认决策包保留 32,000 UTF-8 字节上限及旧拒绝回归。显式 `delivery_mode=paged` 才采用大包分页交付：固定一个不可变 canonical body，manifest 保存全部页面的范围/摘要、总字节数与总摘要；单页不超过 16,384 字节，总包设明确安全上限，超限显式失败，不裁剪风险。仍须在结项当前请求交付完整包的所有字节，不能仅送 manifest，也不跨请求累计决策包授权。重启/缓存淘汰从持久草案恢复，缺页/混 draft/错 digest/跨代回执均拒绝。原材料继续按 E1/E2 固定身份跨请求累计。该显式协议扩展不改变 evidence-v1 的代内不可降级契约。

## 7. 持久化、兼容与恢复矩阵

schema16 只增量增加分配/资源所需关联，scope/route/decision 扩展优先保存在现有版本化 data。迁移沿现有备份、BEGIN IMMEDIATE、完整性及旧值核对模式；失败回滚，可重试。只读 schema9–16 不迁移；schema15 及以下无 E3 元数据时投影为 legacy/unknown，不补造预算、比较或探索历史。旧单 `next_work`、Legacy 结项及历史成功请求重放继续有效。

| 中断/变化 | 行为 |
|---|---|
| decision 成功但响应丢失 | 原 operation 重放，无重复预留/intent |
| materialize 前重启 | 冷启动暂停，显式恢复同一 intent |
| 选择后证据撤回/路线变化 | 未启动工作 settle stale + durable result/notice，释放仅明确未用部分 |
| 暂停/关闭 | 停止新增授权；关闭结清未启动意图，已授权执行如实收尾 |
| unknown host submit/exit | 保留执行、预算和资源核对；不自动重跑 |
| 通知交付后、wake 前重启 | 沿原 notice/wake 账本恢复，不重复结果/工作 |
| 迟到/重复 callback | 幂等 source identity，不能覆盖较新状态；结果仍可触发修订 |
| 新 generation | 旧历史可读；不继承已读、完成或预算余额为新科学结论 |
| exploration off | 不降级本代 evidence-v1，保留 defer/propose_stop 收尾入口 |

## 8. 模型接口与看板

复用 `research_scope`、`research_route`、`research_decide`、`research_query`、prepare/publish/conclude。工具只增加当前决定必需字段；预算配置/退出核实继续走现有宿主控制边界。capabilities 同步 backend 与插件，缺能力明确拒绝，不静默用旧包运行新协议。

主上下文展示 active scope 摘要、当前投入、pending/expected/missing、资源等待、临时选择与强替代；其余只给总数及稳定分页索引。模型不反复抄全局路线表。盲评不注入全局排名。看板继续概览/研究/成果，研究列表按 scope 标注，详情展示关系/固定基线/历史，概览展示活跃范围、预算覆盖和等待原因。

## 9. 可审查纵向切片与验收

| 切片 | 跨层交付与依赖 | 验收 |
|---|---|---|
| S0 基线/计划 | 状态核验、源码/修复核对、本文与基线清单 | 保留已有修改，明确 E1/E2 已完成及效果边界 |
| S1 多范围与局部依据 | scope/route 版本、关系、全代分页；evidence scope 过滤；变更传播 | A/B 同时 active；改 B 不废 A；相关撤回仍废 A；只读索引穷尽 |
| S2 批量投入/共同预算/资源 | schema16、既有预算预留关联、work_batch、claim 原子资源、队列公平 | 并发不超共同额度；共享探针一次扣账；unknown 保留；争用排队 |
| S3 异步比较/组合恢复 | expected/missing/provisional/revises、可比性、组合固定输入、恢复理由 | 快结果临时选择、迟到修订；不可比不能直接排名；组合退化可回冻结单路线 |
| S4 多范围结项/显式大包 | 全代 scope stop、排除历史、basis 原子重算、paged manifest | 关闭/缩小不洗风险；旧 draft/缺页拒绝；大包完整多页后可结项；旧32k回归保留 |
| S5 工具/上下文/看板 | 插件 schema、engine 多 intents、摘要/索引、三入口投影 | 普通节点执行、无新权限；无关工具不因确认失败卡住；看板可查暂缓路线 |
| S6 隔离验证/交付 | 本轮全量、sync、迁移、独立包、实际 OpenCode provider、模型评估任务/运行评分方法 | 记录实际版本/命令/工具结果；不改共享宿主或原研究数据 |

S1 为 S2/S3/S4 的共同依赖；预算实现与证据大包可在接口约定后并行。S5 随纵向切片接入，不能以只有后端测试宣布完成；S6 在相关切片测试通过后运行，不无理由重复全套。

最终必要检查：针对性 Python 与 Bun，完整 Python/Bun/TypeScript，`sync:python` 后源码/内置 Python 一致，15→16 及旧库迁移/readonly/导出，独立包不依赖仓库源码，隔离真实 OpenCode 时序。

宿主端到端至少覆盖共享一次探针→快慢异步选择修订→一次组合退化及恢复→完整各范围停止结项；另独立验证多个活跃 scope。记录实际合成工具结果，fakeHost 只作为另一层证据。故障注入包含未物化、暂停/off、重启、重复通知及不确定执行。

## 10. 效果评估与发布判定

E3 评估任务补齐可执行测量入口和隔离生成器，覆盖共享探针、分布反转/异步迟到、不可比、组合退化、暂缓后条件变化、单路径。研究者只见任务和测量入口，独立评分材料留在评估侧；冻结相同 provider/model/variant、上下文、工具、环境、预算，配对比较 E1/E2 与 E3，保留失败/中断及 unknown 用量。

当前 E1/E2 评估记录未固定或授权具体真实模型与额度，因此先交付可运行任务、命令和评分方法；检查现有明确授权，如无可运行配对预算则真实模型效果标“未验证”，不擅自启动付费服务。评分关注答案、前提校验、有效转向、限定、无效投入和停止理由，不给分支数量奖励。

分别报告：①工程契约；②实际宿主+模拟 provider；③真实模型质量/成本；④发布。计划编写时尚无推送、PR、合并、发布或共享服务切换授权；本地实现与打包不等于部署。后续 PR 按用户明确指示提交。

## 11. 实施台账

- [x] S0：完整交接、基线/修复与主要源码核对；保存本计划。
- [x] S1：多范围、局部依据、关系及分页。
- [x] S2：共同预算、批量工作、资源与公平。
- [x] S3：异步比较、组合与恢复。
- [x] S4：多范围停止、风险历史、大包。
- [x] S5：插件、上下文及看板。
- [x] S6：本轮隔离工程、宿主、独立包与评估交付。

关键设计若因实际源码/测试需要调整，先在本文追加原因、兼容影响及对应回归，再实现；不删断言或放宽科学契约换取通过。

### 实施中的明确兼容调整

- E3 执行指纹不再因为新增未选候选失效。原 PR11 无节点 stale/wake 回归改为修改**已选路线的关键前提**触发，保留原有 stale、无假 task、持久结果/通知及重启唤醒的全部断言；另增候选不废工作回归。这是依赖粒度修正，不放宽相关撤回门禁。
- 最新迁移幂等检查从 schema15 迁移函数改为 schema16，旧迁移函数继续拒绝错误版本，不把任意未来库当作成功。
- 升级前尚未提交的草案可按原文读取；新增全代依据字段后通常需重新 prepare。旧成功结项/请求重放不变。schema15 及以前所有未启动意图因视图摘要格式升级，沿原 settle stale/result/notice 机制以 exploration_basis_format_upgraded 要求重新选择，不自动执行旧授权，也不丢失候选或结果。

### S2 实施补充：正数工作额度

实现核对后将可执行工作的 `budget_amount` 限定为有限正数，默认 1；零投入申请会绕过共同额度，因此拒绝并保持事务无副作用。纯候选登记仍无预留，不用零额度伪装成免费执行。回归见 `test_zero_work_units_cannot_bypass_shared_budget`。

### 最终选择与兼容说明

- 可选 candidate_key 表示同 scope 的稳定机制身份，重复身份显式冲突并指向现有路线；不自动判断语义近似。work key 防止本代同 scope 重复分配，追加投入使用新尝试 key。
- 全代依据保留 scope/route 历史版本已移出的科学引用及移出理由，沿同一有界图检查；局部工作指纹不纳入无关历史，避免新候选／范围使所有执行失效。
- 共享探针由一个 scope 拥有并绑定其多条路线，其他 scope 复用同一固定结果引用；不存在跨 scope 重复计费或复制为独立证据。
- 停止与 deadline 在现有控制事务内结清未物化及 queued 工作并释放明确未用预留；已开始／退出未知保留资源，普通软暂停不结清。
- 工程结果、真实宿主／模拟 provider、评估设施、真实模型效果与发布状态分列于 [本轮记录](./evidence/e3-multiroute/README.md)。S6 的真实模型交付为可执行任务和运行／评分方法，配对效果仍未验证；不把该项工程完成写成效果改善。

### 后续核验：执行输入也纳入依据指纹

已复现原 `scope-work-v1` 指纹只覆盖证据快照根、范围及选中路线，而未覆盖工作本身的 `inputs`、前驱 `input_refs`、`anchor_ref` 或 `question_ref`：这些固定输入的支撑被撤回后，尚未物化的工作仍可能启动。新意图使用 `scope-work-v2`，在保存选择时记录规范化的全部工作输入引用，物化及启动认领时共同重算其支撑图和原有范围／路线指纹。相关输入变化须沿既有 stale、结果、通知及预算释放路径处理；无关结果和未选候选仍不使工作失效。

PR #12 review 纠正：schema15／pre-E3 的真实意图没有 basis_mode，旧全局 view 的摘要与 E3 新布局不兼容，所有未启动工作统一以 `exploration_basis_format_upgraded` 结清 stale 并要求重新批准，而非声称科学依据改变。`scope-work-v1` 只在 E3 开发首轮存在，开发快照的输入未绑定时要求重选；输入全部在根中的 v1 可沿原开发期计算复核。已授权或退出未知的执行继续按原运行控制核实；历史 decision 和成功请求重放不重写。真实旧格式回归夹具由 main `31fd90d` 的 NativeService 和 view builder 生成，不用 v1 冒充 schema15。验证同时覆盖直接输入、前驱输入及 anchor 在物化前和启动认领前的撤回。

### 后续核验：持续跟踪异步缺失结果

已复现临时选择列出的 `expected_results` 在下一条选择省略该字段时会丢失，导致取消或失败的慢路线结果不再出现在最终停止说明。后续选择自动继承同范围上一条选择中仍缺失的预期工作，重新检查其已发布结果及实际 task 状态；新证据到达后自然解除缺失，取消、失败和未核实保持缺失，不能成为反证。带缺失结果的继续投入仍须明确 provisional，defer／propose_stop 可保留原因并停止，结项决策包及附录保留尚缺条目。

本修改不要求每条路线都执行，历史 decision 不改写；已完成的旧选择只在本代再次决策时向前传递其仍缺失的预期。明确的 defer／propose_stop 对当时缺失的工作保存决策 ID 与理由；以后可以继续其他工作而不永久强制 provisional，但缺失及其处置仍保留在选择、结项包和附录中，直到对应结果真实到达。旧自由文本 `missing_results` 在省略时沿用，显式重新声明仍由模型给出选择理由。

### 后续核验：显式未知条件不能视为可比

当两个结果都把同一比较字段填为 `unknown` 时，仅检查相等会错误地允许排名。空值、空容器和常见的显式未知标记均保留为不可比；不确定性也采用相同规则。调用方仍可登记不可比结果和安排校准；程序只识别明确标记，不声称能理解或验证任意自然语言测量条件。

### 后续核验：共享输入的审阅范围与多范围汇总

真实隔离宿主复验发现，共享输入的一个节点结束时，审阅义务会被提升并退出该节点范围；将这些纯范围事件纳入另一个节点的执行指纹，会错误地把同一批慢路线判为 stale。`scope-work-v2` 排除审阅历史中的 `promoted`、`reentered`、`out_of_scope`，继续纳入实际审阅、挑战、处置、知识版本和当前风险；既有 v1 指纹保留原计算语义。

审阅义务协调也从“最后一个 scope”改为合并本代所有 scope 中已投入／采用路线的声明引用，再与既有项目审阅范围、结项范围合并；不因创建第二个 scope 将第一个 scope 的有效依据移出审阅范围。回归覆盖共享输入的兄弟节点、真实审阅挑战导致 stale，以及双 scope 审阅义务同时存在。

### PR #12 review 后的契约补充

资源退出核实的权限属于当前 attached main，覆盖本项目历史终态任务，避免接管后失去释放途径。正常模型 finish 仍不证明外部进程退出；每项声明资源的工作需用户核实，后续阻塞保存去重通知与命令，空资源不形成租约。分页上限由开发期 1MiB 收紧到 256KiB，决策页默认 16KiB，仅第一页返回完整 manifest，仍须同请求完整交付；只去除重复工作计划，保留风险、比较、停止与缺失处置。具体条目和验证见 [review 处理记录](./evidence/e3-multiroute/review-response.md)。
