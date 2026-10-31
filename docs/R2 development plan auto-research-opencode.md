# R2 具体开发计划

日期：2026-10-31（Asia/Shanghai）。状态：R2 的 S0–S7 已开发完成并完成内部验证，统一验收包已齐备，尚未安装到共享 OpenCode 或发布。六项运行时能力、M2 调用观测和 M3 持久预算均已接入；结果见[实施记录](./evidence/r2-runtime/README.md)。下文保留原设计与验收判据，实际接口和边界以实施记录及源码为准。M4 真实模型效果实验另行开展。

2026-11-01 首轮审阅修订：challenge 按事件保留并显式处置，scope 声明与 checkpoint 解耦，观测归属并入业务响应，基线登记共用上下文等待期限；节点源码／配置真实变化可计作开发推进。具体处理与 push back 见[审阅处理记录](./evidence/r2-runtime/review-response.md)。

同日第二轮收尾：源码探测改为宿主有界 worker，后端不扫描；关联元数据可缺失，deadline 刷新失败保留原定时器；完整 checkpoint 上限 4,000 字符，Legacy 优先展示当前代最新挑战。资源预算、缓存语义和新增验证见[第二轮处理记录](./evidence/r2-runtime/review-round2.md)。

代码基线：`main` / `0c7b6d5d4d0dba21910c90f407bbb6d5b7fe17fe`，v0.1.1，schema 10；R1 已随 PR #7 合并。依据为用户提供的本地资料 `Remediation implementation plan auto-research-opencode.md` 第 4、7、9 节，技术原则服从 `Remediation roadmap auto-research-opencode.md`；这些规划原稿不随本次 PR 提交。旧计划中的 `0476d81` 是历史调研基线，不作为本次实现基线。

## 1. 本次交付

R2 让机器承担明确可推导的版本、路由和来源保留工作，让模型继续声明科学关系、失效范围与证据判断。最终交付是六项运行时能力：

1. 普通发布、结束、修订和 correction note 产生审阅候选；当前实际依赖、受影响或显式提升的目标才产生审阅义务。
2. 精确链接的审阅结果关闭对应任务；风险由原有 impact/disposition 语义决定，离开范围也保留风险。
3. 五个窄写工具替代模型当前看到的 `research_memory action`；旧调用保留适配入口。
4. 常见修订使用 preset；明确声明未增加来源时可保留原来源；响应返回实际持久化检查结果。
5. checkpoint 必须带有模型已收到的读取基线，双会话陈旧写不能追加成功。
6. `research_relate` 统一 typed 关系入口；引用形成派生 adoption，减少重复 consume。

同时交付 M2 调用测量和 M3 推进/预算修复。**全部功能、迁移、打包和内部检查完成后，再交付一次统一用户验收。** 内部拆分为可审查提交，不让用户在半完成的真实项目中逐段验收。

### 边界

- 保留 Legacy / R1 两种读取策略。R2 是写入契约与运行时能力，不新增 `/research context-policy r2`，也不要求先删除 Legacy。
- 保留“概览／研究／成果”三个主入口。候选和义务在“成果 → 依据检查”中解释；运行状态在“研究”中显示。不恢复独立上下文检查页，不增加测量仪表盘。
- 不改 R3 的最终结项准入、三轴结项状态、完整 evidence appendix、复核独立性和正式 coverage 判定。R2 不修饰历史 `review.status`，也不宣称解决现存空引用结项缺口。
- 不开发 R4 的 prediction、experiment candidate、确认性注册或数据消费必填表单。
- 不建立通用 command bus、策略注册中心、观测服务或自动修复队列。只在 API、宿主事件、IPC、文件与数据库边界校验；内部使用明确数据结构和现有事务，错误沿原错误路径返回。

## 2. 已核实的现状与代码落点

下表 Python 路径相对 `src/auto_research/`，TypeScript 路径相对仓库根目录。行号以本计划基线为准。

| 位置 | 现状 | R2 修改 |
|---|---|---|
| `native_store.py:307 _mutate()` | 同一 request_id 要求 operation 与序列化 payload 完全相同；失败回滚 | 保留该语义；依赖当前状态的 preset、来源继承和 CAS 校验放在首次执行的事务内 |
| `memory_store.py:540 queue_review_in_tx()` | 普通事件直接插入 pending `review_todos` | 保留旧行，增加候选解释和精确义务；旧状态不能直接证明新义务已完成 |
| `memory_store.py:659/714` | record/revise 返回值在 `field_checks.evaluate()` 前构造 | 事务内计算后重新读取并返回实际持久化结果 |
| `memory_store.py:1034 checkpoint()` | 只有传入 expected_revision 才比较 head | 新写入强制基线；原始 head 与可见 checkpoint 分别绑定 |
| `memory_store.py:1125 checkpoint_view()` | 隔离错归属/归属不明的旧全局 checkpoint | 保留隔离；不得将可见旧行的 revision 当成原始 head |
| `memory_store.py:1170 review_todo_state()` | pending/running/proposal_ready/completed，无日常模型闭合入口 | 旧 API 保留历史意义，新生命周期使用独立事件与目标 |
| `epistemic.py`、`context_policy.py:23` | 已有 typed support、精确 impacts、dispositions、当前范围读取 | 复用纯函数和传入 db；读取不反向更新生命周期 |
| `native_store.py:690/1102` | consume 逐次追加；relate 不处理知识 typed 关系 | 增加派生 adoption 读取及 typed 路由；不可就地修改历史知识版本 |
| `service.py:703/1111` | capabilities 与旧 memory_write 路由；宿主身份由服务端补充 | 协商新工具契约，旧入口进入相同权限和领域事务 |
| `plugin/src/research.ts`、`plugin/index.ts` | context 缓存是字符串；context_record 异步保存；没有已交付 checkpoint 基线 | 保存与本次模型请求一致的结构化基线，明确读取与交付边界 |
| `plugin/src/storage.ts` | 同一传输请求最多两次超时重试；后端错误只保留 message | 保留既有重试上限；记录真实尝试，传递 R2 的 code/details，不新增重试机制 |
| `plugin/src/engine.ts:129/164` | 每个成功 writes 都重置进展，idle/busy/retry 主要在内存中 | 改用角色相关领域事件；预算与 idle 的必要事实持久化 |
| `native_store.py:78` | 只读允许 schema 集合是 `{9, SCHEMA_VERSION}` | 升级后明确支持 9、10、11，不能因常量变为 11 而丢失 schema 10 读取 |
| `plugin/scripts/sync-python.mjs` | Python 打包使用显式文件白名单 | 新模块加入白名单，用独立 tarball 验证 |

本地 SDK `@opencode/plugin@2.0.18` 已提供 context/model.request/retry、tool execute.before/after hooks，以及 execution、step、usage 事件。**execute.after 只表示工具结束，不证明后续模型已收到结果。** 实际模型与 variant 优先取 step.started，usage 按宿主事件字段读取，不沿用旧版宿主字段名猜测。

## 3. 公共契约与存储

### 3.1 版本和身份

- 新增 capability `memory_write_contract="r2-v1"`、`checkpoint_base="receipt-cas-v1"`、`review_lifecycle="candidate-obligation-v1"`，均已实现。
- capabilities 同时返回项目实际 schema、支持的工具契约及预算执行范围；进程支持新版不代表只读旧库已升级。
- 全局 capabilities 仅报告进程能力；新增 `project_read(view="capabilities")` 以只读方式报告指定项目实际 schema/契约。获取项目能力不得触发可写打开。
- 升级客户端只向模型提供五个新 memory 工具。旧 `research_memory` 作为服务端/历史调用适配入口保留，不与新工具同时注入模型上下文。新客户端连接不支持的后端时明确报版本不兼容，不猜测降级。
- 服务端继续以真实 host/session/association/attempt 解析角色和当前 owner。模型不能指定 `asserted_at`、伪造执行身份或借新入口扩大写权限。specialist 仍然只读。
- 读取策略切换不影响强制 CAS。旧结项 receipt、已保存 context、原始 execution_refs 和原 review_todos 不重写；新 generation 也不能覆盖旧结项记录。

### 3.2 增量 schema

使用 schema 11。新增表的职责如下；DDL 与索引统一定义在 `schema11.py`。

| 表 | 必要字段/键 | 用途 |
|---|---|---|
| `review_candidates` | candidate_id；legacy_todo_id 唯一；trigger_kind/ref、node_id、target_refs、mapping_status、created_at | 解释候选来源；无法精确映射的旧事件保留 unknown，不发明 claim 目标 |
| `review_obligations` | obligation_id；generation、scope_owner、target_kind/ref、change_id；state、created_at、updated_at | 普通目标键为 `(generation,scope_owner,target_kind,target_ref)`；impact 键额外包含 change_id，target_ref 即精确 affected_version |
| `review_lifecycle_events` | event_id、obligation/candidate_id、action、reason、evidence_refs、scope_snapshot、source_sequence、operation_id、actor、created_at | 追加 promotion、review link、closure、出/入范围、例外和重开事实；同次操作/目标/动作唯一 |
| `checkpoint_read_receipts` | base_id；host/session、plugin_instance_id、association/attempt、generation、node_id、head_revision、selected_checkpoint_id/revision、state_digest、source_sequence、origin、created_at | 绑定已交付快照；origin 区分 context/query/write_result，不是执行来源证明 |
| `runtime_budgets` | generation、limits、started_at、deadline_at、updated_at | 保存预算配置及不会因重启/暂停而重置的截止时间 |
| `runtime_budget_reservations` | reservation_id、generation、kind、session_id、owner、operation_id、state、created_at | 对受管自动轮次/节点创建原子预留；操作重放不重复扣账 |
| `runtime_progress` | generation、host/session、last_event_sequence、idle_turns、last_finished_turn | 跨重启保留进展边界，重复宿主事件不重复累计 |

`scope_owner` 是项目或具体 node，不能只用容易被替换的 session。association/attempt 留作审计及 checkpoint 绑定。新表不复制造一套 risk；risk 继续从 `knowledge_changes/impacts/dispositions` 与已有检查派生。

scope_owner 使用非空规范值 `project` 或 `node:<id>`；target_kind 为 knowledge/material，knowledge target 必须带 revision。普通义务和 impact 义务分别使用 `change_id IS NULL` / `IS NOT NULL` 的部分唯一索引，避免 SQL NULL 导致同键重复。candidate promotion 与 event 追加和义务插入共用事务。

候选/义务关系及审阅报告链接写入 lifecycle events，projection 从事件和精确键读取，不另建通用图存储。M2 调用日志采用项目内追加 JSONL，见第 9 节，不再加数据库观测表。

### 3.3 幂等、适配和事务

执行顺序固定为：身份与权限 → 确定入口版本及稳定的原始意图 payload → `_mutate()` 查原 receipt → 首次请求才展开 preset/来源/当前 owner/CAS → 领域写入、检查、生命周期事件 → 保存响应并提交。

- 旧 action 采用原 operation 名和原 payload 序列化形状。尤其省略 execution_refs、expected_revision 的形状不被新 adapter 补写；已经保存的 requests 行不迁移、不重新编码。
- 把 revise 对当前 checks 的预读取校验移到首次 work(db) 内；重放已有请求不得因当前文件、head、owner 或新规则变化而先行失败。权限仍需检查。
- 新入口使用固定 `contract_version` 与未展开的意图作为 payload。展开结果记录在响应和领域事件中，不把“当前最新值”加入重试 payload。
- 旧无 base checkpoint 的适配读取凭据属于宿主执行元数据；首次执行时验证并将实际 base 写入 checkpoint 审计，不能改变旧请求 payload。新入口显式 base 属于其意图；同一 call 的宿主注入值冻结，传输重试不得替换。
- 同 ID/同意图返回原响应；同 ID/不同意图冲突。不同 ID 即使内容相同也不是幂等重放，是否合并由具体领域规则决定。
- 复用 `_mutate()` 和少量接收 db 的领域函数，不嵌套公开写方法、不增加第二套权限实现。新旧接口语义等价不等于可以用不同操作 ID 互相去重。
- post-commit projection 失败必须与 mutation 已提交区分；重试原 ID 返回原结果，不重复追加领域记录。

幂等 receipt 只表示该次操作的结果。它不重新签发当前有效的 checkpoint base，不重算 field_checks，也不因后来发生的风险变化修改已保存响应；最新状态通过独立 query 读取。

## 4. 审阅候选、义务与闭合

### 4.1 候选形成与提升

保留 `queue_review_in_tx()` 现有历史插入及 ID 生成，追加 candidate。迁移只给原 todo 建候选解释，不将旧 completed 转为新义务的 completed。

| 来源 | 候选目标 | 何时成为义务 |
|---|---|---|
| publish / finish | publication/item 的精确材料及显式 pinned knowledge；没有知识引用时 mapping_status=unknown | 当前明确采用的材料/支撑版本，或显式 promote |
| knowledge revision | 新版 fixed ref、change 与 affected versions | 当前使用的目标或当前受影响版本 |
| correction note | 原 note 引用；只有结构化引用才可作为精确目标 | 显式 promote；不从自然语言猜受影响版本 |
| 既有/新增 impact | `(change_id, affected_version)` | 当前依赖受影响版本时；发现晚到引用也执行相同规则 |

节点“当前使用”从活跃工作段的 fixed inputs、typed dependencies、已登记 adoption 和知识支撑边读取。项目审阅范围使用下述明确持久集合；近期发布、词项相似、`in_use` 或缺少范围，均不足以自动证明已采用。材料目标的审阅完成不能推出该材料中未登记 claim 的科学 coverage。

项目范围冻结为 `project.config.r2_review_scope={generation, refs}`，refs 为明确的 fixed knowledge/publication/item 引用；复用配置存储，不新增范围表。只有以下写入口可以变更：

- project owner 的 `research_checkpoint` 可选传 `decision_refs` 和 `scope_reason`，在 checkpoint CAS 成功的同一事务替换 refs。省略表示保留，`[]` 表示明确退出原集合；变更时 scope_reason 必填。node checkpoint 不接受该项目范围字段。
- `review_scope_set(refs, reason)` 使用同一内部函数，现通过 main 专用 `research_review_scope` 向协调者开放声明入口，不依赖 checkpoint；`review_promote`／例外控制仍为 host-only。提升项目义务时将精确目标加入 refs，并注明 promotion 来源。替换 scope 时保留前后集合、actor、reason 与事件上界。
- typed support 祖先及已声明材料的 pinned knowledge 从 refs 派生，不把所有 project-visible 知识当项目核心依据。scope 替换/当前 generation 结束触发退出；新 generation 从空集合开始，不能偷用上一代 final_ref。

该集合只声明需要关注/审阅的项目依据，不充当 R3 最终报告选择或结项证明。不提供 refs 时项目范围保持 unknown，不自动 out_of_scope；显式空集合则可确认原显式范围已退出，仍不消除全局风险。旧 conclusion.final_ref 仅用于历史读取，不拿来创建当前 generation 的义务。这里的 checkpoint 可选写字段与 query/context 的同名只读选择参数有意区分。

promotion/reconciliation 只在有关写事务内触发：propose/consume、record/revise/typed relate、publish、影响或 scope 变更，以及工作段/当前 generation 生命周期。读取 context、query、board 的 `decision_refs/final_ref` 仍是只读选择参数，不落库，不提升义务。

新增分页集合 `review_candidates / review_obligations / review_events`，按 scope_owner、generation、精确 target、state/change_id 筛选，使用现有固定上界与 filter-bound cursor。mutable 状态导致集合变化时明确要求刷新；只有无关 usage 等事件变化时仍能续页，不因全局 sequence 每变一次就失效，不自动循环重试。R1 的 review_backlog 和 Legacy 的对应提示都改为解释候选/当前义务，旧 review_todos 作为可查历史来源；不能只改成果页却继续把旧 pending 数注入每轮提示。风险集合沿 R1 语义保持完整。

提供专用宿主 API `review_promote(target_ref, scope_owner, reason, evidence_refs, change_id?, candidate_id?)`，用于明确提升或接管旧候选。generation 取当前项目 generation；target_kind 由规范 ref 解析，拒绝无固定版本的知识和无法确定的可变目标。传入 change_id 时须验证该 change 的确影响 target_ref；不传则只提升普通目标义务，不猜某个 impact。返回完整 obligation key 与 obligation_id。candidate_id 仅关联来源，不替代精确目标。

该 API 由关联 main 宿主操作；自动提升也使用相同的内部键规则。不增加新的主视图或每轮必填范围表单。无法从当前数据确定范围时保留 unknown 与已有 R1 风险提示，不自动 out_of_scope。

### 4.2 生命周期

义务状态只表达工作：`pending / running / completed / out_of_scope / waived`。其中 waived 是有理由和证据的显式例外，不表示通过；风险状态沿原 risk/disposition 读取。

| 事件 | 任务变化 | 风险变化 |
|---|---|---|
| 进入明确当前范围或显式 promote | 创建 pending；同键不重复创建 | 不覆盖 risk |
| 开始对应审阅 | pending → running | 不变 |
| 精确目标获得完整审阅报告链接 | pending/running → completed | 报告可 supports/challenges/inconclusive；不能据此自动 resolved |
| 对应 disposition 追加 | 仅在它本身提供完整审阅链接时完成任务，否则任务状态不变 | 按原精确 change/version 语义计算；unresolved 继续可见 |
| 明确离开当前范围/工作段结束 | 追加 out_of_scope，记录原任务状态、scope 和原因 | 历史风险保留，全量查询仍可见 |
| 同一目标重入范围 | 复查同键报告；晚于最近一次 reopen 且未被撤销的完整报告可恢复 completed，其余恢复 pending；新 change 独立建义务 | 旧 unresolved/challenge 仍出现，不被离开/重入清除 |
| 宿主手动 waive/reopen | 追加 actor/reason/evidence/scope，不覆盖原事件 | 不改成科学通过 |

跨目标不能闭合：完成 `K@3` 不关闭 `K@2`；处理 `(CH-1,K@2)` 不关闭 `(CH-2,K@2)`，也不关闭其他 owner/generation 的义务。一个 todo 涉及多个目标时逐项展示，不用 todo 的单一 completed 字段吞掉未完成目标。

reopen 事件建立新的审阅周期边界，不能被出/入范围或重放旧报告撤销；恢复 completed 必须有边界之后的新完整报告，或 main 显式 `accept_previous` 事件（附理由与证据）。scope reconciliation 不根据科学风险是否“看起来没问题”自行接受旧报告；风险变化和任务状态分别处理。

### 4.3 R2 所需的最小审阅链接

通过 `research_relate(label="reviews")` 路由到专用领域事务，必须输入 obligation_id、frozen 报告 source_ref、精确 target_ref、result、`complete`、理由及 evidence_refs。服务端按 obligation_id 读取唯一的 generation/scope_owner/target_kind/change_id，并验证 target_ref 一致；若额外传 change_id 也必须一致，不能据 target_ref 批量闭合。服务端补 actor。main 可链接本项目当前 generation 的义务，node_core 只可链接自己当前 owner 的义务，specialist 无写权限。

- 报告必须解析到实际冻结文件/报告材料；target 必须存在。没有精确目标、只有历史 review.refs、空报告或直接用 target 自引，不能自动完成新义务。
- `complete=true` 且精确链接齐备才完成相应任务；截断/超时/部分报告保持待办。challenges 和完整的 inconclusive 可以完成“做完这次审阅”，同时保持未解决科学风险。
- R2 仅验证链接与任务完整性，不计算正式独立性、审阅类型资格或综合 coverage；显示 `coverage=not_assessed`，不升级 conclusion.review.status。
- 宿主 API `review_exception(obligation_id, action, reason, evidence_refs)` 处理 waive/reopen/accept_previous，记录完整 key 和真实 main actor；accept_previous 还须指定原 report link。旧 review_todo_state 继续只改旧任务，不旁路完成新义务。模型不获得该例外 API。

## 5. 窄工具与修订 preset

### 5.1 模型看到的工具

| 新工具 | 必须由模型表达 | 自动处理 |
|---|---|---|
| `research_record` | kind、statement；claim/observation/lesson 的 evidence；必要 applicability | 当前 owner/写入身份；沿用已存在的证据检查 |
| `research_revise` | fixed ref、changes、reason、preset；custom 的失效语义 | 由 fixed ref 得 expected revision；preset 展开；明确的来源保留 |
| `research_checkpoint` | state；变更项目范围时可选 decision_refs 与 scope_reason | 当前 owner、已交付 base；显式 base 可用于响应后连写；省略 scope 字段保留原集合 |
| `research_dispose` | change_id、affected_version、disposition_kind、reason、必要 evidence/replacement | 固定目标验证和原 disposition 事务 |
| `research_narrow_scope` | change_id、明确保留的精确范围、reason | 复用 narrow_impact_scope 事务；不猜失效范围 |

模型可选显式传 expected_revision，但必须与 fixed ref 一致；stale ref 冲突，不转投 latest。项目级 record 仍须明确 visibility=project，node 不能写 project checkpoint。工具说明同步出现在角色提示和 engine kickoff，移除“修订必须重抄全部 execution_refs”的旧引导。

### 5.2 具体展开规则

| preset | 展开 | 限制 |
|---|---|---|
| `editorial` | `change_kind=reword`、`affected_scope_mode=none`、空 affected_scope | 仅改 statement 文案/author；不得同时改变 scope/conditions/status、证据、依赖或 checks。语义上是否真的仅改文案仍由作者负责 |
| `correct_previous` | `change_kind=correct`、`affected_scope_mode=versions`、`affected_scope=[输入的旧 fixed ref]` | 只声明直接上一版；服务端 CAS 验证它仍为当前版本；不会自动扩大为全部祖先版本 |
| `custom` | 按显式 change_kind/affected_scope_mode/affected_scope | retract 必须精确 versions；narrow/不确定纠错不得硬套 editorial/correct_previous |

preset 默认不自动挑选。冲突字段报 `preset_conflict`，不让显式字段悄悄覆盖 preset；多版本撤回/纠错必须 custom。旧 action 没有 preset 时沿旧显式语义，不补猜。

常见调用示例（来源保留规则见下一节）：

```json
{
  "ref": "knowledge/K-012@3",
  "preset": "editorial",
  "changes": {"statement": "在已声明的测试条件下，没有观察到错误。"},
  "reason": "收紧表述，实验和依据未变化",
  "preserve_sources": true,
  "no_new_evidence": true
}
```

响应仍提供知识记录原字段，另加 `applied`（实际 preset/失效范围/来源操作）、`field_checks`、`provenance`。checks evaluate 后使用同一 db 重新 decode；响应值必须与提交后的 query 一致，不能把声明的 checks 当成计算结果。

## 6. 来源保留与诚实归属

`preserve_sources=true` 必须配合 `no_new_evidence=true`，只用于 revise；它表示新版本继续引用旧版已有执行来源，不表示本次重新执行过实验。

- evidence_refs、dependencies/grounded_in、checks 等证据结构必须与旧版本相同。可以省略，也可显式给出完全相同的值；发生变化时报 `source_preservation_conflict`，要求选择显式来源路径。
- preserve_sources 与显式 execution_refs 不得同时出现。继承仅复制旧版执行引用及原解析状态，响应记录 `preserved_from=K@N`；本次 asserted_at 仍由真实调用者生成，不把旧执行归给本次作者。
- 旧版没有来源，保留后仍为空/unknown；unlinked 不能因保留操作变为 verified。禁止从 author、当前 session、attempt 或“编辑发生在这里”推导过去实验由此执行。
- 不选择保留时沿旧契约：execution_refs 省略就是 `[]`。显式提供仍由 `_execution_refs()` 解析；新增证据无可核实来源时允许探索记录，返回 unknown/unlinked。
- 服务端只能排除结构冲突，不能证明“无新增实验”的语义真实性。不把这个声明用作 R4 强执行来源资格。

## 7. checkpoint：模型已收到的基线与强制 CAS

### 7.1 读取对象

context 和 checkpoint 专用 query 在一个读快照中返回：owner（项目/node）、association/attempt、generation、原始 `head_revision`、实际展示的 selected checkpoint ID/revision、完整 state 的摘要及 source_sequence。该 owner 尚无 checkpoint 时 head=0；若旧全局最新行被隔离，head 可以大于 selected revision，二者绝不混用。

只读返回本身不写 receipt。只有本次请求实际包含完整 checkpoint state（或明确的空状态/隔离说明），才能建立自动基线；被预算省略或截断的 state 不能凭元数据获得“已读”资格。小预算不足时提示执行 checkpoint 专用 query，不强行扩大每轮 context。

新增 `research_query(collection="checkpoint")` 返回当前 owner 的完整 state、recovery 信息与读取描述；其他 owner 仍按既有读取权限，不能因此取得它的写基线。board/project_read/shadow/context_preview 不签发运行时基线。

### 7.2 宿主交付路径

1. 插件每次启动生成 `plugin_instance_id`；HOST_ID 目前固定为 opencode，不能拿它代替进程实例。
2. context hook 先确定最终要插入的 checkpoint 文本，插入后用专用 host-only `checkpoint_observed` 将上述快照登记为 receipt。登记的是这次文本的旧 head，不重新取最新 head 补值；宿主 API 验证它对应不可变历史快照。
3. receipt 成功后只保存于本次模型请求关联的 base；异步 context_record 继续负责原有上下文留痕，但“保存成功”本身不再充当交付证明。若登记失败，保留明确缺失 base，下一次 checkpoint 要求先读，不静默补值。
4. query 的输出先按原样返回。下一次 context hook 在 request.messages 中确认该完整工具结果确实进入当前模型输入，才登记/激活其 receipt；不在 execute.after 时先行替换 base。
5. 成功 checkpoint 返回随机不可猜的 `next_base`，对应本次写入的完整 state。后续模型可以显式回传该 token；插件只在确认响应进入后续模型输入后自动使用它。平行发出的工具不能拿未收到的响应提前升级基线。

插件按请求/assistant message 绑定已交付 base，并在开始处理一个 tool call 时冻结，不用单个 session 上的“最新 base”变量替代。相同 assistant message 中并行调用同用原 base；后续模型 step 读到成功工具输出后才可使用 next_base。query/write_result 的 receipt 登记及调用关联是宿主执行元数据，不改变模型看到的业务结果或原 operation_id。

这里只证明模型请求收到相应内容，不证明模型理解或执行成功。不扫描自由文本推测“读过”；只识别插件自身带来源标识的结构化 context/tool result。S0 必须用本地 SDK 确认真实消息结构及 hook 顺序，随后冻结 TypeScript 类型和契约测试。

### 7.3 写入和错误

在 `_mutate()` 的首次 work(db) 中解析真实 owner，然后验证 receipt 的 host/session、instance、association/attempt、generation、owner 及当前 raw head，验证与追加在同一个 `BEGIN IMMEDIATE` 中完成。

- 无 receipt 且无旧显式 expected_revision：`checkpoint_base_required`，返回先读 checkpoint 的恢复动作；不发生 append。
- receipt 属于其他会话/owner/工作段/进程实例：`checkpoint_base_mismatch`；不自动转换。
- 原始 head 已变化：`checkpoint_conflict`，只返回 current revision 和读取入口；拒绝后不自动读新 head 重试旧 state。
- 旧显式 expected_revision 继续作为显式 CAS 接口；新工具同时给 receipt 与 expected_revision 时两者必须一致。离线写必须显式 node_id 和 expected_revision，不保留省略基线的后门。
- 旧无 base 调用只接受宿主为该调用者确认过的匹配 receipt；无可信快照时修复旧不安全行为，要求先读。已经成功的历史 operation_id 仍先返回原 receipt，不因新增 CAS 重新执行。
- 缓存回退只可携带实际再次展示的那个快照的 base。后台新响应、超时后迟到响应、owner/policy 变更不得替换本次 base；旧 head 被其他会话更新后必冲突。
- 插件重启不恢复内存中的 base；持久化 receipt 不能跨新 instance 自动复用。原 ID 的幂等重放仍返回原写结果；继续写须在当前实例重新收到 state。

双会话 A/B 都读 head=1，A 写成 2 后 B 旧 state 必须失败；B 读 2 后提交新 ID 才能写 3。并发同 ID 同 payload 只追加一次；并发不同 ID、相同旧 base 至多一方成功。

## 8. typed 关系与 adoption

### 8.1 单入口、单语义

保留 research_relate 的 source_ref/target_ref/label/note，按明确 label 和目标类型路由：

新工具固定携带 r2-v1 契约，以下 typed 路由只适用于新契约。旧入口/历史 relation 保留原 scientific 语义与 receipt，不把早先自由标签同名的边追溯改成调度/支撑边；需要 typed 语义时显式追加新事实。

- 普通 scientific 关系沿原 relations 事务，不提升为调度依赖。
- depends_on/branches_from/revises 路由到 node_dependencies 的领域写入。source 是消费者/后继，target 是前驱节点或它的 publication/item；从 publication 可唯一解析 owner/material，歧义报错。depends_on 必须有明确材料；其他两类不因此变成调度边。
- 节点 proposal 的 fixed inputs 不改写。后加边记录为追加关系；只允许修改尚未 dispatch 的节点调度依赖，拒绝自依赖/环/活跃任务重排。正在运行的节点采用后来的材料使用 consume 或知识引用。
- grounded_in/answers/supersedes/complements/challenges 若 source 为知识版本，进入知识 revision 事务，**创建新版本**，要求输入对应修订理由和显式失效语义；不能在旧 revision 上追加边。返回新 fixed ref；旧 ref 的出版 pin 保持不变。
- motivated_by 只记录启发关系，不生成支撑边，也不沿它传播失效。新增 grounded_in 继续成为依赖并触发晚到影响检查。
- reviews 路由第 4 节的审阅事务。未知 label 保留原普通 scientific 语义，不能借同名字符串绕过保留 label 的校验。

抽出 `propose` 内已有 predecessor 校验和接收 db 的写入函数，供追加 typed 关系复用；它目前不是一个现成的独立公开写 API，不能直接调用 propose 重建节点。知识 route 同理复用 revise 的事务函数，不嵌套两次 `_mutate()`。

### 8.2 派生采用信息

adoption 读取汇合三类事实：proposal fixed inputs、material_consumptions 的显式采用、当前 attempt 内知识 evidence/dependencies/grounded_in 对材料的显式使用。

- 保留每条来源标记与 originating ref，不为每次引用额外写一条 consume；没有 structured citation、但确实已采用材料时仍可显式 consume。
- 聚合键为 `(adopter node, attempt, fixed material ref, relation/purpose)`。重复相同使用合并展示，保留全部来源；不同 attempt、relation、use_text/purpose 不合并，不用语义相似度判断。
- 同一 operation_id 是请求重放；不同 operation_id 的相同显式 consume 若在同 attempt 同键，返回原采用事实并标记 deduplicated，不追加相同事实。不同用途继续追加。
- 历史 consume 不删除、不改主键。知识引用没有可可靠归属的旧 attempt 时，attempt=unknown，展示为未知归属，不能并入当前 attempt。
- 新增 `research_query(collection="adoptions")` 分页及 provenance 展开；已有 refs 与来源仍可追溯。不把 adoption 解释成 evidence verified 或科学结论通过。

派生 use/purpose 使用固定来源标识（proposal_input / evidence / grounded_in），显式 consume 的 use_text 保留原文。没有精确用途对应时保留为不同使用项，不做自然语言同义合并；工具说明明确“已经有 structured citation 时无需再 consume”。

## 9. M2：先采集调用事实，再改协议

M2 作为第一个可运行切片，先在 R1 契约上建立基线，然后用同一口径采集 R2。不从历史空表推导零成本。

S0 的可重复基线是在本基线提交的临时副本中运行相同确定性宿主/工具序列，覆盖成功与拒绝；它衡量协议开销和错误恢复，不代表真实模型自主选择工具的成本或研究收益。真实模型数据属于第 14 节的 M4，不能拿模拟调用冒充。

### 记录边界与落盘

- 在本插件已获可写运行授权的关联研究会话 dispatch 外层记录 start/end，覆盖参数校验、权限拒绝、领域冲突、成功、异常与取消。独立只读 attach、project_read/board/preview、非研究会话不创建目录或日志。运行会话中的 research_query 可以记录调用观测，但查询自身的数据库事务仍为只读，不与独立只读浏览混用。
- 采用 `.research/observations/<plugin_instance_id>.jsonl`，单进程追加；每条完整 JSON 一次 append。避免多个宿主实例争用同一个文件，不新增后台写入队列或上传服务。
- 必要字段：invocation_id、operation_id、session/owner/attempt/generation、tool/contract、started_at、duration_ms、outcome、error_code、transport_attempt、replayed、deduplicated。归属在 start 时冻结；分别记录模型原始入参字段数/编码长度、宿主补入字段数和 preset 展开字段数供负担比较，但不保存 prompt、文件正文或任意原始 tool payload。
- start 在业务操作前保存，end 在业务结果后保存；日志不加入 `_mutate()`，因此领域事务回滚、服务崩溃也不会回滚拒绝记录。缺 end 保留 outcome=unknown；尾部不完整行标记日志覆盖缺口，不补造成功。
- 日志写失败按已知 I/O 边界显式上报 coverage 缺口；已提交 mutation 的结果不改成“业务失败”。不加 broad catch/pass，不自动重试记账，不把失败计为零成本。
- StorageClient 记录原本就发生的最多两次超时重试；transport ID 与 operation ID 分开。模型修复参数后另发调用是新 invocation，不能靠相似文本标成同一次重试。

### 模型与执行使用量

用实际 step.started 的 model/provider/variant，step.ended/failed 与 usage.recorded/updated 的 token/cache/cost（有时可得）建立宿主事件观察。按原生事件 ID/sequence、session、assistant message/step 去重；usage 更新覆盖对应观察，不能把 step 与 usage 两次相加。reasoning/cache 是否包含在 total 中遵循本地 SDK 的 TokenUsage.total 契约，报告分项与统计定义。

继续复用 `usage_observations/adjustments/coverage_gaps` 保存可归属的使用量，补齐新版宿主事件到现有模型的适配，不新建影子账本。新挂接的实时 JSONL 与数据库事实按 native source key 对应；没有可映射的 turn/step 就保持 unknown，不按“当前活跃 attempt”猜归属。

捕获能观察到的 shell/外部工具调用 ID、起止、退出状态与显式 execution_refs；看不到 shell 子进程实际模型或 CPU/GPU 使用时记录不可计量。不得把配置模型当实际模型，也不抓取 provider 请求体来估算成本。

离线脚本 `scripts/r2_measure.py` 输出调用成功/拒绝/重放/重试、耗时中位数与尾部、字段负担、重复采用、模型使用量及覆盖缺口。成果页只复用现有用量摘要，不增调试入口。

## 10. M3：有意义的推进与持久预算

### 10.1 进展按领域事实判断

用服务端提交的领域事件及 source_sequence 替代 `writes=true`。每轮只消费尚未处理的事件，持久化 last_event_sequence/idle_turns；失败或幂等重放不得再次清 idle。

| 角色 | 可以计作进展 | 不单独计作进展 |
|---|---|---|
| main | 合法新节点/dispatch 状态推进、采用有新内容的节点结果、精确风险处置或审阅闭合、有效最终发布/结项 | note、checkpoint、普通 query/relate、重复 propose/consume、只切状态、空发布 |
| node_core | 新证据支持的知识/有效修订、形成新材料的 publication、实质风险处置、finish/明确 blocked | 相同事实重写、文案 editorial、note/checkpoint/snapshot、wait/query、重复关系 |
| specialist | 仍是前台单次任务，不进入 Engine 自动循环；报告完成记录为父任务结果 | 不因“委派已发出”或空报告重置父角色 idle |

这是结构性推进信号，不是研究质量评分：验证存在实质字段/材料/目标变化，不让另一个 LLM 每轮打分。waiting 且确有运行子任务时保持现有等待机制；收到结果后只有实际处理才记进展。保留现有 IDLE_LIMIT，暂停理由改为“连续若干轮没有研究推进”，不能继续说“没有写入任何记录”。

### 10.2 预算的执行范围

首版硬门覆盖**插件控制的自主调度**：所有 engine synthetic/resume、节点 kickoff、specialist kickoff 都在调用宿主前进行持久化 reservation；预算也在 propose/dispatch/工作段切换时检查。用户手动发起、插件外模型和 shell 内部无限循环无法由这些接口保证全局硬上限，capabilities/界面必须写明范围。

- `max_session_turns`：按当前 generation 的 owner 和实际 session 记录受管执行轮次，换 session 不重置 owner 已用额；一轮可有多个 provider step，后者另外计量。失败且未确认宿主未开始的 reservation 不自动返还，标记 unknown。
- `max_total_nodes`：整个项目的节点总量，不是并发数。检查和 propose 插入共用事务；旧项目已有节点计入总量，不能用重新 generation 绕过。单次调度幂等不重复占节点名额。
- `max_wall_seconds`：从明确启动当前 generation 开始计算持久 deadline，包含暂停和重启时间；resume 不刷新。超时停止后续自主调用、撤销排队任务并请求中断受管活动 session；退出未证实保留 unverified，不声称外部进程已被杀死。
- token/cost：已有真实观察可用于“达到已观测额度后停止后续调用”；有已知请求上限时可预留。无可靠输入/输出/费率上界时不得称为不可超支的全局硬预算；缺使用量显式 unknown，不按 0 发放额度。
- 预算修改是宿主显式配置操作，记录原值、新值、actor、reason；到限不能靠普通 /research resume 清零。旧运行迁移为 cold，不自动重新调度或补造历史消耗。

增加 host-only `runtime_budget_set(limits, reason)`，复用项目配置写入口；限制值只能为正整数或显式 disabled，cost 另允许正小数，generation 启动在同一事务固定 started_at/deadline_at。只读查询返回 configured/disabled/unknown 和执行范围，不将缺 key 解释成已有硬门。到限后的提额保留已用额，更新限额不能创建新 generation 洗掉消耗。

预算参数复用宿主项目配置/已有运行状态，不增加模型每次调用的预算字段。首批 R2 测试项目必须显式设置三个有限上限；契约测试使用 5 turns / 3 nodes / 60 seconds 的小额度。**本次不凭空改变存量项目默认运行时长；面向全部新项目的默认额度随试点数据另定。** 未配置的额度显示 disabled，不能显示“硬预算已启用”。

S0 核实 synthetic/resume 的真实启动语义和 hook 顺序，列出全部调用点。若 SDK 不能在手动请求前阻断，就保留上述自主调度边界，不能靠执行 started 后扣账宣称事前阻止。

## 11. 迁移、兼容与呈现

### 迁移

1. 只读打开 schema 9/10/11 均不得建表、改配置、写 context/receipt 或创建日志；capability 对旧库明确标记生命周期不可用，不能以空列表伪装“没有义务”。
2. 可写路径按 9 → 现有 10 → 11 顺序升级；先 SQLite 一致性备份，再单事务 DDL/候选解释/版本号提交。迁移失败回滚，重跑不重复候选。备份校验原库 schema、integrity 和必要行数；不能把随意复制 WAL 活跃文件当一致备份。
3. 旧 todo 的 ID/state、知识版本、disposition、conclusion、requests、execution_refs 全部保留。旧 candidate 目标不能精确确定时 unknown；不回填“已独立审阅/已执行/已解决”。
4. 存量项目只读浏览不升级。首次明确的可写 attach/takeover/research 操作显示迁移及运行契约版本，保持 cold，用户实际继续研究后才开始新运行；不因看板刷新/插件启动自动重开已结束项目。
5. 新写契约的 CAS 修复适用于 Legacy 和 R1，不是可绕过的 feature flag。R2 生命周期采用 capability 及新写事务生效；历史结项自述保持原意义。

### 界面

依据检查显示当前义务、候选数量和可分页历史，按目标解释“为什么要审阅／为何完成／报告链接／风险是否仍未解决”。候选默认折叠，不用旧 pending 总数制造阻断。out_of_scope、waived 和 completed+unresolved 文案必须可区分。

研究中的现有运行详情显示有意义推进暂停、预算值/使用/未知范围。没有独立上下文检查页、M2 仪表盘或第四主入口；只读查看不隐式改变 scope。

### 回退

读取策略可回切 Legacy；这不关闭 CAS、不降 schema、不恢复不安全无基线写。需要回退代码时先停写、保留升级后完整库及日志；旧 schema 备份只在副本恢复。禁止将丢弃 R2 新记录称为无损回退。

## 12. 实现顺序与完成判据

按依赖推进，不承诺缺少容量依据的日历工期。内部可以提交多次，但 R2 功能不完整时不发布给真实项目验收。

| 切片 | 实现内容与主要文件 | 依赖 | 完成判据 |
|---|---|---|---|
| S0：M2 基线与契约夹具 | `plugin/index.ts`、`storage.ts`、新增 `plugin/src/observations.ts`；`scripts/r2_measure.py`；真实 SDK 类型/交付/启动顺序探针 | main 基线 | 参数拒绝/事务回滚/IPC 中断均有独立观测；冻结 checkpoint 消息结构、预算调度点；记录 R1 调用基线 |
| S1：schema 与公共事务 | 新增 `schema11.py`、`review_lifecycle.py`、`checkpoint_receipts.py`、`runtime_budget.py`；`native_store.py`、`service.py` | S0 契约 | 9/10/11 读能力与 9→10→11 迁移通过；旧 receipts 精确重放；事务及身份契约冻结 |
| S2：强制 checkpoint CAS | `memory_store.py`、`query_store.py`、`service.py`、`plugin/src/research.ts`、`plugin/index.ts` | S1 | 双会话、原始 head 隔离、缓存、重启、连写、权限与幂等场景全部通过 |
| S3：候选/义务闭环 | `review_lifecycle.py`；publish/finish/revise/consume/workflow 调用点；`epistemic.py` 复用；`context_policy.py`/Legacy 提示；query/workbench/依据检查 | S1；与 S2 同一 owner 契约 | 精确目标提升/闭合/离开/重入通过；读零写入；风险不因任务结束被隐藏 |
| S4：窄工具/preset/来源 | `tools.ts`、`schema.ts`、`service.py`、`memory_store.py`、engine/角色提示、`field_checks.py` 读取复用 | S2/S3 | 新旧契约等价；常见输入减少；来源不造假；返回值与持久化检查相同 |
| S5：typed relate/adoption | `native_store.py` 的 node/consume/relate 域函数、`memory_store.py` 支撑边、`query_store.py`、`tools.ts` | S3/S4 | 不修改旧 pin/version；晚到影响传播；引用可派生采用，同用途重复减少而信息保全 |
| S6：M3 与整体验证 | `runtime_budget.py`、`workflow_store.py`、`engine.ts`、`specialists.ts`、调用观测适配、现有运行详情 | S0/S1；事件类型在 S3–S5 稳定 | note/checkpoint 刷屏不能无限续跑；跨重启预算持续；全部调度点先预留；不夸大外部限额 |
| S7：文档、打包、统一验收包 | README、plugin README、CHANGELOG、sync-python 白名单、新验证证据目录 | S0–S6 全部通过 | Python/Bun/tsc、独立 tarball、旧项目副本验证及统一用户验收步骤齐全 |

M3 可在 schema/事件冻结后与领域实现交错开发，但它是本次 R2 的交付门。M4 定向任务夹具随 S0 准备，真实模型运行另见第 14 节。

## 13. 必须覆盖的验证矩阵

测试覆盖结果和已知缺口，不为包装函数、文案变更逐个编写镜像测试。新用例优先扩展现有 `tests/`、`plugin/test/` 的真实 service/Store/宿主模拟测试。

| 类别 | 核心场景 | 必须成立 |
|---|---|---|
| checkpoint 并发 | A/B 同 head、不同 ID 并发；同 ID 并发与丢响应重试 | 至多一个新 revision；stale 冲突；原响应可重放 |
| checkpoint 交付 | 预算省略/截断 state、旧 cache、迟到新响应、工具结果未进入下一请求、同轮并行/串行写 | 不凭元数据/后台缓存/execute.after 偷换 base |
| checkpoint 归属 | main 有 node 工作段、node→project、跨 session/owner、attempt/generation 变更、进程重启、旧全局隔离 head | 不越权；原始 head 与 selected 分离；缺基线先读 |
| 旧接口与幂等 | 升级前五类 action 的原 ID/payload；checks 文件状态改变；旧无 base 的已成功/首次调用 | 原成功 receipt 原样；新无可信 base 拒绝；不同 payload 冲突 |
| 生命周期 | 同 todo 多版本；同 affected_version 两个 change；多 owner/generation；项目 scope 的省略/空集/替换；unknown scope；离开/重入与 reopen | obligation_id 精确定位；不跨目标闭合；旧报告不撤销 reopen；历史事件可查 |
| 审阅链接 | 空/自引/非精确/部分/超时/完整 supports、challenges、inconclusive；waive/reopen | 仅任务合理完成；unresolved 和 challenge 始终可见；coverage 未评估 |
| preset/来源 | editorial/correct_previous/custom；冲突字段、多版本纠错、来源保留/旧来源空/unlinked、新证据 | 展开确定；不扩大失效范围；不从作者推导执行来源 |
| checks | record/revise 的 consistent/inconsistent/not_checkable；失败事务 | 响应等于落盘查询；不生成虚假 pass；失败无半条 revision |
| typed relation | pub/item owner；歧义；调度环/运行中重排；知识关系新 revision；grounded_in vs motivated_by | 不猜科学/调度关系；不改旧 pin；仅支撑边传播 |
| adoption | 显式和派生重复、多 purpose/attempt、历史未知归属、同 ID/不同 ID 重复 | 汇总减少重复但保留来源，不删除历史/合并不同用途 |
| M2 | 参数校验、权限/领域拒绝、IPC 超时重试/崩溃、取消、日志写失败/缺 end；usage 更新/重复事件 | 失败不随回滚丢失；结果与观测状态分开；未知不计 0、不双算 tokens |
| M3 | 纯 note/checkpoint 循环；重放/no-op/editorial；有效进展；等待真实子任务；重启/重复事件 | idle 只被有意义新事件重置；等待不空转；重启不清账 |
| 预算 | 两会话争最后节点名额、所有 kickoff/resume、到限/暂停后重启、改额、未核实退出、外部执行 | 原子预留；超额不继续自主调度；执行范围诚实 |
| 只读与迁移 | schema9/10/11；9→10→11；中断/重跑；只读 board/query/preview | 原库不写；原子迁移；新字段不把历史 unknown 洗成 completed |
| UI/包 | 80/132 列；候选历史分页；completed+unresolved；旧项目；独立 tarball 启动和 migration | 三主入口保持；实际加载包内新 Python；不依赖源码树 |

继续运行 R1 风险夹具和来源/权限/结项回归。R2 的“无空集 reviewed/resolved”验收限于本次新增生命周期派生，不能用它宣称 R3 旧结项漏洞已经修复。

真实项目兼容检查只用稳定 SQLite backup 或 DB/WAL 一致副本；验证前后记录行数/必要摘要。已结束项目可验证迁移、读写兼容和历史保留；运行时 CAS、自动推进和新工具效果必须在副本中的新工作段/测试项目执行，不能靠对已结束项目切一次 policy 证明。

## 14. 发布门与统一验收交付物

R1 实施记录已有 481 Python / 66 Bun、TypeScript、独立打包及副本检查，这是历史基线，不冒充本次 R2 已运行的结果。R1 尚无 M4 真实模型配对结论，不阻止开始确定性的 CAS/生命周期修复；R2 发布也不能据此宣布研究质量提升。

R2 开发完成需要同时满足：

- S0–S7 与第 13 节全部必要检查通过；关键风险无新增漏项、无 stale 写穿透、无来源伪造、无旧标签改写。
- 相同任务中常见 checkpoint 的模型必填仅 state；常见 editorial/correct_previous 不再重抄版本和失效范围字段。以实际 schema/input 统计，不拿工具介绍字数代替。
- 普通候选不会自动成为当前阻断；精确审阅闭合有效；未解决风险可继续展开；跨调用重复 consume 降低且采用信息完整。
- M2 有修改前/后的同口径报告，M3 在受管范围稳定阻止超额；观测缺口和未配置预算明确显示。
- README/plugin README/CHANGELOG、错误恢复、兼容例外、迁移/回退说明，以及包内模块校验齐全。

交付 `docs/evidence/r2-runtime/README.md`（实施时创建），索引自动化结果、迁移副本报告、调用测量、终端帧、tarball 校验和一份统一用户验收脚本。验收顺序：旧项目只读/历史 → 可写副本迁移 → 测试项目双会话 CAS → 真实后续研究的新工具/审阅/预算闭环。开发期间不修改真实项目数据、不替换共享本地插件、不重启共享 OpenCode、不擅自调用真实模型。

M4 准备同模型/variant、工具、数据、预算的旧/新契约配对任务，包含常规修订、纠错后继续、陈旧 checkpoint、负结果和反证，另与单 agent 对照；用独立评分统计正确性、过度概括、风险披露、修正速度与总成本，保留随机重复和不确定性。实际模型实验在完整验收包形成后单独执行；样本不足保持 opt-in，不扩大默认部署或宣称普遍收益。

R3 的完整 synthesis 只读路径可在此期间设计；只有 R2 的 exact-version、review lifecycle、CAS 与预算契约稳定后，才接入新结项事务。R4 继续等待 R1–R3 的效果门，不随本计划自动启动。
