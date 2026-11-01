# PR #13 第二轮处置

日期：2026-11-02（Asia/Shanghai）。审阅基线：`698722a`。[两条新 Review 原文](reviews.md)。本轮在隔离工作树追加修复；不切换共享研究宿主，不合并 PR。

## 处置

| Review 项 | 结果 |
| --- | --- |
| 中文 N1／英文 A：补查误判正常完成 | 接受。入队即登记已收到的消息 ID，事件链有积压时跳过；正常通知和补查修改共用项目队列。transcript 读取移到队列外，并有 1 秒等待上限；读取后重新核对任务与事件状态。只有相隔至少 15 秒的两次观察均发现未收到的终态，才处理遗漏。后端返回 changed，终态已提交时不标未知、不发 fault。覆盖“正常处理先完成”和“扫描更新前状态已变”两个顺序。 |
| 英文 B：失败事件写库报错堵住终态队列 | 接受。记录 coverage gap，在 finally 清理调用等待；后续终态仍能处理，遗留 starting 由终态核实路径建立可用恢复入口。注入写库错误的回归确认只尝试一次、事件队列保持 ready。 |
| 中文 N2：inbox 残留和 warm 重复预约 | 接受。warm transcript 恢复只补记终态，不登记 live inbox、不预约。近期投递去重；缺少 delivered 事件时，模型请求前可凭 transcript 最后输入的精确 ID 恢复绑定，无法核对或绑定被拒则不放行工具。绑定写库失败可重试且不重复预留。入队后超过 30 秒仍未核对的门闩转为显式暂停，不再隐藏阻止续轮；该路径不把已完成的 shell 任务改成 failed。 |
| 中文 N3：后台请求被拒仍要求核实 | 保留保守处理。现有拒绝 trace 只证明特定前台案例没有启动进程；通用 tool.execution 不是“确定未发送”的专用证明。首轮中断 trace 中进程已经启动，失败事件却同样带 executed:false，因此也不能据此自动释放后台执行。不根据错误文案或缺少 shell.created 事件推断退出。 |
| 中文 N4／英文性能建议 | 接受。完整 context 读取逐步退避到最多每 60 秒一次，实际遗漏候选仍需二次确认；慢读取不持有项目串行队列。宿主未提供分页 context，未声称只读取最近 N 条。 |
| 中文 a：预算拒绝重复提示 | 接受。明确拒绝标记在投递时不重试。用户显式恢复后，若通知并入已有受管轮，用该轮的已绑定授权，不重复预约；未绑定的新轮仍不能获得工具。 |
| 中文 b：失败事件订阅过宽 | 接受。未登记在 shellCalls 的失败直接进入 telemetry，不占研究有序队列。300 条无关失败事件的回归未触发队列缺口。 |
| 中文 c：finished 的多余 UPDATE | 接受。前台终态直接删除临时意图，保留原有事件与幂等回执。 |
| 中文 d：resume 误标在途执行 | 接受。仍 open 但未绑定／ambiguous 的执行返回 execution_binding_pending，保留任务 running 和 shell starting，不伪造退出未知，也不提示一个当前不可用的 recover task 命令。 |
| 英文 beforeTool 错误分类 | 接受。保留底层错误 code；无 code 时使用存储不可用类别，不把活动任务缺失或写库错误都说成项目暂停。 |
| 英文每 job 暂停、暂停项目重复暂停 | 接受。每会话聚合实际变化后最多提示一次；项目已暂停时不追加 fault 提示。 |

补查的宽限期用于识别事件缺口，不是实验运行超时。没有配置 seconds 时仍不增加隐藏墙钟上限。迟到超过宽限期且无法对应受管轮的唤醒继续要求人工核实。

## 复现和验证

- 在审阅版本的隔离副本恢复本轮修改的四个产品文件，运行新增可控时序用例：[正常事件竞态与队列阻塞](baseline-reproduction.txt) 3 项预期失败；[失败事件写库](baseline-storage-failure.txt) 1 项预期失败。新 fixture 由本轮提供，其余产品内容保持审阅版本。
- 修复版：[Python 全量](pytest.txt) **799 passed**；[插件全量](bun.txt) **219 pass / 0 fail**；[TypeScript](typecheck.txt) 通过。
- 新增 `plugin/test/background-reconcile.test.ts` 的 17 项定向回归，覆盖竞态双方顺序、真正遗漏的二次观察、暂停项目、慢读取不阻塞命令、退避、inbox 残留、重复入队、拒绝预约、用户恢复、绑定失败重试及 warm 恢复。
- Python 回归检查终态记录的条件更新、未绑定在途执行的 resume 行为，并对实际 pending 查询（IN、两个 json_extract、ORDER BY）确认使用原生 shell 索引；SQLite 可以选择 pending 或 identity 索引。
- [独立打包宿主](packed-host.txt)：OpenCode 2.2.5、本地脚本化 provider；删除构建树后，从解包文件执行后台任务至节点 finished。未使用外部模型。
- [源码摘要与环境](validation.json)。前台中断／拒绝的原生协议证据沿用[首轮 trace](../README.md)，本轮不声称重新运行四个原生协议探针。

复现修复版：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -B -m pytest -q -p no:cacheprovider
(cd plugin && bun test)
(cd plugin && npm run typecheck)
python3 -B scripts/probe_packed_release.py --background-node
```

证据保留测试输出，规范化临时根路径及尾部空白。定向竞态测试使用可控 fake host 与存储状态；打包探针验证真实宿主协议，均不证明真实模型的研究质量。
