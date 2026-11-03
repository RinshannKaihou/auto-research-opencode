# PR #20 review 处理记录

日期：2026-11-03。评审：[review comment](https://github.com/RinshannKaihou/auto-research-opencode/pull/20#issuecomment-6594221011)。同步方式：merge 最新 main `98fe573`，保留现有提交，不 rebase、不强推。该 main 已包含 #17、#18 和 #21。

## 逐项处理

| 意见 | 处理及验证 |
| --- | --- |
| 1. finish 工具说明与 node_core / 探针 kickoff 不一致 | **修复提示冲突**。节点角色、普通/探针启动消息和工具说明均要求先发布新增证据/限制/建议；无补充信息的正常结束原文复制本 attempt 最后完整发布的 summary；不同总结仍是新信息，受阻使用 stopped。Python 回归读取真实 node_core context；插件测试读取实际 kickoff。宿主脚本改为从实际发布中取 summary，只有一个刻意不确认的 legacy 路径保留独立 finish，不再为 slow 节点单独硬编码一个相同字符串。 |
| 1. 建议引入 confirms_publication | **本轮采用评审的 minimum 方案，保留原严格条件**。引用正确的 publication 本身不能证明不同的结束总结没有新信息。通知送达也不等于新信息已进入研究待办/依据。若未来加入显式确认字段，需要同时定义附加总结的义务和版本语义，本轮不扩大该写入契约。真实模型是否遵循提示、归并命中率仍未测；不支持“实际模型永远不会归并”或“提示修复已证明收益”的绝对结论。 |
| 2. investigating 无结果时被 parked 挤占 | **修复**。增加 investigating_route 原因，其展示优先级高于 deferred/excluded；只表达已投入状态，不自动判断科学优劣。回归覆盖 1 条 investigating、4 条 excluded、零 pending result。 |
| 3. 风险索引容量需测量 | **测量并修复**。先填稳定风险索引与 checkpoint/recovery，再填探索 details、完整规则和 meta，最后重新分配正文预算。12,000 字符、3 scope、长前提/探针、6 个失败检查的真实读模型场景：修复前 14 个风险中只索引 9 个，其中失败检查 3/6；修复后索引 14/14，失败检查 6/6。角色/控制仍在，总长 11,787 字符。[观测](./review-context-pressure.json)。 |
| Minor：knowledge_changed / stale 未采样 | **修复**。两种结构化结果均进入 result_sample。表示需要考虑的记录变化，不自动把所有修订判断为科学反证；有回归。 |
| Minor：inbox 按 hash 排序 | **修复**。待处置组优先，然后按最早 source_sequence、group key 稳定排序；保留原分页依据校验。回归覆盖非任务创建顺序的发布、处置一组后翻页、已处理组排后。本轮未新增 pending-only 过滤参数。 |
| Minor：missing_expected_result 依赖前六个固定版本 ref | **修复**。内部使用完整 considered-route identity 集合，按不带 revision 的稳定身份匹配；展示仍限制为六项并声明省略。回归覆盖第七条与第九条路线，以及同一决定推进路线 revision。 |
| Minor：裸码注释与极小预算 fallback 不符 | **修正文案**。说明低于最小 brief 的预算可带 brief_omitted 降级；900+ 的已有回归继续保证可执行短句。 |

## 本轮验证

- Agent 本地 macOS / Python 3.11 / Bun 1.3.10 / SDK 2.2.5。
- 针对性 Python：105 passed，13.41 秒。
- `PYTHONDONTWRITEBYTECODE=1 make test`：退出 0；**847 Python passed**（65.08 秒），**224 Bun pass / 0 fail**（1058 assertions，36.86 秒），TypeScript 通过。main 已包含 #18，因此本轮不再跳过 artifact mtime-tick 回归。[日志](./review-make-test.log)。
- 独立包 E3 full：passed=true、backend_from_extracted_package=true、build_tree_deleted=true、used_external_model=false。[运行记录](./review-packed.log)。
- `bun run sync:python` 后所有内置 Python 模块逐字节匹配源码。
- `git diff --check` 通过。

更早 E3 历史剧本曾记录 13 次 decide/prepare、4 次过期拒绝。**本 PR 之前那次实际独立包记录为 11 次尝试、1 次过期；本轮为 9 次尝试、1 次过期**。这些脚本运行含时序差异，且本轮改变了正常节点完成策略；不能混作真实模型命中率、生产拒绝率或成本收益对照。[本轮计数](./review-scripted-counts.json)。

旧 head `0c1b994` 的 GitHub CI Python 3.11/3.12 均通过；本轮修复提交推送后的 CI 结果另查，不以旧 head 的通过代替新 head。真实模型对照仍按用户安排后置；证据归属、正式比较、采用/排除依据等未列入本次修改。
