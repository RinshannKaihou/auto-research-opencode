# E1/E2 详细开发计划

日期：2026-11-01（Asia/Shanghai）。状态：E1／E2 已接入开发分支并完成工程回归；真实模型效果对照和用户统一本地验收尚未完成，部分评估任务还需可执行测量服务与配对运行设施。实现与验证范围见 [E1 工程记录](./evidence/e1-exploration/README.md)和 [E2 工程记录](./evidence/e2-conclusion/README.md)。

代码基线：`324f2ed2ad15e88dc6ab39a50f2a3762023f8379`，schema 12，已包含 R1/R2、运行控制修复和协调者执行边界。本文是[当前实施计划](<./Remediation implementation plan auto-research-opencode.md>)中 E1.1–E2.5 的接口与开发细化；语义服从[主设计](<./Exploration and multi-route management design.md>)，范围服从[当前路线图](<./Remediation roadmap auto-research-opencode.md>)。

## 1. 本轮完成什么

交付一个可以恢复的循环：

> 保存候选 → 选择一个有区分力的工作 → 普通节点执行 → 读取结果及当前依据 → 改变或保留选择 → 保存停止理由 → 发布有证据附录的报告 → 原子结项。

E1/E2 首先覆盖**每个研究代一个当前决策范围、多候选、顺序投入**。路线与节点保持多对多关联，允许一个普通比较节点评估两个候选；不实现多个活跃范围的调度、跨路线资源分配、组合实验管理或未来结果汇合屏障。组合可以作为建议登记，真正的组合管理留给 E3。

交付型任务可以直接执行，仅在出现决定方向的未知时探索；不要求固定候选数、固定反证次数、每个候选都执行或每个主张都独立复验。E2 也适用于单路径任务，不能为了使用新结项契约而先凑备选路线。

开发顺序是 E1 后 E2。各提交完成对应工程验证；面向用户的统一本地验收在 E1/E2 接通后进行，不把建表、提示词或单独风险预览当作完成交付。本轮规划不调用真实模型，不切换现有项目策略，不安装或重启共享 OpenCode。

## 2. 从当前实现到新增能力

| 当前代码事实 | 本轮处理 |
|---|---|
| `query_store.py` 的普通分页固定 `upper_id`，各 RPC 各自读事务；不承诺跨请求同一快照 | 新增针对决策依据的不可变读取快照；保留普通列表的既有语义 |
| `context_policy.py` 已计算支撑祖先、撤回依据、字段检查、当前影响与挑战 | 提取可复用的依据投影；上下文、查询和结项使用同一计算入口 |
| `publication_check()` 已把空知识引用判为 coverage unknown，但 `conclude()` 不采用它作门禁 | E2 将该未知纳入结项检查，不能由调用者填写 reviewed 绕过 |
| `review_lifecycle.py` 已有精确目标、审阅事件、挑战处置；coverage 仍为 `not_assessed` | 增加类型与实际执行身份，按声明需要推导覆盖；不重写历史事件 |
| `workflow_notifications` 与 `workflow_intents` 已处理通知、唤醒、启动与恢复 | 通知继续负责交付；新增待决策结果负责“是否已考虑”；执行意图复用原账本 |
| `node_finish_request/commit` 已区分结束请求、原生 terminal 和提交 | 在确认结束的事务里记录结果事实，不在模型提出 finish 时提前认定结果到达 |
| `runtime_budget.py`、controller revision、宿主提交链已限制执行授权与预算 | 新探针及恢复全部经过该链；不增加旁路调度器 |
| 目前 `research_conclude` 的审阅状态为调用者自述 | Legacy 记录照旧；新契约只返回派生 coverage，移除该模式下自填 reviewed 的输入 |

实现时以这些入口为起点，不对 `native_store.py`、engine 或 schema 作无关重构。只抽出确实需要在一个事务中复用的 propose/task/review 查询函数，不嵌套调用 `_mutate()`。

## 3. 启用、版本和兼容契约

### 3.1 三个版本分开记录

| 配置 | 建议取值 | 作用 |
|---|---|---|
| `context_policy` | 保留 `memory-v2` / `memory-r1-v1`；新版读模型增加能力标记 `exploration-context-v1` | 说明模型实际获得的上下文能力 |
| `exploration_policy` | `off` / `exploration-v1` | 是否使用持久选路与结果后的决策协议 |
| `conclusion_contract` | `legacy-v1` / `evidence-v1`，按 generation 保存、代内只升不降 | 选择后端结项契约；不由调用者请求参数决定 |

宿主 capabilities、保存 context、选择、结项与评估记录同时保存以上实际版本，以及插件/后端版本。context 能力来自实际加载的代码和渲染结果，不能仅凭 schema 号宣称支持。

### 3.2 用户入口与生效时间

计划在现有 `/research` 命令下增加：

- `/research exploration`：查看当前研究代的三个版本及阻塞原因。
- `/research exploration on`：E1 版本启用探索，并显式采用带探索块的 R1 读取；E2 版本同时将当前代升级为 `evidence-v1`。命令结果明确列出变更。
- `/research exploration off`：停止新探索编排，保留路线、待处理结果和科学风险；E2 的结项契约继续生效。已有执行不会因此被误报为已退出，暂停/取消仍走原控制入口。

E2 另提供 `/research conclusion-contract evidence-v1`，让单路径项目显式采用新结项契约，不要求启用探索；不带参数时只查看当前契约。采用操作不重算旧报告。prepare/conclude 不隐式采用，Legacy 项目请求新准备工具时返回采用说明。

已完成的旧项目只读浏览保持原语义；重新研究须先按现有方式创建新 generation。对未结项旧项目的采用操作记录采用时点与当前范围，不把此前节点反推成“已探索”。E1 项目升级到 E2 插件时保留其原契约，重新显式采用后才启用新门禁。

新项目默认保持旧模式。新 generation 在探索关闭时默认使用 Legacy 结项契约，在探索仍开启时使用 `evidence-v1`；本代可显式升级，不能降级。已结项项目可先关闭下一代的探索偏好，再创建新代。新代不继承旧代的回答或材料已读事实。

启用探索与所需读取能力在一次配置事务中生效；能力不满足就返回明确错误，不留下半启用状态。探索开启期间请求降回 Legacy context 被拒绝，并提示先关闭探索。已启用 `evidence-v1` 的当前代不支持切回宽松契约；关闭探索也不能通过旧 API 绕过。用户停止与预算暂停独立于以上配置。

以上命令已实现；用户入口保持统一，不增加同义入口。

## 4. E1.1：领域对象、存储和权限

### 4.1 最小持久对象

新增 `exploration_store.py`，承接以下五类领域表；交付回执表在第 5 节定义。沿用项目内计数器、事件与 `requests` 幂等表。

| 对象/建议表名 | 关键字段与约束 |
|---|---|
| `exploration_scopes` | `(scope_id, revision)` 主键；generation、task_kind、question_refs、objective、constraints、comparison_basis、baseline_refs、budget_policy、goal_version、reason、created_at、actor。每代至多一个当前 scope；revision 追加保存 |
| `exploration_routes` | `(route_id, revision)` 主键；scope_id、goal_version、title、rationale、critical_assumption、hypothesis_refs、baseline_refs、evidence_refs、next_probe、cost_estimate、reactivation_condition、disposition。修改追加，不覆盖旧卡 |
| `exploration_decisions` | decision_id；scope_revision、considered_route_revisions、basis_digest、source_sequence、result_ids、missing_results、chosen_action、route_changes、rationale、remaining_options、next_work、actor。全记录追加 |
| `exploration_bindings` | route_id/revision、scope_id/revision、node_id、role_in_route、decision_id；同一 node 可关联多路线，引用现有知识/发布而不复制结果 |
| `exploration_results` | result_id、scope_id、route_ids、source_key、source_ref、source_sequence、kind、payload、handled_by_decision。`source_key` 唯一；同一事实重复通知不新增待决策项 |

没有节点的路线只是候选，不占 node quota。假设评价仍用带范围的现有 knowledge 版本，路线卡引用它；不另建一套科学知识账本。scope 与 R2 的 `r2_review_scope` 分开，不能复用字段名覆盖审阅集合。

`task_kind` 为 `delivery | selection | discovery`，是当前范围的提示信息，不控制节点执行权限。`budget_policy` 保存计划/估计及原预算配置引用；E1 不赋予它独立扣账能力，实际配额仍由 runtime 决定。

新 scope 必填 objective/task_kind；constraints/comparison_basis 未知时明确保存未知，不让模型编造测量条件。新路线必填 title、rationale、critical_assumption，尚无证据时 refs 可以为空；选中工作时才要求具体 next_work 与固定可用输入。cost_estimate 为可带 unknown 的估计，不冒充硬预算。hypothesis_refs 可随后关联知识，不要求为每张想法卡先写一条知识。revision、goal_version、actor、时间和 source sequence 均由后端生成；目标/约束/比较口径改变才推进 goal_version。

### 4.2 状态与变更规则

- 路线处置取 `untried | investigating | deferred | excluded | adopted | combination_proposed`，与节点状态分列。
- 新建路线默认为 untried；普通路线编辑只改描述、引用和下一探针。处置变化通过 decision 提交，并创建新 route revision。
- excluded/deferred 必须有目标版本、原因和恢复条件。执行受阻、超时或测量不可比可以产生 deferred，不能由程序自动变成“假设已反驳”。
- 排除/暂缓不删除路线，也不终止已有进程；要停止实际工作仍需原控制链核实退出。
- 目标、硬约束或比较口径变化产生新 scope revision，并列出继承材料的适用性说明；旧决策不能据此自动支持新目标。
- 第一版不允许第二个活跃 scope 或子 scope。新研究代可显式继承旧候选的固定引用，但创建本代范围与选择记录，不复制历史完成/覆盖标签。

### 4.3 模型写接口

新增三个窄工具，均只开放给当前 main；operation id、host/session、generation、execution identity 由宿主注入。

| 工具 | 模型必要输入 | 结果 |
|---|---|---|
| `research_scope` | 新建时 objective/task_kind；修改时 scope_ref、expected_revision、changed fields、reason | 当前精确 scope_ref、revision、下一步读取入口 |
| `research_route` | scope_ref；新建卡片或 route_ref+expected_revision+changed fields+reason | 精确 route_ref；不创建 node |
| `research_decide` | scope_ref、route revision 列表、basis_snapshot、result_ids、action、reason；必要时 route_changes、missing_results、next_work | decision_ref、更新后的路线引用、执行意图或停止建议 |

读取通过 `research_query collection=exploration_scopes|exploration_routes|exploration_decisions|exploration_results` 及 `ref` 展开，游标绑定项目、generation、scope、过滤条件和读取边界。稳定引用采用独立前缀，例如 `explore/scope/ES-001@2`、`explore/route/ER-002@3`、`explore/decision/ED-004`；不假装它们是 frozen evidence，也不让它们成为 claim 的原始实验来源。

node_core 继续提交知识、发布、建议与本节点结束请求；不能改全局候选处置。research specialist 只读允许的材料，blind specialist 仍只经 opaque input 读取。项目讨论、handoff 和 detached 会话均不能借新接口修改全局选择。

新操作通过 `_mutate()` 先处理成功请求重放，再检查当前身份、版本与 controller。相同 operation id 不同 payload 冲突；旧成功请求即使后来切换 main 也只返回旧结果，不重复执行副作用。

## 5. E1.2：一致的完整依据读取

### 5.1 一个投影计算入口

新增 `evidence_read.py`，提取 `build_evidence_view(store, db, roots, scope)`，由 context、query、E2 准备和 E2 提交共同调用。E2 在同一入口上增加 coverage 与 closure 计算，不能复制三套“当前风险”逻辑。

输入为明确的 `core_claim_refs`、选路 `basis_refs`、可选 publication/item；所有知识引用固定 revision。publication 解析到 owner，item 采用该 item 的 knowledge_refs，whole-publication 采用其声明集合并保留逐 item 来源，不把共享 publication 的其他发现自动连成前提。选路快照同时绑定所考虑的 scope/route revisions 与待决策结果；首次探针尚无证据时，保留无证据的事实，允许决定获取证据，不把它视为闭合通过。

投影必须包含：

1. 选定版本和当前版本并列的完整知识记录，含 scope、conditions、dependencies、evidence、已存 field checks、provenance。
2. `knowledge_support_edges` 的支撑祖先、边来源、固定材料 owner/对象摘要；grounded_in 和 knowledge-version evidence_refs 均参与。motivated_by 只展示动机，不传播失效。
3. 当前 publication_check、有效 impacts、撤回/修订 notices、dispositions，以及完整/部分审阅提出的未处置挑战。
4. 相关风险及未知相关风险。相关性基于所选引用与支撑路径，不能按年龄、todo 状态或全局 in_use=false 隐去。
5. `limitations`：缺失引用/字节、未知支撑类型、遍历限制、未展开正文、无法检查和覆盖未知；保留 affected refs、已检查范围与未完成位置。

没有 impact row 的已撤回支撑仍须通过图遍历发现。读取不重新运行 field check、不调模型、不 reconcile、不修改审阅任务。字段检查的新计算只在明确的写入/检查操作中发生。

### 5.2 跨 RPC 的快照与分页

`research_query collection=evidence` 首次输入 refs/scope 后返回：

```text
snapshot_id, project_id, generation, scope_ref, source_sequence,
basis_digest, manifest_digest, schema_versions,
counts, graph_complete, limitations, required_parts, cursor
```

在一次 SQLite 读事务内生成规范化结构快照；后续分页读该不可变快照，不能重新计算 current 后拼接旧页。普通快照使用有容量上限的进程缓存，重启或淘汰后明确要求重读；持久 draft 的决策包由保存的正文重建，不重新计算科学状态。结构闭包最多读取 2,048 个引用、8,192 条边、4,096 条相关审阅事件；超限保留不完整原因，不能据此 answered 或安排要求完整比较的新探针。

普通结构快照采用字符游标；固定材料和持久决策包采用 UTF-8 字节范围及正文摘要，页面不切断字符。两种范围不能混合。摘要不计作完整正文，遇限制时 `graph_complete=false`；游标走完只表示已交付该快照的全部内容，不能把不完整图升级为完整。

结构快照与冻结文件正文分别标明完整性。固定对象的元数据不是其正文已读凭据；非文本材料、指定摘录和未交付原始文件都保留阅读范围。后端对冻结对象的定位/读取复用 `frozen_refs` 与 workbench 分块，不回退到可变 workspace。文件完整性核验在 SQLite 写事务外执行；再核对账本指纹，避免长文件哈希阻塞写入。

`source_sequence` 只负责审计“当时看到了哪里”；`basis_digest` 覆盖当前范围、所选/当前知识版本、支撑图、检查、相关影响/挑战/处置及结果集合。使用这一领域指纹判断陈旧，不因 usage/context 保存、通知确认等无关事件使每次决策都失败。重新计算出的差异必须列明 changed refs/results/scope，而不是只有一个笼统冲突。

### 5.3 交付凭据

复用 checkpoint-delivery 的宿主观测方式，增加独立的 evidence delivery 描述与 `evidence_read_receipts` 表；不能把 checkpoint base 当作依据已读凭据。E1 决策按所需材料读取，不要求每个轻量探针支付最终审计的全量交付成本；E2 完整读取门禁针对明确的结项 draft。

- 宿主验证进入模型输入的完整工具正文。决策包凭据绑定当前请求；材料页在该请求实际产生模型响应或工具调用后确认，失败或未发送的请求不会形成有效材料历史。
- 范围绑定 project、generation、当前 owner/session、snapshot/draft、part/range 和 digest；决策包还绑定当前插件实例和模型消息。模型不能自填 `read_complete=true`。
- E1 可据此报告交付完整性；普通 board 浏览与 query 不写科学状态。持久登记由宿主的明确 acknowledge 操作完成，仅记交付事实，不提升 coverage、不产生选路进展；临时快照过期后不能凭旧回执续读。
- E2 对持久 draft 的交付登记落库，重启后可重读同一 draft；同代同主会话的已确认材料范围可跨请求累计，新 main 或新代不继承。迟到旧回执不能补成当前请求的决策包凭据。
- 证明的是特定材料已被交付，不是模型理解、科学验证或所有原始实验文件均已阅读。原始材料未读的限制进入检查结果，不能用结构记录的完整交付掩盖。

## 6. E1.3/E1.4：探针、结果与选择事务

### 6.1 精简节点协议

`next_work` 使用现有 propose 字段，并补充 work_kind=`scout | probe | deepen`、decision_question、change_choice_if、estimated_cost、required_outputs；这些字段限定任务范围，不提供额外权限或配额。

轻量输出为：result/knowledge refs、固定证据、条件、尚未知事项、建议。执行正常但结果不支持/混合/不足均是有效交回；不要求成功优化、独立包或长报告。工程深化可以使用原有完整交付规范。

材料侦察可用协调者已有读取能力或普通节点；需要专家时由节点调用 read-only specialist。测量、复算、重实现均是普通执行节点。main 的原生执行限制、盲评边界、specialist 超时/并发和用量记录继续生效。

### 6.2 结果进入待决策集合

结果集合与通知状态分开：通知 delivered 不代表模型考虑过；选择记录才消费结果。

| 触发事实 | 待决策记录 |
|---|---|
| 绑定节点发布新的固定结果 | publication/item + node/route + source sequence；一个发布事实只登记一次 |
| 绑定节点完成、停止、失败或退出未核实 | terminal/missing-result 事实；缺证据时明确不足，不能当负实验结果 |
| 已采用支撑被修订/撤回、收到挑战或新处置 | 精确 ref/change/review event，合并到受影响 scope |
| scope 目标/约束变化 | 新 scope revision 待重新选择 |
| 心跳、纯说明修改、重复通知 | 不新增关键结果 |

这些登记接在产生事实的事务中，内部 helper 接收现有 db；不得在只读查询时补造。一个 publication 后续伴随 finish 可在选择时批量消费，但保留两个事实的类型与引用。对采用前的历史材料仅记录显式导入清单，不伪造当年的通知/决策。

### 6.3 选择与执行意图

`chosen_action` 取 `probe | deepen | redirect | defer | recommend_combine | propose_stop`。模型给出理由和真正考虑的结果；程序不根据收益分数自动选赢家。单次 decision 最多提出一个 next_work，可关联多个候选，不启动多路线全局调度。

事务 A：提交选择。

1. `_mutate()` 重放检查之后，验证当前 main/controller、generation、scope/route expected revisions。
2. 重算所选依据和待决策结果的领域指纹；新关键结果、挑战或范围变化返回 `exploration_basis_changed` 及差异。无关遥测不冲突。
3. 追加 decision、处置变化的新 route revisions；仅将本次明确考虑的 result_ids 标为 handled，其余保持待处理。缺失结果进入 decision，不制造未发布材料引用。
4. 有 next_work 时，在同一事务写 `workflow_intents(kind=exploration_work)`，id 由 decision 唯一确定；只存固定计划与关联。没有 next_work 就不创建执行节点。

意图保存**该选择提交后的** scope/route revisions，以及支撑事实与结果边界；decision 的 observed basis 仍保留提交前快照。派发核对不把本选择产生的新 route revision/handled 标记误当外部证据变化。E1 每个范围最多一个未退出的选路执行工作；可记录新判断，但不会并行启动第二个探针。未 materialize 的旧意图可由新 decision 显式 supersede 并给理由；已经映射到 task 的工作必须沿原控制链核实结束。

事务 B：宿主 materialize 意图。

1. 新增宿主内部 `exploration_materialize` 操作；在 controller、控制状态和现有预算允许时，复用事务内 propose/task helper，原子创建 node、route binding、queued task，并把映射写回该意图。
2. 一个意图至多映射一个任务；重复调用返回已有映射。预算不足/显式暂停时保留未执行意图及原因，不创建假任务、不把路线标 failed。
3. 已生成 task 后由现有 `node_launch_claim → prepare/start → runtime_reserve → host submission` 路径执行，不另造 session/模型调用链。实际执行状态以原 task/launch intent 为准。
4. 准备派发前再次核对意图的依据、scope 和控制版本；已失效的意图标为需要重新决策，不自动启动陈旧工作。已启动任务不因新选择被假定退出。

宿主重启先恢复 controller/现有启动意图，再处理未 materialize 的探索意图。不会以“没收到工具响应”为由重新 propose/dispatch。接管 main 后由当前 main 认领本代未执行意图，已发送/不确定工作沿原 recovery 核实。

### 6.4 上下文、唤醒与 idle

扩展当前 main context：目标与关键未知、当前路线及证据、最强剩余替代、未处理结果、失效决策、待执行/受阻意图、关键风险。候选全量通过稳定索引/计数/游标访问；小预算先保留这些入口，标明未显示数量，不承诺所有路线详情必塞入上下文。

已有 deliver/wake 继续负责可靠交付，唤醒后提示“处理这些结果并决定下一步”，不自动深化第一条成功路线。同批结果可在一个决策处理；无新事实的唤醒不会额外产生选择。预算/用户暂停时不自动唤醒模型。

失效、关闭或永久业务错误使未启动工作终止时，同一事务保存结果与通知；无节点意图也可唤醒对应的等待。探索关闭后允许处理已有结果和 defer/propose_stop，但不允许创建新工作。当前严格依据检查覆盖完整选择视图，因此新增无关候选也可能使排队探针失效，须重新选择。

路线编辑、反复排名或新增近似名称不重置 idle。可计作选路进展的是第一次处理新的结果/支撑变化并形成有依据的决策，或实际节点工作；反复引用同一结果翻转处置不累计进展。沿用总轮次/墙钟限制，不另加分叉率或决策次数配额。

## 7. E2.1/E2.2：当前支撑、审阅类型与覆盖

### 7.1 当前核心集合

E2 声明 `core_claim_refs`、固定 evidence roots 和回答范围；后端只检查声明的核心，不声称抽取了报告所有断言。answered 需要非空、精确、可解析的核心主张及非自引的结构化支撑；不能仅由最终报告、路线理由或 decision 互相循环支撑。

核心集合的 pinned/current 差异不被自动替换。核心被修订、支撑变化或挑战出现后，需重读、明确保留旧版本并给出处置，或改用新版本与报告范围。缩小核心集合/scope 保存 before/after、理由、排除项与残余影响，原风险仍可追溯。

R2 显式审阅范围、当前选路依据和当前结项核心集合，作为带 origin 的贡献合并到项目审阅投影；不能用新范围直接覆盖 `r2_review_scope`。派生相关性不清除挑战，且不把全项目历史 backlog 都变成结项必填。

### 7.2 类型化记录

复用 `review_link`、review lifecycle event 和 challenge disposal，扩展现有 `research_relate(kind=review)` 的类型化字段；新增 `review_assessments` 以 review event 为键保存新增信息，而不修改旧事件的含义。

| 字段 | 约束 |
|---|---|
| `target_refs` / obligation | 精确 claim revision 或固定材料；匹配事件所服务的目标 |
| `review_type` | `citation_scope \| semantic_inference \| recompute \| reimplementation \| adversarial_test` |
| `result` / `complete` | 继续使用 supports/challenges/inconclusive 与显式完整性；completed 不等于无风险 |
| `report_ref`, evidence refs, limitations | 非空独立报告、实际查看范围、输入不足/未执行等限制 |
| 执行身份 | session/attempt/specialist task、实际 provider/model/variant、effective tools；宿主观测填充，未知保持 unknown |
| 独立性 | 作者关联、是否同模型、是否共享研究上下文、blind assigned-input 范围、执行关联；分别记录事实，不以新会话推导“独立” |

引用/范围、语义审阅可由只读 specialist 完成；重算、重实现、对抗实验要求普通执行节点的可关联执行记录与固定复核输出。仅填 `review_type=recompute` 不会获得执行复核覆盖；来源不足时只能保留声明与限制。这里核对执行关联、工具能力与输出，不授予强执行来源资格，也不保证科学复核内容属实。

report 与 target 通过解析后的 owner/对象身份检查自引，覆盖字符串别名、whole-publication/item/子路径，不能只比 ref 字符串。相同报告/同一次执行对多个目标的覆盖按目标展示，不计作多份独立验证。字段检查 consistent 不自动满足语义审阅或执行复核。

### 7.3 覆盖推导

模型按风险为核心 claim 声明需要的审阅类型及理由；允许 `not_required` 并保留理由，不要求所有祖先走相同仪式。需要独立性时显式声明要求，后端根据实际事实匹配。要求变化保留在结项草案版本中。

派生输出包含 required、covered、missing、limitations 和每种类型/版本/身份的明细，汇总为 `unknown | unreviewed | partial | reviewed`：

- 无有效新型审阅时为 unreviewed；只有历史自述、目标覆盖不明或实际身份不明时保留 unknown/限制。
- 部分所需类型/版本有覆盖为 partial；所有声明需要得到有效覆盖且无覆盖未知，才为 reviewed。空要求集合不产生 reviewed。
- 同模型或非盲评可以提供明确受限的审阅；若不满足某项已声明的独立要求，就不覆盖该要求。不能一概冒认为独立，也不一概丢弃所有非独立审阅。
- supports 的覆盖记录不会消除旧 challenge；所有完整/部分挑战仍需现有精确事件处置。inconclusive 可以表示审阅任务做过，但其证据限制必须进入 closure。
- 新 revision 不继承旧版本 coverage；旧代记录仅在精确对象及条件适用、显式接受复用后参与。R1/R2 历史 completed/self-declared 不自动升级。

## 8. E2.3：四维结项和范围内停止

### 8.1 保存结构

| 维度 | 内容/状态 |
|---|---|
| `outcome` | 沿用 answered / partial / unresolved，是当前目标的回答程度 |
| `evidence_closure` | resolved / qualified / unresolved / unknown；附每项风险、限制和判断 |
| `review_coverage` | 上节推导的状态与矩阵；不能由最终调用者填写 reviewed |
| `exploration_stop` | 当前 scope/decision、已考虑候选、已到/缺失结果、未做原因、残余未知、恢复条件 |

每条相关风险或限制的判断使用 `risk_id, affected_refs, core_impact(yes|no|unknown), reason, evidence_refs, disposition, gap`。风险 id 基于精确 ref/change/review event/限制类型生成，不能靠改描述让原风险消失。程序判断记录是否匹配、引用是否可用、已声明状态是否矛盾；科学重要性由模型判断，并保存依据供复核。

### 8.2 outcome 与证据状态矩阵

| 条件 | 允许提交 |
|---|---|
| 非空核心依据及支撑完整，已知相关风险均处理 | answered + resolved；review 独立计算，仍可以 unreviewed |
| 有完整核心答案，残余缺口明确不影响核心且有理由、依据、限定和 gap | answered + qualified |
| 已知未解决项削弱核心，或其核心影响仍 unknown | partial/unresolved；不能 answered |
| 核心为空、核心引用无效、报告自引构成唯一依据 | evidence unknown；仅 partial/unresolved 并解释缺口 |
| 遍历/材料/引用类型不完整 | 不得 resolved；核心影响不明时不能 answered；可证明属非核心且核心另有完整支撑时可 qualified |
| 只有历史自述 review | review unknown，不得派生 reviewed；不单凭未独立审阅否定探索性 answered |

resolved 只表示声明范围内已知问题得到处理。没有注册、执行来源未知或没有独立复核，不单独决定 outcome；如果目标本身要求独立复验或执行资格，缺失就作为核心要求检查。

### 8.3 停止说明

stop reason 支持 `goal_met | sufficient_for_current_choice | low_expected_value | resource_limited | user_requested`，并记录理由，不推导全局最优/穷尽。

后端从当前范围导入所有候选、结果和工作状态；模型只补尚缺的处置与重要性判断，避免每次抄写整张候选表。已有有依据的 deferred/excluded 处置可引用复用；未试候选可以批量应用同一适用理由，但保留逐项索引和恢复条件。

结项前，当前范围内的重要未试路线、待决策结果、缺失结果须明确处理或按范围排除并给理由；不能仅用“节点都结束了”。不要求清空项目历史 open_question。单路径交付可以记录“目标明确，无额外探索”；路线清单为空不会自行证明 evidence resolved，也不强制补候选。

用户停止、预算暂停与退出核实始终先执行。缺最后综合预算时保存已有部分产物和未结论状态；不自动生成 answered，也不自动追加实验补齐表单。之后显式恢复才继续准备结项。

## 9. E2.4/E2.5：准备、报告与原子提交

### 9.1 工具和流程

新增 main 专用 `research_prepare_conclusion`，沿用 `research_publish` 与 `research_conclude`。新模式工具参数按已采用契约呈现，不把新旧两套必填字段拼成长可选表单。

1. `research_query collection=evidence`：读取结构快照，按 `material_ref` 分页读取固定材料，再重新查询读取覆盖与关键风险。
2. `research_prepare_conclusion`：输入 snapshot、core_claim_refs、预期 outcome/gaps、风险判断、审阅要求和停止理由；核对当前指纹后保存完整结构依据及单独的决策包。材料读取状态在此冻结；后续继续读材料须重新查询并 prepare。
3. 分页读取不可变决策包；结项所在请求必须完整收到对应 draft 的所有页。决策包上限 32,000 UTF-8 字节；结构事实超限时显式缩小范围，不能自动裁掉风险。图和材料限制仍然可见。
4. 写最终报告并 `research_publish`：带 draft_id 与精确核心引用；发布路径将生成的 evidence appendix 一起冻结，并登记 report/appendix/basis 的关联。不要求模型手抄附录或伪造哈希。
5. `research_conclude(draft_id, final_ref, summary)`：从 draft 取得 outcome、gaps、risk judgments 与派生 review；原子重算后提交。不能在最后一步另传一份不一致的 coverage/核心集合。

prepare 不是停止执行，也不是结项完成。修改核心、判断或停止理由应产生新 draft；旧 draft 保留为历史，不能原地覆盖。preview 不产生有效结项凭证，普通只读检查也不自动启用新契约。

### 9.2 持久记录与哈希关系

E2 新增 `conclusion_drafts`、`conclusion_evidence`，复用 E1 的 `evidence_read_receipts`；conclusion_evidence 以现有 conclusion_id 为键保存四维快照/报告关联。旧 `project_conclusions` 和旧 requests 回执不重写。

- draft 保存完整结构快照、独立决策包、范围/路线、相关结果、风险判断、审阅要求、source sequence 和摘要；大块原始材料留在固定对象中，不塞入决策包。决策包的 manifest 对应其实际交付正文。
- evidence appendix 从同一 draft 生成，含核心/支撑索引、pinned/current、条件、风险与处置、覆盖、阅读范围/限制、路线停止说明。
- basis digest 不包含由它生成的最终报告/附录，避免循环哈希。appendix 不包含自己的哈希或未来报告哈希；发布 manifest 关联 basis digest、appendix hash、report hash 和精确 refs。
- 报告的结构化声明引用同一 draft 和核心集合；结项核对 final_ref 的 owner、版本、字节完整性和 manifest。文件内容改变须重新发布；核心依据变化须重新 prepare/read/publish。
- 源码与工具能保证这些结构一致，不能保证自然语言标题完全不过度概括。语义复核仍保留“平均份额≠p99上限”“零命中≠不可能”等检查要点。

### 9.3 原子结项检查

冻结文件预检在事务外完成；提交事务内只检查确定的账本身份/哈希、当前科学状态与控制状态，不在写锁里遍历大文件。事务原子性覆盖账本结项，不宣称文件系统与 SQLite 具有同一事务。

事务内按顺序执行：

1. 请求幂等重放；当前 main、controller、generation 和已采用契约校验。
2. 沿用 live tasks、specialists、attempts、focus queue、退出未核实等既有执行闭合检查。范围内未 materialize 的探索意图须在停止决策中显式撤销/取代；不能在保存结项后继续派发。
3. 验证 draft、完整交付凭据、final publication/report/appendix manifest 指向同一依据版本。
4. 同源重算核心支撑、当前风险、审阅覆盖、scope/route/decision revisions、待处理结果及意图。新关键结果/撤回/挑战/范围变化返回 `conclusion_basis_changed`、具体差异和刷新入口；usage、交付确认、生成的本 draft 报告等无关事件不使其自我失效。
5. 检查 outcome/closure 矛盾、必需风险判断和范围内停止处置。不完整或未知不能按空集通过。
6. 一次保存旧结项主记录、新四维快照、报告/附录关系及检查事件上界，再结束当前主工作段、置 complete/paused。任一失败全部回滚。

所有结项入口最终汇合到同一 `ConclusionStore.conclude` 契约分支；不能以省略 contract 参数、旧 tool schema、直接 service 调用、切换 context/exploration 或 workflow complete 绕过。历史成功请求继续返回其原结果，不被追溯重判。

## 10. 最小产品可见性

沿用当前概览、研究、成果三个目的地，不新增“上下文检查”“探索审计”或重复主视图。

| 位置 | E1/E2 增量 |
|---|---|
| 概览 | 当前选择问题、下一工作/受阻原因、未处理结果；已结项时四维状态与停止理由 |
| 研究 | 在既有流程中查看路线列表/详情、关联节点与选择历史；可展开暂缓/排除候选 |
| 成果 | 最终报告与生成附录，当前风险、核心版本及覆盖明细；历史自述明确标识 |
| 工具结果/context | 可行动的缺口、差异和读取引用；不以原始大 JSON 作为主要说明 |

只读浏览旧 schema 不迁移、不增加路线和关系。不存在的路线显示“未登记探索”，历史 review 保持 historical/self-declared；不能显示为本轮新增门禁已通过。

## 11. 迁移与代码落点

### 11.1 schema 与兼容

E1/E2 使用 schema 13/14，复审修复新增 schema 15，用于代际契约、无节点探索通知和材料回执来源；接口/契约版本不依赖数据库号。

- E1 migration 增加五类领域表、evidence_read_receipts 及 generation 策略配置；执行意图复用 workflow_intents。回执 schema 预留可空 draft 关联，E2 复用，不再建第二份回执表。
- E2 增加 typed review、draft、四维结项扩展表；不将旧 review completed 或结项 review.status 回填为新 coverage。
- 新库创建、逐级迁移、备份、失败回滚/重试和 readonly 支持范围一起更新；schema 9–12 的只读行为保持。历史缺字段通过显式 legacy/unknown 投影，不批量造路线、decision 或已读凭据。
- 策略关闭保留新增记录；数据库物理降级不在普通开关内。回到旧备份会丢新增记录，不能称无损回退。
- 同步插件 Python bundle、RPC 能力声明、工具 schema 和文档版本；独立包必须在仓库 src 不可用时运行闭环与恢复。

### 11.2 改动清单

| 层 | 新增/修改 |
|---|---|
| 领域/迁移 | 新增 exploration_store、evidence_read、结项证据计算模块与增量 schema；修改 native_store 继承/事务接点 |
| 证据/审阅 | epistemic、frozen_refs、review_lifecycle、conclusion_store；复用支撑边、字段检查与精确处置 |
| 读模型 | query_store、context_policy、memory_store、workbench_read；证据快照、路线查询、上下文投影 |
| 执行/控制 | workflow_store、runtime_budget、service；结果登记、意图 materialize、控制/版本校验与 idle 事件 |
| 宿主/工具 | plugin/src/tools.ts、schema.ts、research.ts、commands.ts、engine.ts、specialists.ts、index.ts；版本化工具、可靠交付、实际审阅身份 |
| 交付观测 | 扩展 checkpoint-delivery 的公共交付机制或提取小型共享模块；evidence receipt 独立类型，不混用 CAS base |
| 看板 | board 的 Overview/Process/Review/Materials 等现有视图与 model/format；不增加顶层目的地 |
| 发布 | 两份 README、CHANGELOG、plugin/python、独立 pack 探针与恢复说明 |

## 12. 开发提交顺序与完成条件

默认使用一个 `codex/e1-e2-exploration` 开发分支，按以下可审查提交推进。E1、E2 可分 PR 审阅；用户统一验收待闭环集成后进行。此处不承诺日历工期或代码行数。

| 顺序 | 对应工作包 | 具体交付 | 完成条件 |
|---|---|---|---|
| D0 | V1/V2 | 冻结接口样例、状态矩阵、评估任务与基线探针 | baseline 能重现空依据/自述 reviewed 漏洞；fixture 不含研究者可见答案 |
| D1 | E1.1 | scope/route/decision/result/binding、迁移、CAS、权限与策略采用 | 重放/冲突/越权/readonly/历史不补造验证通过 |
| D2 | E1.2 | 同源支撑/风险投影、快照分页、完整性与交付描述 | 当前/固定版本、无 impact 撤回、晚页风险与过期游标验证通过 |
| D3 | E1.3 | 三类工作协议、普通探针与最小结果交回 | 负结果/不足可交付；无权限或预算旁路 |
| D4 | E1.4 | 结果登记、选择事务、持久意图、materialize/恢复、idle | 重启/重复通知/新证据/预算不足不重复或陈旧派发 |
| D5 | E1.5/E1.6 | 生效策略核验、main context、现有视图路线入口、单范围闭环 | 能据负结果转向、保存候选和停止建议；单路径不强制扩树 |
| D6 | E2.1/E2.2 | 核心集合、typed review、实际身份/独立性和 coverage | 自引/旧 revision/任务 completed 不伪造覆盖；有效审阅可计入 |
| D7 | E2.3/E2.4 | 四维判断、draft、完整交付回执、原子结项及旧路径统一 | 核心未知不能 answered；合法 qualified/partial 和先停止可用 |
| D8 | E2.5 | 同源附录、报告 manifest、四维展示和旧历史显示 | 报告/附录/receipt 指向同版本；无重复主入口 |
| D9 | V1–V3 | 全量回归、稳定副本兼容、独立包/宿主恢复、真实模型匹配对照 | 交付工程证据与效果结果/限制；效果不足保持 opt-in |

D1 与 D2 可分别实现，但 D4 必须依赖二者的版本/依据契约。D6 在 D2 稳定后可推进；D7/D8 共用同一个 draft，不各建一套临时风险结构。每次只做当前提交所需抽取和验证，不为 E3 预建调度器、规则 DSL 或插件化科学状态机。

## 13. 必须通过的验收用例

新增 `tests/test_exploration_*.py`、`tests/test_evidence_read.py`、`tests/test_evidence_conclusion.py` 及相应 Bun 集成用例；优先断言持久事实、授权和可观察行为，不为可逆文案调整写镜像测试。

| 编号 | 场景 | 必须观察到 |
|---|---|---|
| E-01 | 保存多候选，尚未选择 | 零执行节点；无 node quota 扣减 |
| E-02 | 两请求并发改同一路线/范围 | 一个成功，旧 revision 冲突，历史不覆盖 |
| E-03 | node/specialist/discussion 调全局选路 | 后端拒绝，原科学记录不变 |
| E-04 | 选择成功后工具响应丢失再重放 | 只有一个 decision/intent/节点/任务 |
| E-05 | 选择后、materialize 前重启；启动发送前后重启 | 恢复同一工作；不盲重发不确定提交 |
| E-06 | 预算/用户暂停发生在派发前 | 意图可查，未新增执行；恢复不重置预算 |
| E-07 | materialize 前出现挑战/新关键结果 | 旧意图不启动，要求重新选择；已运行工作状态真实保留 |
| E-08 | publication+finish+重复通知 | 结果事实可批处理；通知交付不消费选择，重复无额外派发 |
| E-09 | 探针不支持、混合、不足、崩溃、unverified | 科学评价与执行状态分开；不自动 refuted |
| E-10 | 反复改路线说明/翻转同一排名 | 不持续重置 idle；新结果后的首次有依据决策可推进 |
| E-11 | 只有单一明确实现 | 正常交付，无强制候选/反证/审阅配额 |
| R-01 | pinned 与 current 不同 | 两版并列；不静默换 pin |
| R-02 | 支撑已撤回但没有 impact row | 仍出现风险，motivated_by 不误传播 |
| R-03 | 相关风险位于最后一页/长字段 | 全部可读、范围和 digest 一致，摘要不计完整 |
| R-04 | 翻页间新修订/进程重启/缓存淘汰 | 旧快照稳定或明确过期；不混合多个状态 |
| R-05 | 对象缺失/目录/非文本/不支持引用/遍历上限 | 有限读取不崩坏控制操作；限制保留，不按空集通过 |
| R-06 | 截断工具结果、未发送请求、迟到/跨会话回执 | 不产生当前完整交付凭据；新 main 重新读取 |
| C-01 | 空核心、最终报告自引、同对象别名自引 | 不能 answered + resolved/reviewed；可诚实 partial/unresolved |
| C-02 | whole publication 与 item 指向不同核心集合 | owner/知识范围正确，未相关发现不变成隐式前提 |
| C-03 | review complete 但 challenge 未处置 | coverage 与风险分别显示，核心风险不被完成标签清除 |
| C-04 | 旧 revision/历史自述/未知模型/只读声称复算 | 不满足新精确覆盖/执行类型/独立性要求 |
| C-05 | 有效类型审阅、实际身份、适当独立性 | 仅覆盖对应版本/类型；同报告不变多份独立证据 |
| C-06 | 已知核心风险 yes 或 unknown | answered 被拒；partial/unresolved 可保存缺口 |
| C-07 | 有证据证明只影响边界的非核心缺口 | answered + qualified 可用，无关 backlog 不阻断 |
| C-08 | 准备后撤回/新挑战/关键结果/范围改变 | 旧 draft 提交失败并返回差异；无半条结项 |
| C-09 | 仅追加 usage/context receipt/本 draft 最终发布 | 不误判科学依据变化，不进入无限 prepare 循环 |
| C-10 | 改 final report/附录/核心 refs 或拼接旧回执 | 版本/哈希检查拒绝；重新准备/发布后可提交 |
| C-11 | 排除候选/缩 scope 后结项 | 历史与原风险仍可查，需理由和同步收缩后的声明 |
| C-12 | 待决策/缺失结果、未试候选 | 范围内有明确处置；不要求全部执行 |
| C-13 | 新契约下调旧 API/关探索/切 context | 不降级结项；历史成功请求仍幂等返回 |
| C-14 | 用户停止/预算尽，缺最终表单 | 能先终止执行；保留未结论/部分结果与恢复入口 |
| X-01 | schema 9–12 readonly；12→E1→E2 写迁移失败再试 | 原库只读不变；备份有效；迁移原子、旧回执/语义保留 |
| X-02 | 盲评/主协调原生工具强插/接管/热重载 | 边界仍成立，不泄露全局排名或绕执行授权 |
| X-03 | 80/132 列看板、无路线旧项目 | 可找到当前选择与报告依据；不新增顶层上下文入口 |
| X-04 | 独立 tarball，仓库源码不可导入 | 内置后端、worker、迁移、选路、结项和重启均正常 |

有针对性检查通过后再运行既有 Python、Bun、TypeScript 与 pack 全量验证；若之后只改规划文字，不重复全套运行。原 R1/R2、runtime control、coordinator boundary 的风险/历史语义断言必须保留。

### 一条贯穿 E1/E2 的验收剧本

使用隔离 fixture：目标为改善服务尾延迟，候选 A 为优化平均成本占比最高的组件，B 为先校准尾部关键路径；材料含一个“平均份额可推出 p99 上限”的诱人错误前提。

1. main 保存一个 scope 和两张卡，数据库仍没有新执行节点。
2. 选择校准探针，提交后重启宿主；同一 intent 恢复为一个普通节点，预算记一次。
3. 探针发布区分平均/尾部口径的固定材料。通知重复交付，待决策集合仍可明确区分结果与执行结束。
4. main 读取当前支撑，修订错误依据，选择转向尾部关键路径；A 保留适用条件与恢复原因，不被当成普遍无效。
5. 关键数据未足以支持服务级改善时，允许 partial/unresolved；不得靠报告自引或自填 reviewed 变成 answered。
6. 若随后获得足够的范围内结果，构造带非核心机制缺口的 answered + qualified、生成附录和报告；prepare 后再注入一个核心挑战，旧提交必须被拒。处理后重新读取/准备/发布，才可结项。

该剧本同时检查行为改变与持久恢复；不以“卡片、表或风险 JSON 已出现”代替验收。它属于受控夹具，不声称重跑了原真实项目或证明性能收益。

## 14. 从 E1 开始的真实模型评估

### 14.1 对照组与分阶段执行

| 组 | 读取 | 策略/契约 | 用途 |
|---|---|---|---|
| A | 原实际采用策略 | 原流程 | 描述现有基线，记录实际 memory-v2/R1，不能混成同条件收益 |
| B | 新版完整读取 | 无持久探索、原结项 | 单独估计读取变化 |
| C | 同 B | 仅探索 guidance | 区分提示收益与持久循环收益 |
| D | 同 B | evidence-v1，无新探索编排 | 单独估计证据/结项契约收益 |
| E | 同 B | 完整 E1/E2 | 最终组合效果 |

E1 先比较 B/C/持久 E1，在 E2 完成后比较 B/D/E；A 作为读取差异对照单独报告。每批固定同一 provider/model/variant、工具、环境、目标、预算与输入，按种子配对安排顺序，不把更换模型或打开 R1 的收益算为选路策略收益。

评估任务从开工时冻结，研究者只见任务包，评分标准/隐藏真值不进入其工作区和上下文。首批至少覆盖错误初始前提、有价值替代、单路径、分布反转、噪声/不可比、资源中断；E2 加入主张撤回/陈旧预览、自引结项和合法限定回答。组合退化的完整多路线管理留到 E3；本轮只验证不会无依据宣称组合收益。

先用剧本模型完成工程恢复验证，再运行小规模真实模型配对试验；建议每个入选任务每组至少三次，先冻结总调用预算与单任务额度。这个数量用于发现初期行为问题，不作为统计显著性保证。调用前在 evidence manifest 固定实际模型与额度；本规划不预支未配置的模型服务或费用。

### 14.2 原始记录与评分

独立于研究会话评分：核心答案质量、过度概括/风险披露、关键前提是否验证、负结果是否改变选择、是否遗漏强替代、无效深化投入、停止理由。分叉数/反证数/revision 数只作诊断，不给凑数奖励。

逐运行保留模型版本、策略/契约版本、真实交付上下文、工具/IPC/provider 调用、输入输出 token、墙钟、实验/设备使用、重试/拒绝/重放、启动整理开销与用量覆盖。未知不算零，失败和中断运行不剔除；先冻结评分，再揭示组别并报告分歧。

结果分别报告工程正确性、同状态读取差异、真实模型质量/成本。可以得出“证据不足或没有改善”；不预设节时百分比。若登记/阅读负担压过试探收益、无意义扩树、质量退化、风险隐藏或重复派发，修正相应实现/策略并保持 opt-in，不能放宽核心未知门禁换通过。

## 15. 交付清单与当前未做项

工程完成时提交：代码与迁移、工具/context 契约、同源证据读取与附录、现有视图改动、恢复/采用说明、独立包、工程与真实模型证据。建议证据分别放入 `docs/evidence/e1-exploration/` 与 `docs/evidence/e2-conclusion/`，每项记录 source commit、运行命令、策略/模型、原始输出与限制。

统一本地验收先在隔离副本/隔离宿主完成完整场景，再对旧真实项目的稳定副本核验迁移与读取；不得直接改原项目来制造验收结果。是否安装到共享本地 OpenCode、接管真实项目和启动付费模型，按届时明确的验收指令执行。

本轮不实现：多 scope 调度、分批/共享预算新账本、执行资源租约、异步比较屏障、自动组合与回退、MCTS/固定 beam、确认性注册、严格数据消费资格、强执行来源。保留必要的多对多关联与条件记录即可，不为这些未来能力写空接口或兜底分支。

本文已确定 E1/E2 的实施选择，没有需要阻止开工的产品问题。实际 provider/model、真实试验总额度和隔离宿主配置在 D0/D9 的运行 manifest 固定；它们不妨碍先实现持久事务、权限、读取和结项契约。
