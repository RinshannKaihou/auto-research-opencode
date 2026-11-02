# 探索触发、动态上下文与决策负担开发计划

日期：2026-11-02（Asia/Shanghai）。状态：已实施（同日完成 A1–B3 与 V1；工程验证与归并对照见 [交付记录](<./evidence/exploration-activation/README.md>)，真实模型配对评估仍后置）。本计划落实用户确认的两项工作：①探索触发与动态上下文；②减少重复确认成本。真实模型配对评估按用户要求后置，不是本轮工程交付的前置条件；本轮也不宣称研究效果已经改善。

主设计见 [探索与多路线管理](<./Exploration and multi-route management design.md>)；实现、评审修复与本 PR 的实际验证见 [交付记录](./evidence/exploration-activation/README.md)。本计划是在已完成 E1/E2/E3 上的增量，不恢复旧 R1～R4，也不重新实施 E3。

本 PR 已基于远程 main `13079c8` 独立整合，保留 SDK 2.2.5、后台执行修复与 CI；以下本地 HEAD 及旧版本描述为开发时点记录，不作为本 PR 的版本声明。

## 1. 基线、范围与交付判定

发布基线为 v0.2.0 / `b504e0c6802fd4004ce7422d2f7a0432ca433bbb`，schema 16。本地 HEAD 为 `31fd90d`，有既有 E3、运行时及文档改动，版本字符串仍旧；不能把 HEAD 或工作区整体直接当作发布版。开始实现时先记录当前分支、diff、运行时版本及测试基线，确认待改函数的当前内容，保留其他工作。复核记录中的 124 项通过属于先前的针对性检查，不替代开发完成后的验证。

本轮完成标准：

- 探索开启时，主协调者在零 scope 和已有 scope 两种情况下持续收到适用的探索策略；不把直接交付任务强制改成多分支。
- 默认上下文准确展示路线状态、现行选择、重要待办和展开入口；已排除的早期路线不再占满候选样本。
- 同一工作的相关事实可以成组阅读，模型仍明确提交实际考虑过的 result IDs。
- 可证明只重复已发布信息的正常完成，不再额外产生一项研究处置义务；新增信息、失败、撤回、质疑仍进入原来的决策与失效路径。
- 协调者明确完成的、满足限定条件的 scope 状态动作不再要求对完全相同的信息作第二次确认。
- 版本检查、完整证据读取、结项原子检查、执行授权、预算、资源退出核实和恢复语义保持有效。

本轮不增加：最低分支数、反证/修订配额、强制实验引用、统一单路径审批、selection/discovery 的新结项硬门槛、MCTS、第二套调度器或自动科学排名。前一项先落实提示和信息可见性；它不能保证模型一定探索，相关效果结论留待后续真实模型评估。

## 2. 开发切片与依赖

| 切片 | 交付 | 主要代码位置 | 依赖与风险 |
| --- | --- | --- | --- |
| A1 策略与工具说明 | 持续探索提示；结项提示与有效 schema 一致；关闭探索时隐藏 work_batch；绑定保存真实 work_kind | `memory_store.py`、`context_policy.py`、`plugin/src/tools.ts`、`plugin/index.ts`、`exploration_store.py` | 可独立合入；低风险 |
| A2 动态上下文 | 状态计数、按问题组织的候选/风险样本、容量分配、可追溯选取理由 | `context_policy.py`、`memory_store.py`、`workbench_read.py`、`plugin/src/research.ts` | 在 A1 上集成；中等风险，关注遗漏与角色边界 |
| B1 工作结果聚合读取 | 只读工作分组、稳定成员身份、分页入口；复用为上下文摘要 | 探索读模型、`service.py`、`plugin/src/tools.ts`、`context_policy.py` | 可独立开发；中等风险，关注分页和尝试身份 |
| B2 机械完成确认 | 严格白名单识别重复完成；保留执行事件；减少冗余探索结果 | `native_store.py`、`exploration_store.py`，核对 `runtime_budget.py` / `workflow_store.py` 与插件 finish 链路 | 首选在 B1 后合入；最高风险，关注语义遗漏与恢复 |
| B3 scope 自身状态动作 | 对安全的单纯关闭/暂缓记录动作凭据，避免自生成待办 | `exploration_store.py`，核对 evidence / conclusion / review 路径 | 可与 B2 分开合入；中等风险 |
| V1 联调与交付 | Python/Bun/类型检查、隔离宿主、独立包、文档与证据 | 现有 tests、plugin/test、probe 脚本与文档 | 依赖以上切片；工程验收 |

建议合入顺序：A1 → A2 → B1 → B2 → B3 → V1。A1/A2 不依赖 B2/B3；先合入后，原探索和结项流程仍可使用。B1 仅改善读取组织，单独合入时不承诺减少过期拒绝。

按 Agent 开发工作量，本轮有五个可审查实现增量和一个联合验收增量；主要不确定性集中在 B2 的 finish / review / wake 时序。若 B2 的安全条件不足，可先交付 A1/A2/B1，并明确 B2 尚未完成，不能通过放宽快照比较来凑齐交付。

## 3. A1：持续策略提示与接口一致性

### 3.1 策略适用范围

通过现有 `context → research.memory → memory_context` 通路注入，只对探索开启的主协调者生效。策略从可省略的路线详情中移至有独立容量保障的短块；不修改节点/specialist 的权限，也不向 blind specialist 注入全局候选和排名。

提示内容围绕四个触发点，控制为简短、可执行的规则：

1. 准备较大投入前，指出会改变投入决策的关键未知，考虑用更小的可执行探针检验。是否属于“大投入”由模型结合任务和预算判断，不新增统一分钟数门槛。
2. 对 selection/discovery，考虑实质不同的替代，或说明单路径理由；零 scope 时提示先登记需要管理的选择问题。delivery 允许直接执行。
3. 新结果到达后，判断它是否改变现行选择、重要替代或适用范围。已登记的挑战与反证优先检查；普通新结果不自动标成反证。
4. 宣称平台期或准备停止前，检查依据、未试范围、残余不确定性和继续投入的信息价值；允许有理由停止。

上述为策略指令，不以缺少第二条路线拒绝普通提案或 answered。零 scope 的问题不能只靠“已有 scope 的 task_kind”解决，因此零 scope 也保留相应提示，但程序不凭自然语言目标自动判定任务类型。

### 3.2 同批修正的小问题

- main 的结项说明按当前 `conclusion_contract` 生成：evidence-v1 使用 prepare / draft / 完整交付 / final_ref 流程；legacy 保留旧参数说明。暂停/停止时的控制指令优先于探索提示。
- exploration off 的 closing-only `research_decide` schema 同时移除 `next_work` 和 `work_batch`，后端检查继续保留。
- `exploration_bindings.work_kind` 写入实际工作种类，复用当前 work_kind 的验证结果；不回填历史绑定，不根据新标签改变旧节点 purpose。
- 完成提示说明：新的证据、限制和建议应先发布；只有没有额外信息的正常结束才可原样复用最后一次完整发布的 summary。finish.summary 与发布 summary 不同仍合法，B2 按保守路径保留其独立待办。

验收：on/off、零/多 scope、legacy/evidence-v1、running/paused/stopped、main/node_core/blind 的实际请求文本和工具 schema 一致。检查真实注入内容，不能只断言某个常量存在。

## 4. A2：动态上下文的具体结构

### 4.1 读取与事实边界

沿用 `memory-r1-v1`、同一数据库读事务和既有 context hook。探索子投影标记为 `exploration-context-v2`，在已有 context record 中保存版本、选取理由和省略数量。首版以确定性代码生成，不调用额外模型写摘要。

从已有数据生成：

- 全研究代及每个 scope 的路线状态计数；继续保留已有 `routes_total`、pending 等字段，不悄悄改变字段含义。
- `investigating` 表示已投入路线，不等于正在运行；真实活跃工作从 intent/task 状态推导，分别展示。
- 当前已采用路线、最近明确选择、provisional 和 expected/missing；最新 decision 中的 `route_refs` 是考虑集合，不能全部解释为“赢家”。
- 已登记的 critical_assumption、next_probe、恢复条件及其来源引用。缺少已登记验证不等于现实中从未验证，文案应准确说明记录边界。
- 当前相关的新结果、已有审查质疑/撤回等风险及展开入口。

程序只计算事实和展示优先级。首版不新增“最强替代”的必填字段，也不从 reason/rationale 自由文本猜排名；没有明确结构化记录时显示“重要替代尚未指定”，并展示尚可考虑的候选。模型可以通过现有选择记录说明其判断。

### 4.2 样本选取

替换 `ORDER BY route_id` 后取前三条的唯一策略。优先覆盖：

1. 与已采用/已投入路线相关的待处理结果和明确风险。
2. 现行采用/暂定选择，以及尚缺的重要结果记录。
3. 尚未尝试候选；展示其关键前提和已有 next_probe。
4. 暂缓/排除路线的数量、恢复条件及展开入口；有已登记条件变化关联时提升可见性，不自动判定恢复条件已经成立。

按 scope 分配展示名额，优先有待办的 scope，再覆盖其他活跃 scope；采用稳定次序，单一 scope 不占满全局样本。没有科学优先级声明时，稳定次序仅作展示，不称为质量排名。不能承诺所有候选同时出现，必须保留完整数量、遗漏数量与按 scope 展开的入口。

每项保留固定 route/decision/result 引用和 `selection_reasons`。A2 初版直接使用已有结果索引；B1 合入后可以展示同源工作分组摘要，底层证据引用不变。

### 4.3 容量与角色隔离

总体沿用宿主的 12,000 字符上限，区分字符预算与 token 用量。分配顺序先保障身份/控制、证据风险索引及完整展开入口，再保留探索短规则、主要计数和待办提示，最后填充路线说明与历史文字。

现有 900 字符测试需补充新投影的最小摘要检查；空间不足时先降级为总数、提示码和完整查询入口。最低安全包仍放不下时明确返回预算不足，不能静默丢掉控制或风险。精确引用不截断；长说明可省略但注明展开。路线多、长名称、多 scope 下均有确定性样本上限。

复用当前失败/缓存路径：过期上下文继续标 stale，旧控制指令不授予工作权限。上下文读取不新建 scope/route、不修改候选状态、不重置无进展预算、不触发写迁移。

### 4.4 验收场景

- A/B/C 已排除、D/E 未尝试：默认摘要包含未尝试计数，并展示 D/E 的代表项，不能仍只有 A/B/C。
- 早期路线持续存在：新到反证/质疑与受影响的现行选择进入优先区。
- 多 scope：每个 scope 的总数可查，其他 scope 的待办不能被“最近一个 scope”覆盖。
- 只有一条 delivery 路线：正常展示，不要求凑分支。
- 零 scope：仍有探索触发策略及登记入口，不伪造候选。
- 预算不足、缓存失效、blind：省略和隔离行为明确，科学风险与控制边界保留。

## 5. B1：按工作聚合结果的只读入口

保留 `exploration_results` 原有逐条结果查询和 `research_decide.result_ids` 写契约。新增只读 collection `exploration_inbox`，通过现有 `research_query` 使用；可在独立探索读模型模块实现，供 context/workbench/query 复用，不再复制一套各不相同的统计逻辑。

分组身份为 `(generation, scope_id, task/intent identity, attempt_id)`；优先通过发布的 attempt_id、任务/启动记录和绑定核实。不仅按 node_id 分组，防止同一节点重试时混入旧结果。身份缺失或不唯一时，以 result_id 单独成组并标记 unknown。scope/claim/review 类型变化另成来源明确的组。

每组返回：稳定 group key、所属 scope 和工作身份、当前执行状态、完整/省略标记、成员 result IDs、publication refs、尚待处置的成员数量和展开入口。共享探针对多路线的绑定仍是一份工作；各 scope 的引用不是独立证据。

使用既有 scope_id、ref、cursor、limit 查询参数：ref 在该 collection 内表示 group key，不是可写入 evidence_refs 的科学引用。组和成员均有容量上限、稳定排序、依据上界/摘要校验；成员过多时继续分页并标记不完整。读途中成员变化返回刷新提示，不能把未交付成员静默添进旧页。

聚合视图用于导航，不替代 `collection=evidence` 的正式快照与材料读取。写入仍只接受当前 scope 的显式 result IDs，不能提交一个 group key 让后端自动把“此刻最新的所有成员”算作模型已阅读。`pending_results_total` 保留逐条义务计数，新增组数另命名。

验收：publication+finish 可成组；第二次发布仍显示新增成员；同节点不同 attempt、不同 scope 不混组；大组分页可取全；读后到达的新成员不能被旧决定顺带确认；组查询本身不写库或改变结项状态。

## 6. B2：在事件产生时减少冗余义务

### 6.1 首版技术选择

**保持 `build_evidence_view` 的完整依据比较，不先引入宽松的“忽略部分变化”快照。** 优先在 finish 事务中识别可以证明没有新增信息的完成确认，使它不再生成独立 pending exploration result。

`attempt.finished`、attempt/task 状态、结束摘要、运行通知、审阅排队、预算结算与执行退出核实全部保留。探索结果表是需考虑的领域结果，不要求每个运行事件都再复制成一条研究义务。新增可审计的完成归并事件，关联 publication、attempt、task（可核实时）、scope、规则版本和操作身份；不得伪造 handled_by_decision 或声称模型读过 finish。

这与“直接删掉 finish”不同：运行事实仍完整；只减少可证明重复的研究待办。它也不保证每次 finish 都能归并。

### 6.2 可归并的严格白名单

新 finish 同时满足以下条件才可走归并路径：

1. 正常 `finished`，来源为当前受管、身份可核实的节点 attempt；不是 stopped、failed、cancelled、unverified 或用户/预算强制终止。
2. 同一 attempt 已有冻结且可解析的发布，最后一份发布为 complete；对应 publication 的探索结果已按当前 scope 绑定存在。不能拿上一次尝试的发布作依据。
3. finish.summary 与该发布 summary **原文相同**；其他 finish details 仅含协议明确识别的运行字段，没有额外自由文本或未知载荷。首版不用模糊匹配、空泛“完成了”或模型语义判断证明等价。
4. 发布引用、已登记风险、预期/缺失证据的意义未因本次操作发生需要重新考虑的变化。正常执行状态变化单独留在运行记录；身份或影响无法核实时保留原路径。

当 finish 的总结补充限制、建议、否定或任何不同内容时，继续生成独立结果。现有工具说明本来允许 summary 承载新信息，所以不能只检查“存在 publication”。所有新 publication，无论与上一份有多相似，仍按新结果处理。

归并判定与 finish 状态更新、审计事件在同一事务完成；重复请求不重复通知/归并。publication 未提交时、finish 先到时或进程崩溃时按真实已提交状态处理。历史 pending finish 不批量清理，重启后也不重新解释旧决定。

### 6.3 必须保持的边界

- 若 finish 同时导致真实 review 风险或其他依据变化，选择仍可失效；不能为了达到“零重试”跳过这些变化。
- 已批准 work 的 `scope-work-v2` 指纹及启动检查不修改。已经运行的工作不因展示变化取消。
- 正常 finish 不证明外部进程已经退出；资源/退出未知继续由原流程核实。不能用归并状态释放不确定资源。
- 唤醒和调度仍处理真实完成；合并研究义务不能造成等待协调者永不恢复。通知消费不等于科学处置。
- 自动归并不计作主协调者的有效研究进展，不用它重置无进展预算。
- 结项仍检查当前 live work、pending 实质结果、风险、完整交付凭据和依据版本；本轮不改 E2 四维语义。

### 6.4 确定性验收

建立固定时序：publish → main 读证据 → finish → decide/prepare。

- 相同 summary、同 attempt、无其他依据变化：不新增 pending finish，旧快照不会仅因该确认失效；原执行/审计事件可查。
- 不同 summary、partial 发布、跨 attempt、无发布、未知字段或无法核实身份：保留独立结果及相应 stale 拒绝。
- 新发布/撤回/审查质疑/失败/迟到证据：仍须重读，不得被归并掩盖。
- 发布已处理后才 finish：纯确认不再次形成研究待办；有新内容仍形成待办。
- 重放、冷重启、控制器接管、共享探针、资源退出未知：无重复派发、无丢失结果、无错误释放。

已有 4/13 的脚本轨迹只作诊断起点。保留原轨迹；新场景分别覆盖“确为机械确认”和“finish 含新信息”，不把所有拒绝次数归零设为目标。

## 7. B3：避免对自身 scope 状态动作重复确认

在 `research_scope` 的当前 revision / attached-main / controller 检查内识别以下窄场景：当前主协调者明确将 scope 从 active 改为 closed 或 deferred；给出理由与恢复条件；只有状态及其说明改变，科学目标、约束、比较口径、引用、父范围等均未改变；该 scope 无待处理结果和活跃/待执行工作。

满足时照常追加 scope revision、保存操作者与完整状态动作，并记录“该状态动作已由本次明确操作表达”的审计凭据；不再为这个相同动作单独创建 `scope_state_changed` 待办。凭据引用实际操作，不虚构 decision ID。

不满足白名单时保持现有结果生成行为，不额外禁止原本合法的 scope 更新。尤其目标变化、恢复 active、已有待办、活跃工作或未知字段时继续保守处理。只要 scope revision 变了，动作之前的选择/草案仍应失效；本优化只省掉动作之后对自身事实的额外确认。

关闭 scope 不删除路线、历史负结果或原待办；untried/investigating 的停止处置、缺失结果、核心风险、多 scope 结项覆盖均继续适用。若状态变化使其他受影响范围需要重新考虑，不得一并自动确认那些影响。

验收：纯状态动作后用新快照准备结项，不再被其自生成事件阻塞；旧快照仍被拒绝；有未处置路线时 answered 仍被拒绝；有真实风险、任务或其他变化时保留原约束；partial/unresolved 出口不收紧。

## 8. 兼容性与生效方式

- 首版方案不新增数据库表/列，保持 schema 16；新增读取投影和事件载荷复用现有存储。若实现发现无法满足身份/审计要求，应先修订方案，不能借“降摩擦”隐式丢弃信息。
- A1/A2 对已显式启用 exploration-v1 的项目生效；off/legacy 不自动启用探索，也不自动切换已有研究代的结项契约。
- B2/B3 只影响新操作中满足白名单的义务产生方式；历史结果、历史决定、历史成功请求重放保持原样。旧记录身份不明走保守路径。
- 对新上下文/聚合能力使用明确版本或 capability；旧包不认识时保持已有读取入口，不把缺少新视图误认为没有待办。新插件与内置 Python 同步发布；旧控制器继续受现有 fencing 约束。
- `basis_digest`、材料 digest、交付凭据和冻结草案继续按原契约核验；聚合摘要不进入科学引用，也不替代读凭据。
- 只读查询不迁移库。原库、共享 OpenCode 和真实研究运行不用于破坏性测试。现有未提交工作不清理或回退。

## 9. 验证与交付清单

开发时按切片增加能验证实际风险的测试，而非逐字镜像 prompt：

| 层 | 重点 | 复用位置 |
| --- | --- | --- |
| Python 上下文 | 候选覆盖、多 scope、小预算、角色/盲评、只读与过期 | `test_context_policy.py`、`test_e3_context.py` |
| Python 结果/决策 | 身份分组、分页、白名单、相反条件、CAS、结项、重放恢复 | `test_exploration_decisions.py`、`test_e3_multiroute.py`、`test_e3_evidence.py`、`test_e3_read_contracts.py`、`test_evidence_conclusion.py` |
| 运行控制 | finished/stopped/unverified、review、通知唤醒、资源、预算及无进展 | 既有 runtime / E3 control / budget 测试 |
| 插件 | 实际 context hook 文本、动态工具 schema、node finish 与材料回执 | `plugin/test/plugin.test.ts`、`plugin/test/e3-multiroute.test.ts`、相关 review/package 测试 |
| 隔离宿主与包 | 发布-读取-完成交错；新实质证据仍使决定失效；重启和独立包 | `scripts/probe_e3_run.py`、`scripts/probe_packed_release.py` |

每个切片先跑相关测试；集成后使用现有 `make test` 覆盖完整 Python、Bun、TypeScript。包验证在隔离构建副本中执行 `sync:python` / pack，确认内置 Python 与最终源码一致，不依赖仓库源码或真实研究库。沿用 probe 的模拟 provider；用户已后置的真实模型对照不偷偷纳入验收。

交付保存：精确源码基线/包版本、运行环境、实际命令与通过/失败日志、代表性模型请求上下文、结果聚合前后对照、保留/归并的事件及依据变化差异。新增证据放在 `docs/evidence/exploration-activation/`，不覆盖已有 review 和 E3 原始记录。

后续报告分开说明：①工程行为与兼容性是否通过；②脚本宿主中的重复待办/重试是否减少；③真实模型探索质量与成本仍未验证。开发计划完成不等于代码已实施；本轮工程通过也不直接证明路线选择更好。
