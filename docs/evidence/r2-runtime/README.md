# R2 实施与验证记录

日期：2026-10-31。代码基线 `0c7b6d5`（已合并 R1）。状态：**S0–S7 开发完成、内部验证通过，统一用户验收包齐备；尚未发布。** [开发计划](<../../R2 development plan auto-research-opencode.md>)保留设计和验收矩阵。

本轮没有替换共享插件、重启共享 OpenCode、修改真实研究项目或调用真实模型。Legacy／R1 读取策略均保留，R2 是写入与运行契约，没有 context-policy r2 或新增主导航。

## 已交付

| 能力 | 实现与关键边界 |
|---|---|
| 增量存储与兼容 | schema 11 七张增量表；只读支持 9／10／11；9→10→11 逐级一致性备份、原子迁移、失败后重新备份；旧 requests／todo／结项／来源不重写，旧 todo 仅成为 unknown 候选 |
| 窄工具与修订 | 模型看到 record／revise／checkpoint／dispose／narrow_scope；旧 action 保留适配。editorial／correct_previous／custom 在首次事务展开；preserve_sources + no_new_evidence 需明确且与证据／支撑／checks 一致，空与 unlinked 来源不升级。响应返回持久化 checks |
| checkpoint CAS | 完整已交付 context、query、写入结果形成 base；绑定真实身份、工作段、owner、generation、插件实例与原始 head。实际 assistant message 及工具 call 冻结基线，同轮并行不升级；冲突不补取最新 head。旧显式 expected_revision 兼容，旧成功请求优先原样重放 |
| 审阅生命周期 | 普通事件生成候选；活跃节点采用与主协调显式 decision_refs 形成版本／变更／owner 精确义务。完整独立非空报告只关闭对应任务；coverage 尚未评估，挑战继续作为风险。支持 scope 出入、重开、显式例外；旧报告不能自动撤销 reopen |
| typed relation／采用 | 知识关系追加 revision；节点前驱明确采用材料、拒绝循环及运行中改排；审阅链接按 obligation_id。支撑传播排除 motivation；引用派生采用，同用途 consume 去重且保留不同 attempt／用途及历史来源 |
| M2 调用观测 | 业务事务外 JSONL 覆盖本地拒绝、后端失败、传输尝试、重放、展开、原生模型／variant／usage 与外部执行元数据；不记录参数／提示词／命令正文。实际用量按来源合并，聚合事件不重复计量，缺失保持 unknown，I/O 缺口单独报告 |
| M3 进展与预算 | 领域新事实决定 idle；note／checkpoint／重放／纯文案／相同处置／重复发布不清 idle。所有自主请求在 kickoff 前持久预留，轮次按 owner 累计、节点按整个项目计数、deadline 包含暂停与重启；显式提额不清账。到时请求中断并保留未核实退出 |
| UI／打包 | 概览／研究／成果三入口不变；依据检查中折叠候选、区分完成任务与未解决风险；现有运行详情显示预算与未知覆盖。所有新增 Python 模块进入独立 tarball 白名单，启动协商拒绝旧包契约 |

主要代码：Python 的 `schema11.py`、`checkpoint_receipts.py`、`revision_presets.py`、`review_lifecycle.py`、`adoption.py`、`runtime_budget.py`；插件的 `checkpoint-delivery.ts`、`observations.ts`、`native-observations.ts` 及既有工具／引擎／看板接入。

## 内部验证

- Python 全量 **545 passed**，见 [Python 结果](python-tests.txt)。包含 checkpoint 并发／归属／缓存／幂等、preset／来源、迁移回滚／只读、精确审阅／scope、typed relation／采用与预算／idle 用例。
- Bun 全量 **79 passed**，TypeScript `tsc --noEmit` 通过，见 [插件结果](plugin-tests.txt)。包含 Legacy／R1 的完整 goal→节点→综合→结项宿主流程、实际 SDK 事件形状、自动基线、重启预算与 wall-clock 主动中断。
- 独立 tarball 测试在 Bun 全量中通过：解压到临时目录、脱离仓库 src，验证 schema 11／R2 capabilities、结项及进程重启；打包 prepack 同步 Python 白名单。随后扩展并单独通过[包内迁移测试](package-migration-tests.txt)：schema 10 只读不升级、首次可写迁移至 11、创建备份、旧 checkpoint 原响应重放。
- `git diff --check` 通过。源码与生成的插件 Python 副本已同步。

[旧项目兼容报告](local-project-compatibility.json)验证 **8 个真实旧项目的稳定 DB／WAL 副本**：schema 10→11，全部历史表逻辑行不变、原 DB／WAL 摘要不变，迁移没有创建义务或把旧 completed 洗成新完成。原冻结对象仅供读取，未修改真实项目。schema 9 迁移另有自动化测试。

公开证据中的项目标识使用 project-01…08，测试日志中的本机 Python 安装路径已替换为占位符；真实项目完整路径仅保留在本地输入清单。

## 协议与计量证据

[修改前](r1-protocol-baseline.json)使用基线 commit 的真实后端，[修改后](r2-protocol.json)使用当前后端，由同一 `scripts/r2_protocol_probe.py` 执行两次常见修订和一次 checkpoint，并探测陈旧写。

| 同口径指标 | R1 基线 | R2 |
|---|---|---|
| 三次模型工具意图字段总数 | 21 | 13 |
| checkpoint 模型字段 | action、state、expected_revision | state |
| 宿主 API 调用数（含准备与冲突探针） | 8 | 9 |
| 省略基线的陈旧写 | 被接受 | checkpoint_conflict |

字段减量来自窄入口、preset 和宿主基线管理；新增调用是已交付快照登记，**不据此宣称总调用或总成本下降**。文件中的单次耗时仅供诊断，不是延迟基准；两组均未调用模型，也未评分研究质量。

[原生事件日志](native-invocations.jsonl)来自实际插件观测器与假宿主集成测试；[汇总](native-measurement.json)由 `scripts/r2_measure.py` 直接生成。该夹具的 compaction 使用量合并为一个来源、10 tokens／0.25 cost，重复事件和 99 cost 的 aggregate 不重复计入，外部工具只记录元数据。这里的用量是测试输入，不是真实模型账单。解析器另测真实传输重试与业务重放的分离、缺 end／截断日志／缺失 token 与 cost 的 unknown 处理。

## 终端帧与统一验收

- 依据检查：[80 列](ui/review-80.txt)、[132 列](ui/review-132.txt)。同屏显示任务已完成、风险尚未解决、覆盖尚未评估、精确版本／change 及报告链接。
- 运行详情：[80 列](ui/runtime-80.txt)、[132 列](ui/runtime-132.txt)。轮次／节点／截止时间、token 已观测值与 cost 未知可见。
- [统一本地验收步骤](acceptance.md)：旧项目只读 → 可写副本迁移 → 隔离宿主新运行 → 双会话 CAS／审阅／预算闭环 → 计量与回退。

## 已知边界

- OpenCode SDK 2.0.18 的实际 step／usage／execution 字段已接入并用宿主模拟测试验证。自动基线使用每 session 的串行 provider／tool 步序和 assistant message ID；没有匹配交付事件时要求显式读取，不使用服务端最新值兜底。共享真实宿主的新运行尚待上述统一用户验收。
- 预算硬门覆盖插件自主调度，手动请求与外部 shell／CPU／GPU 不在全局硬门范围。token／cost 仅为已观测子集的停止阈值，未知不能视为零，也不能保证并发请求的全局费用上限。
- R2 的审阅完成是任务事实，科学 coverage／独立性与最终结项准入属于 R3。旧结项标签保留，不借新界面宣称已修复全部科学审阅问题。
- M4 [配对任务](m4-tasks.json)与[独立评分准则](grading.md)已准备，真实模型实验未运行；缺少研究质量与等预算收益证据，R1 仍保持 opt-in，不扩大默认部署。
