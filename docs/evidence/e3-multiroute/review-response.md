# PR #12 review 处理记录

审阅基线：`752462a`；本轮优先按用户指定的英文 Claude review 核验，同时处理两份中文 review。实现与验证在隔离 PR checkout 中进行，原工作区已有未提交材料保留。

## 修改顺序与验收

1. **资源与兼容契约（Claude 1–4；中文 B1/F3）**：允许当前 attached main 核实本项目已终态任务的外部退出，不依赖已退任 parent；保留模型拒绝、终态检查、未知用量不退款。空资源不持有租约。资源阻塞产生可去重持久提示，列明 blocker 与核实命令。真实 schema15 无 basis_mode 意图显式以 `exploration_basis_format_upgraded` 结清，要求重选；开发期 v1 单独标注。覆盖正常 finish→接管→核实→后续 claim、未核实/模型拒绝、通知重启/去重、真实旧格式迁移。
2. **可见状态与分页（Claude 5–6；中文 S5/F1/F2）**：从后端 expected_missing 计算缺失与未处置数量，准确区分在途与历史已物化工作；manifest 只随第一页返回，决策页默认 16KiB；非对象 cursor 显式校验，覆盖预算分页游标失效和边界。
3. **批量执行与廉价改进（中文 S4/S7；Claude nits）**：已提交决策的逐项物化错误返回明确部分成功，不伪装成整个决策失败；一次批次 reconcile 一次并复用相同依据；资源等待写入只在变化时发生；generation 在资源占用前校验；迁移逐块比较；快照一次构建 task→scope 映射；预算等待提示去重、预算命令只显示摘要。其余建议结合语义与成本逐条给出接受或 push back 依据。
4. **验证与交付**：针对性回归、Python/Bun/TypeScript 全量、内置 Python 同步、独立包与隔离 OpenCode 资源场景。保存新轮次证据，不覆盖历史失败；更新当前 PR 状态与逐项答复，提交到同一 PR。真实模型配对效果仍未验证，不以工程通过推断探索质量。

## 契约选择

- 资源核实是用户对物理进程退出的声明；会话接管不改变项目对历史资源的管理权。正常模型 finish 不证明外部进程退出，不根据“工具调用已结束”自动释放设备。
- pre-E3 全局 basis 与 E3 视图布局不同。选择显式失效并要求重新批准，避免声称能保留旧摘要；历史决定、已开始工作与成功请求重放不重写。
- 多路线投入仍由用户可选预算控制，不凭 review 引入每范围固定并发/分叉配额。限制与默认变化必须在文档中准确说明。

## 核验与逐条处置

优先审阅：[英文 Claude review](https://github.com/RinshannKaihou/auto-research-opencode/pull/12#issuecomment-6556558301)。另外核对了 [中文 request-changes review](https://github.com/RinshannKaihou/auto-research-opencode/pull/12#pullrequestreview-5727550672) 和 [ZCode/GLM review](https://github.com/RinshannKaihou/auto-research-opencode/pull/12#issuecomment-6556687739)。认可项以实际代码和回归为依据，没有按 reviewer 票数裁决。

| 意见 | 处置与依据 |
|---|---|
| Claude 1：接管后租约无法释放 | **修复**。当前 attached main 可核实本项目所有终态任务，包括旧会话／旧代任务；原 parent 不再是条件。模型仍被拒绝，unverified 须先走执行核实。覆盖正常 finish→takeover→release→next claim。工作投入不退款，实际用量仍 unknown。 |
| Claude 2：pre-E3 兼容声明与测试错误 | **接受并更正**。schema15 实际不带 basis_mode，所有未启动旧意图明确以 exploration_basis_format_upgraded 结清，不能补造授权。夹具由 main 31fd90d 实际 NativeService/view builder 生成，校验旧摘要，再测试 15→16 后物化前和认领前处理；输入是否在旧根中均须重批。原 v1 测试更名为 development-only，文档明确其从未在 main 发布。 |
| Claude 3／GLM F3：空资源永久 held | **修复**。空声明不进入 held；读投影过滤早期空租约，旧 held 空声明正常 finish 时置 released。普通完整宿主／独立包的 5 个无资源任务最终均为 released。 |
| Claude 4／中文 B1：正常结束后等待无提示 | **修复**。阻塞 claim 通过既有 workflow_notifications 保存 stable resource_wait 通知，列明持有任务、状态与核实命令；插件负责可靠交付，重复 claim／重启不重复发送相同通知。看板显示资源等待数量，README 明确每项资源工作结束后需用户核实。实际宿主容量 1 的两项工作经通知→用户核实→第二项执行并结项。 |
| Claude 5：缺失计数读错字段 | **修复**。读 expected_missing，并分别列 missing／unacknowledged／free-text；在途工作联接任务状态，不累加已完成 materialized 历史。临时范围总数覆盖全部范围，不依赖前三个样本。 |
| Claude 6／中文 S2：manifest 重复、分页成本 | **修复**。仅 offset=0 返回完整 manifest，后页保留包 digest；决策页默认 16KiB。新 prepare 的显式分页上限由 1MiB 收紧到 256KiB，超过即拒绝。接近上限的实际宿主包为 247,781 字节、16 页，仅第一页附 manifest，同请求完整交付后成功结项。工具说明提醒上下文和 token 成本；不宣称该字节上限适合所有模型，也未引入缺乏可靠 provider 资料的自动上下文预检。 |
| GLM F1/F2：非对象 cursor／budget RPC 缺测试 | **修复**。Evidence、exploration、budget 的非对象 cursor 都给 ValidationError；预算分页覆盖完整性、digest 变化、偏移边界与类型。混合 evidence_observed 列表逐项拒绝非对象，有效页仍取得回执。 |
| 中文 S1：强制预算或按 concurrency 限制每 scope 在途数 | **Push back 该限制方案，补文档**。concurrency 是执行并发，work/nodes 是可选投入限制；不把三个维度合并或引入固定分叉配额。现在明确说明单项在途限制已移除，单批最多100但累计投入默认不限，用户须显式配置 work/nodes。已配置的共同额度仍在 decision 事务内原子执行。 |
| 中文 S3：项目视图与决策包膨胀 | **部分修复**。包保留决策身份、理由、缺失处置、比较和路线处置，完整工作计划按固定 decision 引用展开；风险和科学材料不裁剪。工具要求局部选择始终传 scope_id，并加局部选择不遍历未选候选的回归。**保留**旧单 scope 的未带 scope_id 调用语义：不能静默把已读项目快照换成不同局部快照；项目结项也必须保留全代／历史风险。 |
| 中文 S4／GLM F4：批内重复遍历和任务快照扫描 | **廉价项已修复**。一次 batch 内 reconcile 一次，相同输入／路线集合复用 basis；任务快照一次构建 task→scope 映射；资源等待 error 仅变化时写入。**保留**每次启动 claim 在同事务内重新复核科学依据，不跨调度轮缓存授权，也不让资源等待掩盖依据撤回。全量图存储优化另行处理。 |
| 中文 S5：完成工作仍显示在途 | **修复**。materialized 意图只在关联任务仍 queued／starting／running／waiting／stopping／unverified 时算在途，历史意图保留原状态以支持追溯与重放。 |
| 中文 S6：关闭范围继续跟踪变更、担心通知膨胀 | **确认有意保留，并澄清**。关闭／暂缓／未投入路线的已声明科学引用变化仍形成 pending result，保证结项不能通过缩圈消除风险；context 的 investigating/adopted 过滤用于当前采用依据，口径不同有明确目的。变化投影本身只写 result/event，不发送通知或唤醒；新增关闭／暂缓范围回归证明这一点。 |
| 中文 S7：物化部分失败使已提交 decision 表现为失败 | **修复**。返回 decision_recorded、逐 intent 的 work/error 和重试同一意图的说明；status、schedule、deliver 后续失败也不吞掉已提交决定。保留原重启一次恢复回归，并增加部分成功测试。 |

### 其余 nits

- schema16 备份逐块比较保留表，避免两份 fetchall；新表 amount CHECK 与 API 同为 >0。actual_usage 列仍保留 unknown：未接入可靠 work 级实际计量前不能写入0或把计划额度当实测用量。
- waiting_budget 只在进入该状态时提示；generation 在任何资源占用前检查。
- `/research budget` 改为摘要和分页入口；resource 支持 off 移除单项容量。降低／移除容量不撤销既有租约，后续排队按新容量等待且有通知，不伪造物理容量空闲。
- 比较只识别明确未知标记，文档建议使用规范 `unknown`；无法从任意自然语言保证科学条件已知，不扩张为另一个语义判别器。
- 首轮和 round2 证据明确标注历史代码及来源；保留原失败用于追溯。本轮工具 trace 使用 gzip 压缩保存，包内是完整 UTF-8 JSON，不裁掉错误或页内容。
- 内存中的公平轮换提示在重启后回到持久队列顺序，代码注释说明，不承诺跨重启严格轮转。
- **Push back B1 的自动释放／失败即释放建议**：工具调用结束或模型正常 finish 都不能证明其启动的外部进程退出；claim 成功后的启动异常原路径标 unverified，只有确定 not_sent 才自动释放，已有回归保留。没有在启动结果不明时放宽释放条件。资源等待通知告知用户；不会把需要用户核实的设备状态交给模型自行确认。
- 未重写已发布的 752462a，避免更改 review 的基线身份；本轮以独立修复与证据提交追加到原 PR。

## 本轮实际结果

| 层次 | 结果与证据 |
|---|---|
| Python 工程回归 | **781 passed**，含真实 schema15 格式、正常资源核实／接管、非对象 cursor、预算分页、missing／在途统计、局部范围、近上限分页等回归；[原始输出](review-round1/pytest.txt) |
| 插件工程回归 | **185 pass，0 fail，925 assertions**；[原始输出](review-round1/bun-test.txt) |
| TypeScript | 通过；[原始输出](review-round1/typecheck.txt) |
| 资源实际宿主 | OpenCode 2.2.5，容量1，正常 finish→uncertain→持久通知→用户核实→下一项运行→全范围结项；第二项结束仍为 uncertain，未假装核实；[汇总](review-round1/host-resource.json) |
| 近上限实际宿主 | 247,781 字节、16页，完整当前请求回执后结项；[汇总](review-round1/host-paged.json) |
| 独立发布包 | 删除构建树、内置 Python 从解包加载后完整共享探针／异步修订／组合退化／恢复／结项通过；[打包结果](review-round1/packed.txt)、[宿主汇总](review-round1/host-packed-full.json) |
| Python 同步／来源 | 内置 Python 与 src 逐文件一致；三个宿主场景的 76 个运行时文件摘要均为 `4d656b56d654ed299f6b527b0c823abdedd5ba2d345354e096c407acc54fa39c`；[文件清单](review-round1/source-manifest.json) |
| 真实模型效果 | **未验证**。以上 provider 为脚本化本地服务，未调用付费模型；不能据此推导探索质量或成本改善。 |
| 发布 | PR #12 已创建，本轮推送到同一分支；尚未合并、发版或切换共享宿主。 |

首轮 Bun 失败为两处旧预期（整体工具失败、完整预算列表）未同步到本轮新接口，后续保留恢复／预算账本的原断言并调整返回检查，见 [失败输出](review-round1/bun-test-initial-failure.txt)。首次资源宿主失败为剧本 provider 在 wait 后继续发起结项，遇到迟到 pending 的正确拒绝；修正剧本结束当前轮并等待 native wake，未放宽后端 CAS。首次大包宿主用单个 240KB record RPC 被既有64KiB传输限制正确拒绝，改为8次小请求构成大决策包。两份失败汇总及完整压缩 trace 均保留。

公开输出的个人目录已脱敏、日志行末空白已归一化，压缩 trace 的 JSON 内容完整；成功／失败汇总中的 trace 字段指向对应 `.json.gz`。raw输出中的版本与数量取实际运行，不套用旧轮次通过数。

复现（仓库根目录；只使用隔离项目／独立XDG／localhost provider）：

```bash
python3 -m pytest tests/ -q
(cd plugin && bun run sync:python && bun test && bun run typecheck)
python3 scripts/probe_e3_run.py --scenario resource --output /private/tmp/e3-review-resource.json
python3 scripts/probe_e3_run.py --scenario paged --output /private/tmp/e3-review-paged.json
python3 scripts/probe_packed_release.py --e3 --e3-output /private/tmp/e3-review-packed.json
```
