# PR #13 第三轮处置

日期：2026-11-02（Asia/Shanghai）。审阅基线：`a6e4897`。[两条 Review 原文](reviews.md)。在隔离工作树追加修复，共享研究宿主未切换，PR 未合并。

## 处置

| Review 项 | 结果 |
| --- | --- |
| 英文 C：transcript 恢复不能绑定真实后端 | 接受。上一轮 mock 恒设绑定成功，未覆盖真实后端的序号校验，确实漏测。新增显式、host-only 的 transcript_witness 绑定路径；保留原生 start 的真实序号，不把它伪装成 delivery 序号。它要求当前 attached node、running 项目和任务、同一代精确 shell 完成消息、未结束且不 ambiguous 的 start、start 序号晚于上一终态，以及该消息已签发的轮次预算授权。旧的无序号普通 bind 仍拒绝。 |
| 英文 C：context 早于 delivered 时误暂停健康节点 | 接受。真实 SQLite 后端集成回归先推送 enqueued、started，再运行 context，最后才推送 delivered；确认工具仍可用、没有 fault、任务 running、只计一次预约，并检查账本 binding_source=transcript。绑定证据不足时保留 inbox 和工具限制，等待原生事件，不因一次 fallback 被拒而暂停或撤销健康执行。 |
| 中文 M1：同轮未投递通知反复读取完整 context | 接受。先判断是否确实需要绑定新轮；同一执行内的工具检查只核对原有授权，不读取 transcript。测试连续检查 5 次，额外 context 读取为 0。 |
| 中文 M2：resume 返回 pending 却已提交前面修改 | 接受。先分类、后统一修改；遇到仍在绑定中的调用时不修改任何其他 shell／任务／通知。原本已存在的执行未知优先报告 execution_unverified 和恢复命令。Python 回归覆盖两种 shell 排序及已有未知状态。幂等请求回执仍按既有机制保存。 |
| 中文 M3：长轮次内入队的通知立即过期 | 接受。入队年龄与可投递的空闲等待分开记录；新轮开始时清除 idle 计时，轮次结束后开始 30 秒宽限。warm 遗留状态首次观察到空闲时获得完整宽限。长轮次及实际 started/ended 方法的定向用例都覆盖这一变化。 |
| 中文 M4：补查与全局事件缺口修复的分工 | 接受。README 明确：全局缺口／失败事件积压时周期补查避让，由既有事件修复流程处理，可通过 /research resume 触发核对。 |
| 首轮 foreground-denied 标签 | 更正说明。该探针通过权限配置令 shell 工具不可用，实际错误为 “No tool named shell is currently available”；它不是通用权限拒绝错误码的证明。原 trace 与场景参数名保留，首轮表格已改成精确标签。 |

transcript 路径不放宽默认 bind 的约束，也不推测缺失的 native start。模型不能请求该 host-only 路径；新请求不允许同时填写伪造的 delivery event_seq。任务已未知、会话／项目暂停、序号缺失或陈旧、授权不匹配时均不授予新轮工具权限。

## 复现和验证

- [修复前真实后端回归](baseline-transcript-binding.txt)：产品源码与审阅版本摘要一致时，新增集成用例失败，context 丢失 shell 工具。该用例使用 fake SessionHost 配合真实 Python／SQLite 后端，没有把 bind 结果设为成功。
- [Python 全量](pytest.txt)：**813 passed**。
- [插件全量](bun.txt)：**223 pass / 0 fail**。
- [TypeScript](typecheck.txt)：通过。
- [普通投递独立打包对照](packed-normal-delivery.txt)：从解包插件运行至节点 finished，构建树已删除。
- [丢弃投递回调的独立宿主验证](packed-dropped-delivery.txt)：仅在临时解包的 index.ts 中丢弃已知 shell 的 delivered 回调；保留 enqueued、真实 started 事件和原生 transcript。真实 OpenCode 2.2.5 运行至节点 finished，账本出现 transcript 绑定。汇总显式标注 temporary_fault_injection=true。
- [源码摘要和环境](validation.json)。两个宿主场景均使用本地脚本化 provider，未使用外部模型，不评估研究质量。

Python 的新增边界用例覆盖缺失／陈旧 start 序号、ambiguous、无预算授权、不同消息、项目／会话暂停、未知任务、已结束轮次、模型调用及同时填写 delivery 序号。默认无序号 bind 仍失败，成功的 transcript bind 保留真实 start 序号；迟到的原生 delivered 不重复计预算。

复现：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -B -m pytest -q -p no:cacheprovider
(cd plugin && bun test)
(cd plugin && npm run typecheck)
python3 -B scripts/probe_packed_release.py --background-node
python3 -B scripts/probe_packed_release.py --background-node --drop-shell-delivery
```

--drop-shell-delivery 仅适用于该隔离测试脚本；它不修改源插件或共享宿主。测试输出规范化了临时根路径及尾部空白。前两轮记录保留为历史，并明确标出需要本轮真实后端修正的结论。
