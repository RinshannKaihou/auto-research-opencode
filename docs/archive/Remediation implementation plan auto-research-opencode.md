> **已废止的历史规划。** 2026-11-01 归档；下文保留原时间、基线和交付描述，均不作为当前开发要求。当前以 [探索设计](<../Exploration and multi-route management design.md>)、[新路线图](<../Remediation roadmap auto-research-opencode.md>) 和 [新实施计划](<../Remediation implementation plan auto-research-opencode.md>) 为准。归档时只增加此说明并调整跨目录链接。

# 当前项目修复实施计划

日期：2026-10-31（Asia/Shanghai）

状态：计划与基线核验完成；尚未实施产品修复。
代码基线：`0476d81f6463ce13ee03b9de87463a03f5237d9c`，产品版本 v0.1.1，ledger schema 10。

**2026-11-01 优先级更新：** 上述日期、状态和基线描述原始计划。R1、R2 与后续运行控制/协调者边界已合入当前代码；按用户最新要求，下一阶段优先 **R3 + 研究探索能力**，R4 后置。具体范围、DeepSeek 分析核验、多路线管理和分阶段开发建议见 [探索能力设计](<../Exploration and multi-route management design.md>)。下文保留原始 R1–R4 工作项定义，发布顺序以本次更新为准。

依据：[Remediation roadmap](<./Remediation roadmap auto-research-opencode.md>)。该文档决定交付顺序；早期 Improvement roadmap（本地历史记录） 补充证据、审阅、预算和评估语义。两者有差异时，以 Remediation 的明确修订为准，尤其是“确认性验证可选”和“非核心开放问题不一律压低 outcome”。

本计划采用四次发布：**R1 恢复上下文可见性 → R2 简化写入和审阅生命周期 → R3 在决策点闭合风险 → R4 可选确认性验证**。测量从第一步开始。首个切片只改读取与展示，不新增 prediction、candidate registry、data-consumption 必填表单，也不增加 revision 必填字段。这里 R2 的 *review candidate* 是审阅候选，与 R4 的 *experiment candidate* 分属不同概念。

## 1. 本次核验得到的实施依据

### 1.1 代码与行为

| 已核实事实 | 代码落点 | 修复含义 |
|---|---|---|
| 协调者只选 disputed、revised、lesson、project 四组，普通节点 claim/observation 没有正常入口 | `memory_store.py:1490–1509` | 不能只调大上下文预算；需要改变候选选择 |
| 协调者 compact 条目省略 scope，统一压缩还可能把 ref/kind 截成省略标记 | `memory_store.py:1702–1739` | 标识、风险字段必须稳定；scope/conditions 要随发现显示并保留展开入口 |
| pending review 排在 knowledge 前；hints 内 publication risk 排在三类文书提示之后，再统一截取前 8 条 | `memory_store.py:1636–1643`；`hints.py:18–22,149–180` | 先独立选择 critical 风险，再限流常规提示，不能仅缩短现有数组 |
| publish、finish、revision、correction note 会直接产生 pending todo；插件没有日常闭合入口 | `memory_store.py:539–548`；`native_store.py`、`workflow_store.py` 的调用点 | R1 改显示，R2 才改变生命周期；不清空历史表来改善指标 |
| field-check 结果被 context 去掉，写入响应也未重新读取已计算结果 | `memory_store.py:1587–1591`；record/revise 路径 | R1 展示已有风险；R2 在写入返回中补齐持久化结果 |
| checkpoint 只有传入 expected_revision 才执行 CAS | `memory_store.py:1034–1091` | 必须补齐强制基线，不能把它描述成“只隐藏已有强制检查” |
| 插件只缓存 context 字符串，没有 checkpoint snapshot/base 管理 | `plugin/src/research.ts:356–387` | 需要结构化读取凭据，并处理 stale cache、重启和 owner 切换 |
| revise 已区分作者身份与执行引用；省略 execution_refs 会存空 | `memory_store.py:550–559,727–815`；`service.py:1093` | 保留该语义，不能把当前 session/attempt 自动当成证据生产者 |
| publication_check 已将空引用标成 coverage unknown；conclude 没有调用当前检查 | `epistemic.py:880–908`；`conclusion_store.py:83–132` | 复用已有风险计算，把它接入最终决策；不要另造“空集通过”判定 |
| review.status 目前为自述，存在冻结材料就可填写 reviewed | `conclusion_store.py:118–132`；`tests/test_p0_contracts.py:181` | 新增逐版本 coverage 时，必须区分历史自述与新推导状态 |
| 所有成功 writes 都会重置 idle，包括 note/checkpoint | `plugin/index.ts:129`；`plugin/src/engine.ts:129,164` | 保留预算/推进修复配套轨道，不塞进首个上下文切片 |

以上路径除 `plugin/`、`tests/` 外均相对 `src/auto_research/`；行号对应本计划代码基线。

三个临时项目探针进一步确认：

1. 普通节点 claim 带 scope/conditions、被 complete publication 固定引用并 finish 后，协调者默认 knowledge 仍为空；`knowledge_query` 可查到该条目。
2. checkpoint 已从 revision 1 更新到 2 后，省略 expected_revision 提交旧状态仍被接受为 revision 3。
3. 最终发布没有 knowledge_refs 时，publication_check 为 coverage unknown；仍可用最终报告自身作为 review.refs，以 answered + reviewed 结项。

这些是现有契约缺口，不是本次测试失败。可复现脚本和输出见 [行为探针](../evidence/remediation-plan-20261031/probe_current_contracts.py) 与 [探针结果](../evidence/remediation-plan-20261031/contract-probes.json)。探针的写入均发生在自动清理的临时项目。

### 1.2 测试与真实运行记录基线

| 检查 | 本次结果 | 证明范围 |
|---|---|---|
| `PYTHONPATH=src python3 -m pytest -q` | 439 passed，36.47s | 当前 Python 契约回归 |
| `cd plugin && bun test` | 51 pass / 0 fail，4.92s | 插件、假宿主、看板、独立打包链路 |
| `cd plugin && node_modules/.bin/tsc -p tsconfig.json --noEmit` | 退出码 0 | TypeScript 类型检查 |
| 环境 | Python 3.11.4；Bun 1.3.10；Node v22.22.3；TypeScript 5.9.3 | 本机核验环境，不代表全部宿主环境 |

本次没有启动真实模型研究、重启 OpenCode 或修改已有研究项目。测试通过不能推导出研究质量改善。

另对八个实际 schema-10 项目进行了只读统计；公开副本不列项目名称。对数据库及存在的 WAL 做字节稳定性核对后，复制到临时目录读取；八个副本 integrity_check 均为 ok。保存时间为 **2026-10-31 02:39:03 +08:00**，逐项目文件哈希和事件上界保存在 本地原始核验记录（未公开）。这不是八库全局同时刻快照。

| 指标 | 本次快照 |
|---|---:|
| 保存的协调者 context requests | 1,068 |
| 节点 / knowledge entries / knowledge revisions | 63 / 313 / 325 |
| 结项记录 / review todos / knowledge impacts | 13 / 168 / 16 |
| pending_review 字符占比中位数 | 18.5% |
| pending_review + structure_hints 字符占比中位数 | 33.1% |
| knowledge 字符占比中位数 | 20.4% |
| 每项目最新 context 中，上述两个流程块占比 | 28.2%–43.4% |
| 每项目最新 context 中，knowledge 占比 | 14.3%–23.6% |
| usage_observations / turn_bindings | 0 / 0 |

路线图的 1,055 次、161 条 todo 是较早快照，不能被本次数字静默替换。部分项目仍在产生记录，计划采用已落盘的本次统计作为新的规划基线。所有 168 条 review todo 仍为 pending。字符口径包含块标题及 JSON，不含块尾空行，按请求加权；它不是 token 占比。

统计还能区分“含 statement/ref 的显示位置”和“格式完整的去重固定引用”：两者中位数分别为 4 和 3。后者仅匹配引用格式，未逐项解析或查库验证。它们不是路线图中“选择了五项”的同一口径；截断占位符不算格式完整的引用。协调者渲染的这些条目没有独立 scope 字段；conditions 字段存在不代表内容未截断。

**测量限制必须随结果保留：** `requests` 仅记录已提交 mutation，不能据此计算全部工作、拒绝率或重试率；context_record 异步写入且吞错，保存覆盖率未知；历史 token/算力成本不能从空表还原为零。

## 2. 全阶段共同契约

- 冻结发布、追加式知识版本、typed support propagation、所有者隔离、事务和请求幂等继续有效。
- unknown、未覆盖、截断、无法检查都不能转换成 pass。空引用集合不能推导出已审阅或证据闭合。
- 作者是谁与证据由谁执行产生分开记录；linked session/attempt 本身不等于执行验证。
- 后端校验结构、引用、时序和声明是否完整，不判定科学真伪、核心重要性或因果关系。
- 相关风险不能按年龄、candidate 标签或全局 in_use=false 自动隐去；相关性未知时保留风险及未知原因。
- 读取、压缩、看板浏览不得写关系、关闭 todo 或改历史标签。
- 审阅工作完成、科学风险已处理、结项回答程度是不同状态。
- 探索、失败实验、负结果和事后解释无需填写确认性协议全套字段。
- “减少调用、增加关系/审阅数量”均是诊断指标，不是研究质量目标。

## 3. R1：恢复发现可见性，折叠常规审计负担

目标：协调者能看到近期节点实际学到了什么；当前风险保持可见；历史事务仅按需展开。以纯读模型和可切换策略实施，保持 schema 10 写语义。

| 工作项 | 实施内容 | 主要文件 / 依赖 |
|---|---|---|
| R1.0 测量与可切换读取策略 | 从 context_view 分离选择、风险汇总和渲染；保留旧策略，输出 policy_version、source_sequence、选择原因、字符计数、截断和覆盖信息。选择和渲染在同一读事务内完成 | `memory_store.py`、`plugin/src/research.ts`；先于新版默认展示 |
| R1.1 critical now | 对当前节点 inputs/dependencies、待综合材料、当前 final_ref 分别建立决策范围；汇总撤回依据、待处置 impacts、相关 inconsistent/not_checkable、publication attention。先全量筛选再分页，独立于 clerical hints 的限额 | `hints.py`、`epistemic.py`、`field_checks.py`、`memory_store.py`；依赖 R1.0 |
| R1.2 findings | 增加近期完成节点 claim/observation、新发布 pinned knowledge、当前问题及声明依赖的检索通道；fixed/current 版本并列标出修订风险；跨组去重；disputed/revised 保留独立小配额 | `memory_store.py`、`query_store.py`；依赖 R1.0/R1.1 |
| R1.3 backlog 与按需展开 | routine todo 改成总数、类别、最多少量相关例子；clerical hints 从默认长块移出。query 保留全部记录、筛选和分页；看板提供实际列表、详情和展开入口 | `query_store.py`、`workbench_read.py`、`plugin/src/board/*`、`plugin/src/tools.ts` |
| R1.4 shadow 与发布门 | 同一数据库读快照构建旧/新两份 context；比较风险、发现和开销；先切测试项目，再切新项目默认，旧项目显式切策略 | 回放夹具、context tests、plugin tests；依赖前三项 |

具体设计决策：

1. **决策相关性要有来源。** 优先采用 task/input、显式综合引用和知识支撑边。当前问题的词项检索可使用已有 tokenizer，作为可见性补充，不用它裁掉风险，也不先引入向量库。global anchor_set 只能帮助判断是否仍被使用，不能冒充当前决策集合。
2. **固定引用不能被压成省略号。** 先为 ref、node、kind、status、risk 与展开入口分配预算，再压缩 statement、scope、conditions。条件过长时注明“未显示完整，必须展开”，不可把截断后的条件当完整适用范围。源记录缺条件则显示未记录，不生成条件。
3. **覆盖所有完成节点不等于每轮装下所有全文。** 每个完成节点都有发现索引；首次交回协调者时展示至少一项普通发现，或明确“未登记发现”。同批完成过多时列出未展开节点与游标；后续 query 能穷尽，不能因采样永远不可达。
4. **critical 风险多时不静默丢尾部。** compact context 保留风险数量、完整性、具体风险索引或可分页索引及强制展开提示；R3 决策前读取全部当前风险。小预算测试区分“摘要可见”与“详情读全”，不能只看到总数就宣称 100% 风险详情召回。
5. **默认预算是工程参数。** 建议 routine backlog 先限制为约 600 字符、最多 2 个例子，clerical 默认仅计数和入口；同状态 shadow 中以常规审计字符中位数至少降低 50% 为初始目标。该数值是本计划提出的试运行门槛，不是已证实收益；若发现/风险不完整，即使达到比例也不得通过。
6. **field-check 摘要纳入 R1。** consistent 只表示声明字段与冻结材料一致；相关 inconsistent/not_checkable 必须显示原因与展开入口，不能期待原有 structure_hints 自动覆盖它们。

R1 发布验收：所有当前关键风险夹具可发现，人工标注的关键风险召回 100%；无关 backlog 增长不挤掉近期发现；完成节点发现索引覆盖 100%；source 中已存在的 scope/conditions 在 compact 中可见或明确标记截断；query/board 可取全；读前读后事件、版本、关系及 todo 状态不变。风暴场景中 routine 流程块不大于 findings 块，不能通过缩小风险集合达标。

### 首个可直接开工的切片

按四个可单独审查的提交推进：

1. 加三类失败夹具和旧策略快照：普通发现遗漏、9+ clerical hints 挤掉 attention、当前撤回依据/检查失败。
2. 引入只读 policy 选择器及四块视图 `critical_now / findings / review_backlog / clerical_index`，保留旧策略开关和已有查询；不改变 todo 状态。
3. 加 completed-node / publication-pinned findings 选择、稳定引用与 scope/conditions 渲染；把常规队列折叠为摘要。
4. 运行保存文本统计、同状态夹具双策略对比及现有测试；给出发现/风险覆盖和字符变化报告。只有这些通过，才把新版策略交给测试项目。

看板分页浏览属于 R1 发布完成条件，可以接在首切片之后；首切片先保留可用的 research_query 展开通道。

## 4. R2：生命周期与写入简化

目标：机器接管版本抄写、路由和明确可推导信息；模型继续声明失效范围、科学关系和证据判断。R2 在 R1 发布门通过后启动；生命周期与工具适配可以并行，schema/事务契约统一审查。

| 工作项 | 实施内容 | 验收关键点 |
|---|---|---|
| R2.1 审阅候选与义务 | 保留原 review_todos 和历史状态；新增 candidate/obligation 解释层与事件记录。普通事件产生候选，当前 load-bearing、affected 或显式 promote 才产生义务 | 候选不自动阻断；精确到 claim version，影响义务复用 `(change_id, affected_version)`；跨目标不误闭合 |
| R2.2 自动闭合 | 记录 promotion/closure 的理由、证据、scope 和版本；合适 review 关闭审阅任务，disposition 处理对应风险；离开范围记 out_of_scope，风险仍保存 | review 完成但 challenge/unresolved 仍可见；重新进入范围会重新评估/激活；例外手动 API 有审计信息 |
| R2.3 窄工具与兼容层 | 新增 record/revise/checkpoint/dispose/narrow_scope 窄工具；旧 research_memory action 接口走版本 adapter；共享 canonical command、权限、事务和校验 | 同语义写入等价；旧 operation_id 重放不因 payload 重写失效；不向模型同时展示两套冗长工具 |
| R2.4 presets 与来源保留 | editorial 展开为 reword + none；correct_previous 展开为 correct + versions + 精确旧版本；custom 保留全字段。新增显式 preserve_sources/no_new_evidence 路径 | 返回实际展开字段；retract/narrow/模糊 correction 仍须显式范围；冲突字段拒绝；旧省略 execution_refs 仍为空 |
| R2.5 checkpoint 基线 | context/query 返回与 session、owner、head 绑定的读取凭据；插件携带模型实际看到的 base，后端强制 CAS | stale、跨 owner、错误 session、重启无凭据均不能当最新追加；重试仍幂等 |
| R2.6 单一关系与 adoption | research_relate 作为统一入口，按类型与目标路由到既有 typed 事务。输入 publication 可推导 owner/material；后续显式 evidence/dependency 构成派生 adoption 汇总 | 不猜 depends_on/branches_from/revises；grounded_in 继续传播、motivated_by 不传播；采用但尚未引用仍可显式 consume |

主要落点：`plugin/src/tools.ts`、`plugin/index.ts`、`plugin/src/research.ts`、`service.py`、`memory_store.py`、`native_store.py`、`workflow_store.py`、新 schema migration、query/board。

必须先锁定的兼容规则：

- **checkpoint：** 旧显式 expected_revision 继续支持；旧无 base 调用只可使用该调用者实际读过且 owner 匹配的快照来适配。没有可信读取凭据，返回可修复的 `checkpoint_base_required`，提示先读取；禁止写入时偷读最新 head 补值。原有“无 base 无条件成功”属于要修复的不安全行为，不能承诺原样保留。错误名称为拟议接口。
- **同轮多次 checkpoint：** 成功写响应产生下一 base；只在模型已收到该响应的后续写中使用。幂等重放仍返回原结果；后台刷新缓存不得替换模型这轮的读取基线。
- **preserve_sources：** 明确声明无新增实验/来源；若 evidence/source 字段变化则要求显式来源，不自动保留。服务端只能校验结构是否冲突，不能证明声明在科学上真实。新增证据没有可验证来源时仍允许探索记录，但保持 unknown/unlinked。
- **关系写入：** 知识支撑关系仍进入版本化知识事务，不能降格成普通 relations 插入从而丢失失效传播。统一入口需要的 base 或 scope 使用同一版本契约，不能绕开。
- **consume 去重：** 同一 operation_id 的重试与跨调用的重复采用分开。派生汇总保留来源标记，按 adopter/attempt、材料、使用关系区分；不删除历史显式 consume，也不合并不同使用目的。
- **审阅义务：** suitable review 的基本链接记录在 R2 实现，R3 再补类型、独立性和全量 coverage；没有精确版本/证据的旧 review.refs 不足以自动关闭新义务。out_of_scope 不是科学风险 resolved。
- **写入返回：** record/revise 计算 field_checks 后重新读取结果，并返回存储后的 checks、展开 preset 和 provenance 状态，避免模型看到计算前对象。

R2 发布验收：常见 revise/checkpoint 的模型必填输入减少；双会话真实陈旧写稳定冲突；没有作者身份冒充执行来源；旧工具和新工具的权限/幂等/存储语义一致，除已明确列出的不安全兼容例外；重复 consume 减少而采用信息完整；旧候选可查且未被静默删除。

## 5. R3：在综合与结项时闭合当前风险

目标：处理会影响当前回答的风险，保留回答的限定条件；不把清完全项目历史候选当作结项条件。

| 工作项 | 实施内容 | 主要落点 |
|---|---|---|
| R3.1 synthesis 完整读取 | 从最终报告声明的知识引用获取 pinned/current 完整记录、scope、conditions、dependencies、evidence、checks、risk、review、provenance；支持分页与读取完整性；生成同源 evidence appendix | `query_store.py`、`service.py`、`memory_store.py`、发布工具/报告生成路径 |
| R3.2 focused conclusion check | whole-publication/item final_ref 解析到 owner；复用 knowledge_support_edges，沿 dependencies 与 knowledge-version evidence_refs 的支撑边取祖先（含 grounded_in，排除 motivated_by）；合并当前 publication_check、field checks、pending impacts、dispositions、coverage 限制 | `epistemic.py`、`conclusion_store.py`、`frozen_refs.py` |
| R3.3 三种状态分开 | 保留 outcome，新增 evidence_closure，review coverage 单独显示；每个相关风险由协调者声明 core impact、理由、证据及 disposition | 新 schema、`conclusion_store.py`、`tools.ts`、board/README |
| R3.4 按风险选择复核 | citation/scope/wording 用只读 specialist；recomputation/reimplementation/adversarial experiment 用普通隔离 node 执行；记录精确 claim revision、类型、结果、实际模型、独立性及处置 | `specialists.ts`、`engine.ts`、review storage/query、board |

结项契约采用以下规则：

| 情形 | outcome | evidence_closure / review |
|---|---|---|
| 核心问题已回答，声明范围内证据问题均有适当处理 | 可 answered | 可 resolved；仅描述已知证据问题闭合，不证明科学真理；review 仍单独计算 |
| 核心问题已回答，保留明确不影响核心结论的机制/边界缺口 | 可 answered | qualified；逐项保留风险、core-impact 理由、证据和结构化 gap |
| 已知未解决风险削弱核心结论 | partial 或 unresolved | qualified 或 unknown；不得 answered |
| 没有知识引用，或不能列明核心回答的依据与引用覆盖 | 新契约暂不能 answered；补齐结构化核心依据/覆盖声明，或以 partial/unresolved 并列明 gap 结项 | unknown；不能 resolved 或 derived reviewed |
| 已声明核心依据，但某个图路径、引用类型或检查结果不完整 | 逐项声明 core impact；核心影响尚不明确时不能 answered；明确属于非核心的缺口可按上面的限定回答处理 | 保留 unknown 项；整体 qualified 或 unknown，不能因空缺而声称已闭合/已审阅 |
| 仅有旧版自述 review.status 或冻结报告存在 | 保留原始记录 | 标明 historical/self-declared，派生 coverage unknown |

这里的“核心依据覆盖”指可审计的核心 claim → 固定证据引用集合，不可用额外 prose 理由或报告自引替代。执行来源未知、没有 confirmatory 注册、未做独立复核，本身不使探索性 answered 失格；这些限制保留在 evidence/review/provenance 状态中。只有当相应未知确实影响核心回答时，才按 core-impact 契约限制 outcome。

实现时要保证：

1. `review.status=reviewed` 不再由任意 frozen refs 自动推导。每个声明为已审阅的 load-bearing claim 都要有适合其类型和精确版本的覆盖，挑战必须已显式处理；没有审阅可诚实结项为 unreviewed/partial。
2. 不要求每个条目/祖先执行同一种审阅仪式。选择复核对象看“出错是否改变结论”和“独立工作是否能增加信息”；不完整覆盖诚实展示。只读 specialist 不能计作 recomputation。
3. 逐风险记录 `core_impact`、reason、evidence_refs、disposition 与 gap；这是模型承担的语义判断。后端保证记录完整、引用匹配、核心 unresolved 不与 answered 共存；不靠关键词自动判定“非核心”。
4. 最终提交在单一事务内重新计算当前风险并保存 sequence-bound conclusion-check snapshot。此前 preview 仅供准备；新修订/撤回使旧 preview 失效时，返回新增风险并要求处理，不能持陈旧 preview 结项。无关事件是否需要重检以安全优先的第一版实现为准。
5. 完整读取、appendix 与 report 的引用版本/摘要哈希保持一致；提交时发现引用或条件变化需重取。字段齐全不证明模型读懂了，标题强度仍需语义复核。
6. 空 final references、未覆盖引用类型、缺失依赖、遍历超限都进入 limitations。后端只检查已声明引用，不能声称发现了正文中全部断言。
7. 减少 declared set 不能自动抹去风险：保留范围变更记录和所依据的理由；从核心结论移走的风险需解释报告范围怎样相应收缩。out_of_scope 不等于原风险消失。

R3 发布验收：`sdc_agent_222` attention 必须逐项处置；空核心依据不能 answered，空知识引用/报告自引不能得到 derived reviewed/resolved；有结构化核心依据但执行来源未知的探索项目仍可按限定回答结项；`sdc_agent_188` 式 answered + qualified 可表达；P-018 式可执行复核能改变结论；无关历史 backlog 保持未清理也能结项；有限零命中上界与“不可能”之间的过度概括进入独立语义评测。

## 6. R4：可选确认性验证

启动条件：R1–R3 已证明降低负担且不遗漏当前风险，研究结果评估未见退化。探索为默认；按具体 experiment/claim 提升为 confirmatory，不能整节点贴标签。

| 工作项 | 内容 | 必须保留的边界 |
|---|---|---|
| R4.1 prediction/candidate/protocol/data consumption | 固定预测、候选身份、数据集与协议版本；记录消费目的、时点、observed/declared 来源；协议修订 append-only | P-019 式看到结果后的修订永久保留 post-hoc；无消费记录不能证明没接触过数据 |
| R4.2 research-type profiles | 数值随机实验、确定性重放、理论证明、硬件故障采用各自所需材料 | 不统一强塞 metric 阈值、seed/interval；零命中要求合适上界及 exposure 单位；硬件/环境身份按需记录 |
| R4.3 执行来源与 eligibility | 需要强来源时使用 mediated runner，记录 command/cwd/environment/source revision、时间/退出状态、输出哈希及必要设备/服务版本；最终按 claim/prediction/dataset 计算资格 | 聊天记录推测只作弱提示；旧 execution_refs 的“可解析”不等于 verified；无可靠隔离边界不能宣称 affirmative data isolation |
| R4.4 多重尝试与比较 | 汇总 candidates、evaluations、data-use、amendments、attempts；把可比性检查放后端，board 复用 | 一个节点试三候选仍是三次评估；不同 corpus/split/单位/指标定义不可直接比较 |

允许提供探索性或 diagnostic 结果，但不授予其不具备的 confirmatory eligibility。prediction satisfied 只是冻结比较满足，不是 hypothesis true；confirmatory_eligible 是证据条件，不是研究质量分数。

R4 发布验收：探索项目无确认性字段也能结项；混合先验接触按证据分类；确定性复现无需伪造种子/区间；理论材料使用假设与证明范围；强来源执行路径的边界有实际隔离验证，仅 chmod/哈希或 shell permission 不能称沙箱。达不到强来源要求时保留 unverified/unknown，并保持该能力 opt-in。

## 7. 配套轨道：测量、推进与研究效果

这些工作来自 Improvement roadmap，重排交付顺序没有取消它们。

| 轨道 | 何时进入 | 交付与边界 |
|---|---|---|
| M1 上下文观测 | R1 起 | 新 policy/version、选择原因、块字符、快照序列、记录失败/覆盖统计；无需模型手工填 |
| M2 调用与使用成本 | R2 前开始采集，随 R2 发布 | 在插件 dispatch/宿主事件边界记录成功、拒绝原因、重试、时间；能获取时采集实际模型/variant/token/cache，关联 shell 和外部执行记录。未知不计零；失败记录不能依赖会回滚的 ledger mutation 事务 |
| M3 推进与硬预算 | R2 并行开发，最迟 R3 前完成 | 基于角色相关事件判断进展；note/checkpoint/query/wait/no-op 不重置 idle。持久化 session turns、total nodes、wall-clock 上限，重启后不丢；token/cost 仅在可计量范围执行，不夸大 CPU/GPU 全局约束 |
| M4 研究效果对照 | R1 准备夹具；各发布跑定向任务；R3/R4 扩大 | 同模型/variant、工具、数据及预算的旧策略/新策略配对对照；再与单 agent 比较。独立评分正确性、过度概括、陷阱识别、负结果、风险披露、修正速度与成本 |

基准不能让框架给自己的最终结论打分；随机任务需要重复运行并报告变异。历史无 usage 的任务只能做案例回放，不能声称完成等预算对照。真实模型实验应在后续实施时运行，不能由本次 490 个测试通过替代。

## 8. 回放语料与验收矩阵

### 8.1 三种回放要分别报告

1. **保存文本统计。** 对路线图 1,055 次历史上下文和本次 1,068 次快照统计占比、重复曝光、可解析引用、条件显示。原始旧集合没有单独快照清单时，不强行截取当前记录来假称完全复现旧数字。
2. **可复原状态的夹具回放。** 从真实案例提取脱敏、最小、可重建的事件/材料，保留关键版本、引用、修订、检查和处置。严格声明哪些状态能够 as-of 还原。
3. **同状态 shadow。** 在同一数据库读取事务/不可变副本上运行旧新策略，使用相同节点、会话、任务、预算。保留 policy/version 与快照标识，作为因果上可比较的上下文结果。

当前 context_view 无历史 bound 参数，hints 也只评估 current ledger；保存的 context_packs 并非完整历史数据库快照。**禁止用当前库生成新 context 与过去 body 对比后宣称“同状态 A/B”。** 第一版无须为所有表开发全量时间旅行；先落地可复原夹具和同状态 shadow。

### 8.2 必须纳入的案例

| 案例 / 风险 | 目标阶段 | 通过条件 |
|---|---|---|
| 普通完成节点发现；大量旧 review/hints | R1 | finding 可见或明确索引；scope/conditions 不被静默丢掉；历史队列仍可查 |
| 第 9+ 条 clerical 后的 attention；未处置 impact 位于分页之后 | R1 | 分级/筛选先于分页，关键项不被文书提示挤掉 |
| 引用已撤回依据但没有 impact row；in_use/scope unknown | R1/R3 | 仍显示风险，unknown 不通过 |
| field inconsistent；相关 not_checkable | R1/R2/R3 | context 与写入返回可见；结项逐项决策 |
| 双会话 checkpoint；stale cache；重启；owner 切换；同轮连写 | R2 | 陈旧提交冲突，缺失 base 要求读取，幂等请求不重复追加 |
| editorial/correct/custom；新证据下 preserve_sources | R2 | 展开可审计，来源不伪造，失效范围不猜测 |
| review 完成但风险 unresolved；离开/重入 scope | R2/R3 | 任务与风险分开，重新相关时恢复义务，不关闭别的 change/version |
| `sdc_agent_222`；结项 preview 后出现新风险 | R3 | 精确处置全部相关项，提交时重新检查，无事务间隙漏报 |
| 历史项目空引用；报告自引作 review | R3 | coverage unknown，不能空集推导 reviewed/resolved |
| `sdc_agent_188` 限定回答 | R3 | answered + qualified 可表达，非核心缺口保留；核心 unresolved 阻止 answered |
| 历史项目评分器分歧 | R3 | 可执行独立复核，能修订结论，不需清无关历史债 |
| P-019 post-hoc；混合先验数据接触；重复消费 | R4 | 修约/数据使用状态不可洗成 preregistered 或未接触 |
| K-021 确定性 equality；理论；硬件零命中 | R4 | 按研究类型验收，避免无意义字段；上界不渲染为物理不可能 |
| schema 9/10；旧结项；只读 browsing；独立 tarball | 每次发布 | 历史语义保留、只读不迁移、迁移失败可恢复、内置 Python 与源码一致 |
| blind specialist；非研究会话；原角色权限 | 每次发布 | 原隔离与无侵入性质不退化 |

已有回归优先复用：`test_memory_store.py`、`test_063_regressions.py`、`test_067_regressions.py`、`test_p0_contracts.py`、`test_delivery_boundaries.py`、`test_opencode_host.py`、`test_workbench_read.py` 和插件 tests。旧 block-name/渲染形状断言可以更新，风险传播、只读、事务、幂等和历史自述的语义断言不得直接删除来让测试通过。

### 8.3 发布判断指标

- **安全硬门：** 关键风险夹具无遗漏；无 stale checkpoint 成功；无空集 reviewed/resolved；无作者到执行来源的自动升级；无历史标签重写。
- **发现硬门：** 完成节点索引可穷尽；新相关发现能进入协调者视图；可解析 ref 与 scope/conditions 展示符合契约。
- **负担：** 按项目/节点类型报告常规审计字符、连续不变 backlog 曝光、必填字段、重复 consume、成功/拒绝/重试调用；报告中位数及尾部，不仅报告全局均值。
- **研究效果：** 独立评分的 claim accuracy、overclaim、trap detection、negative results 和总成本不能因记账减少而退化。样本不足时报告不确定性，保持 opt-in，不宣布已验证普遍收益。
- **结项负担：** 单独检查最后阶段调用/阅读量，防止把每轮节省的文书工作全部堆到结项。只加载当前 load-bearing 记录与风险，不恢复“清所有 backlog”。

## 9. 迁移、发布和回退

1. R1 保持写 schema 不变；新增 policy 配置与兼容返回字段。已有保存 context 的 policy_version 不改写；可回切旧读取策略。
2. R2 起采用增量 schema migration，具体 schema 号由合并时分配。优先新表/可空字段保留原 review_todos、conclusion receipts、execution_refs；历史缺失项保持 unknown，不回填“完成/确认/执行过”。
3. 数据迁移只发生在明确可写路径，先备份并验证，保证原子性；独立只读 project_read/board 不触发迁移。验证从 schema 9 经现有 schema 10 再到新版本的完整链路。
4. capability/version 协商区分旧 action 接口与新契约。旧 API 入口可保留，不等于允许绕过新项目的结项和 CAS 契约；旧无 base checkpoint 的例外已在 R2 明确。旧结项 receipt 保持自述，重开新 generation 才采用相应新契约。
5. 每发布先 shadow，再新测试项目 opt-in，再决定新项目默认；历史项目显式启用新策略/重新审阅，不批量升级“可信”标签。
6. 发布必须更新 README/plugin README/CHANGELOG、工具说明及错误恢复方式，并运行 Python、Bun、tsc 和独立 pack 测试。存在 `plugin/python/` 时启动优先加载它，发布需通过 sync:python/prepack 验证，避免源代码已修而实际运行旧内置包。
7. **功能关闭与数据库降级分开。** 新版已写数据后，回退旧代码未必能读取新 schema；先停写、保留当前完整副本，再选择兼容读取或在副本恢复旧备份。不能把丢弃升级后记录称无损回退。

停止或回退条件：通过隐藏当前风险换取低开销；扩大遗漏发现；把 unknown 当成功；陈旧版本穿透；自动来源造假；要求清历史候选才能结项；研究结果退化或把负担集中到最后一步。R4 未满足收益门时保持关闭，不影响 R1–R3 的价值。

## 10. 实施组织与交付顺序

以可审查 PR 为工作单位，不在缺少试点容量信息时承诺日历工期。

| 顺序 | 可并行工作 | 合并前依赖 / 交付物 |
|---|---|---|
| 0 | 基线语料与风险标注；R1 选择器设计 | 本计划证据 + 最小夹具清单；先冻结测量口径 |
| 1 | R1.0/R1.1；随后 R1.2 与 R1.3 | 四块 context、query/board 展开、同状态 shadow 报告 |
| 2 | R2 生命周期；窄工具/preset；checkpoint base；M2/M3 | 各自契约测试通过后合并 migration 与接口；R1 发布门先通过 |
| 3 | R3 完整 synthesis/read；conclusion contract；review coverage | 共用 load-bearing/version/risk schema；R2 版本与义务语义稳定 |
| 4 | R4 profiles/registries；mediated execution；对照评估 | R1–R3 效果门通过；强来源能力不足时对应资格保持 unknown |

每项实现 PR 包含：代码与对应夹具、接口/迁移变化、受影响旧测试的语义解释、负担/风险结果、关闭或回退方式。R1 首切片完成后的下一步应由测量结果决定，不能因为工具接口更容易改就先扩写入协议。

## 11. 本次交付与复核方式

本次只新增计划及核验证据，未改产品源码、原路线图、已有研究数据库或宿主配置；未执行真实模型实验。

- [本地统计脚本](../evidence/remediation-plan-20261031/inspect_local_runs.py)：显式传入研究根目录，先检查数据库/WAL 稳定性再读临时副本，只输出聚合统计，不导出研究正文。
- 本地基线 JSON：保存样本、哈希、计数、统计口径和限制；原始记录不随公开仓库分发。
- [当前契约探针](../evidence/remediation-plan-20261031/probe_current_contracts.py)：临时项目复现三个缺口。
- [探针结果](../evidence/remediation-plan-20261031/contract-probes.json)：记录当前行为，不作为修复已完成的证明。

重跑探针：在仓库根执行 `PYTHONPATH=src python3 docs/evidence/remediation-plan-20261031/probe_current_contracts.py`。重跑本地统计：将 JSON 中 `projects[].root` 逐个作为 `inspect_local_runs.py` 的位置参数；新结果另存文件，避免覆盖本次规划基线。若源数据库/WAL 在复制期间变化，脚本报错退出，需另选稳定时刻重试。
