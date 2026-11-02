# 探索触发、动态上下文与决策负担：PR 交付记录

日期：2026-11-03。PR 整合基线：main `13079c8a7fb902e50a9fe0a14e7032ab6bc2d7b6`。本轮从旧混合工作区中仅提取探索改进及评审修复，保留主线 SDK 2.2.5、后台事件恢复、版本号、CI 和其他既有实现。

对应[详细计划](<../../Exploration activation and decision overhead development plan.md>)。schema 继续为 16；真实模型探索收益评估按用户安排后置。

## 交付

- A1：持续探索策略；按实际结项契约生成角色提示；探索关闭时同时隐藏 next_work/work_batch；绑定保存真实工作种类。
- A2：按状态及 scope 选择候选，保留未尝试计数和有语义的短策略；控制/角色优先；长标题截断并标记；上下文记录保存选取原因。
- B1：只读 exploration_inbox 按工作及 attempt 分组，支持成员分页和游标变化检查。显式 result IDs 的决定契约不变。
- B2：同一 auto attempt、最后完整发布、原文相同 summary、无额外信息时归并机械完成确认；保留运行事件与审计事件。新增信息仍形成待办。
- B3：限定条件下的纯关闭/暂缓操作记录自身动作凭据，避免对相同动作再生成确认义务。scope revision 和原风险检查继续生效。

## 评审修复与回归位置

| 问题 | 修复与回归 |
| --- | --- |
| 打包遗漏新模块 | sync 清单加入 exploration_read；package 测试实际同步全部运行模块并用 python -S 导入；E3 宿主探针实际调用 inbox |
| 可选详情挤掉角色/控制 | 优先装配控制和角色；限制样本标题；activation 测试覆盖 5,000/6,000/12,000 字符 |
| intent 身份被冒号截断 | 使用完整载荷身份或从末端解析 outcome；多个取消工作保持分组；未知身份逐结果单列 |
| 详情省略后策略失去含义 | 可执行 brief 与 untried_total 独立保留；900 字符回归 |
| 历史 attempt 被当前任务重试覆盖状态 | execution_state/attempt_state 对应结果所属尝试；task_state 单列当前任务；覆盖重试开始和结束 |

实现与回归见 src/auto_research/exploration_read.py、tests/test_exploration_activation.py、test_exploration_inbox.py、test_exploration_finish_merge.py、test_exploration_scope_state.py 及 plugin/test/package.test.ts。

## 当前 PR 验证

在当前 macOS Agent 环境、Python 3.11、Bun 1.3.10、锁定 OpenCode SDK 2.2.5 上运行：

- `PYTHONDONTWRITEBYTECODE=1 make test`：退出码 0；Python **840 passed, 1 skipped**（68.48 秒），Bun **224 pass / 0 fail**（1054 assertions，39.13 秒），TypeScript 检查通过。见 [完整日志](./make-test.log)。
- skipped 为主线已标记的 artifact mtime-tick 问题，修复属于独立 PR #18，本 PR 未改动该测试或保护实现。
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts:src python3 scripts/probe_packed_release.py --e3 --e3-output /tmp/exploration-pr-packed.json`：独立包 full 场景 passed=true，backend_from_extracted_package=true，build_tree_deleted=true，used_external_model=false。见 [运行记录](./packed.log) 和 [inbox 实际调用](./packed-inbox-call.json)。
- `git diff --check` 通过。GitHub CI 在创建 PR 后才会运行，本地通过不冒充 CI 结果。

隔离包中的 Python 从当前源码经 prepack 生成；探针验证新 inbox 查询、同摘要机械完成归并、其他 finish 结果保留及原多路线结项链路。

## 验证边界

工程测试和脚本 provider 不证明模型研究质量提升。没有启动付费模型，没有移动原工作区改动，没有修改共享研究服务。未改 PR #17 的 spike 文件或 PR #18 的 artifacts 实现；两项 PR 独立整合。
