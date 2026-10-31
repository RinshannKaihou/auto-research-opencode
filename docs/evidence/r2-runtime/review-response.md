# PR #8 首轮审阅处理

2026-11-01。核对顺序：Opus 本地第一份 → GLM 本地 second → Grok PR 评论。以下处理基于源码和临时项目／假宿主复现；没有修改真实研究项目或重启共享服务。

这是首轮处理记录；源码探测等后续调整以[第二轮收尾记录](review-round2.md)为准。

| 意见 | 处理 |
|---|---|
| Opus 1／GLM 2／Grok B1：challenge 离开范围消失 | 完整／部分 challenge 独立于任务 state、owner 活跃期和 generation 保留，Legacy 与 R1 均显示。只对精确 review_event 追加有证据的显式处置；后来的 supports／inconclusive、waive、scope exit、普通 revision 均不自动清除 |
| Opus 2：契约 Promise 缓存失败 | 只缓存成功；失败后下一调用重新检查，仍明确拒绝不匹配契约 |
| Opus 3：上下文写等待／逐步增长 | context 与回执共用 2 秒期限；相同 owner／head／digest 的快照复用回执，显式 query 按调用／快照去重。迟到回执不升级旧请求 |
| Opus 4：事件排在遥测后面 | 控制事件先处理，step.started 同步绑定，原生用量按接收顺序直接入队但不等待持久化；慢遥测集成用例验证无需先等 usage 落盘也能 checkpoint |
| Opus 5：默认 checkpoint 1,000 字符上限 | Legacy 与 R1 在预算内优先交付完整快照；放不下整块省略，绝不以截断 state 补发 base。测试 2,000 字符 state 与紧预算 |
| Opus 6：编程被误判无进展 | 节点轮次比较真实源码／实验配置摘要，发生变化可清 idle；摘要不在写事务中计算。shell 次数、touch、Markdown／note、checkpoint 不构成推进；缺失／读取失败保持未知 |
| Opus 7：scope 依赖 checkpoint | main 专用 research_review_scope 复用 scope_set，显式 refs／reason，不开放 waive 等宿主管理权限 |
| Grok S1：缺失对象阻断控制／重复 reconcile | 历史范围派生按 ledger 遍历，新输入才严格验证对象；缺失材料在 critical_now 显示。精确 scope 变更触发一次 reconcile，普通 pause_reason／task_state 不扫描全图 |
| GLM／Grok：M2 replayed、额外归属 RPC | 非写响应不继承旧 replay flag；调用归属由业务响应附带，移除工具执行前的 invocation_context RPC |
| GLM／Grok：轮询、缓存、实例 ID、边界 | deadline 定时启停；detach 清理会话交付／辅助计量，剔除已压缩工具的完整输出；共享实例 ID；校验 cursor offset、非 main 控制权限；cost 支持正小数；备份连接显式关闭；兼容说明补齐 |

回归覆盖完整／部分挑战的五种退出、精确处置、权限、对象缺失控制、长 state、陈旧回执、慢遥测、开发活动、只读重放标志和小数预算。全量结果以本目录验证记录为准。

## 保留的设计与 push back

- 保留严格 CAS，不接受 last-writer-wins：追加历史不能保证恢复时采用的 head 正确。
- editorial 是明确的作者声明，不用数字变化或 checks 是否存在猜测语义；不宣称它证明科学内容未改变。
- SDK 2.0.18 是明确依赖，不以缺 hook 静默降级；空报告事件当前不可由合法写入口产生，不为假设中的未来 writer 加兜底。
- 自审可登记任务事实，但独立性和完整科学结项准入仍属 R3。
- 两个来源标志保持现有契约，避免本次修复再引入无关 API 迁移。
- 不重写原 PR 的功能提交；修复独立提交。完整图投影的分页复杂度、长期数据库审计保留策略和 CI 基础设施后续单独评估；当前不删除可能被在途调用使用的回执或冻结基线。
- 测试结果是本地回归，不标为 CI 通过；M4 真实模型实验仍未执行。
