# PR #9／#10 第二轮审阅处理

以 [Claude #9 二审](https://github.com/RinshannKaihou/auto-research-opencode/pull/9#issuecomment-6552889466)为主线，交叉核对 [Claude #10 二审](https://github.com/RinshannKaihou/auto-research-opencode/pull/10#issuecomment-6552895577)、Grok 与 GLM。两个 PR 的第一轮修复已获确认；#9 的 N1、N2 是本轮合入前修复项，#10 的持久角色范围保持不变。

| 意见 | 本轮处理与验证 |
|---|---|
| Claude N1：续轮未发送仍误标 unverified | afterTurn 识别 preflight 的 not_sent，仅以 fault 暂停会话，保留原任务。普通续轮／显式恢复的 nudge 也携带原消息身份，按消息撤销 issued 授权并退还 reserved 轮次；bound／submitted 均拒绝退款。第一、第二次 controller_check 分别注入失败，修复前均复现，修复后任务仍 running，热重绑定后可直接显式恢复 |
| Claude N1：旧 issued 授权污染 has_unbound_turn | 查询限定在当前 execution_start 之后创建、相同 host／generation 的授权。早于 ambiguous start 的遗留授权不再把空闲节点误判为仍有下一轮；真正的后续授权仍阻止盲目恢复。不传 token 才取最新轮，传入不存在 token 返回 None，没有任意回退 |
| Claude N2：交接／归属超时泄漏盲评系统提示 | 在 injectContext 入口先按进程内角色／工具授予判断并替换盲评系统、约束工具，再做任何 await／forward／early return。归属未确认且身份未知时也不保留默认系统提示。membership error、1.5 秒 timeout、successor 未 ready 三条分支均先复现泄漏、再验证消失；工具仍受分配清单与服务端 lineage 校验 |
| Claude：事件故障不可见 | /research status 与会话侧栏状态提供 gap／暂存数／待确认归属数，给出 /research resume；变化通过 changed 通知刷新。减少活动的控制仍可用 |
| Claude：finish mismatch 选到旧任务 | 选择相同会话的 ACTIVE_TASK，测试把历史 finished 任务放在当前任务前，确认只标记当前任务 |
| Grok N1：归属未确认时丢事件 | 三次失败后有界暂存未知会话事件，最多 128 个会话、每会话 256 条；溢出保留 gap。恢复时先分类，确认普通会话才丢弃其事件。单次最多核对 8 个未知归属，每次等候 200ms，失败项轮转，不冻结已知研究续轮。覆盖未知终态重放与无关会话故障隔离 |
| Grok N2：未发送派发重复刷屏 | 同任务连续三次 not_sent 后，以 fault 暂停主协调，原任务仍 queued，需排障后显式 resume。reuse 会话未关联属于执行身份未核实，不再作为可重复回退的普通 preflight。回归确认三次后不再自动新增启动 |
| Grok N3：候选长期 pending | 新 owner 生效／显式接管后，其他 pending 候选转 superseded，保留审计历史与旧候选 CAS 拒绝 |
| GLM 遗留 F2：research_wait 控制错误 | 暂停／停止或会话故障时返回结构化 code／details，retryable=false、allow_fallback=false 与 explicit_resume／explicit_auto 语义保持一致 |

新增 5 项针对 Claude 阻塞项的假宿主回归在修复前全部失败、修复后通过。另保留对照：宿主已接受、但真实 API 回执丢失的续轮仍 unverified、仍计费，不把未发送证明扩展到未知交付。所有本轮验证为本地运行，非 CI。

当前 PR #9 全量结果：Python 612 passed；Bun 138 pass／0 fail；TypeScript 与 diff 检查通过。本轮原始宿主探针输出与运行方法见 [round2](round2/README.md)。PR #10 合入同一修复后另记堆叠验证，不以旧摘要代替新运行。

## 保留的范围

- 不因二审通过就合并或部署；保留 #9 → #10 的依赖顺序，通过审阅后一起升级。如果 #9 squash 合入，#10 需在固定新 base 上只重放边界改动并核对 diff，不能直接假定改 base 后差异不变；本轮不重写已有远程历史。
- 自动重发未知报告、stop／restart 后自动解除专家未知状态仍保持原决定；两项已被 Claude 接受为非阻塞设计取舍。
- hostname 参与机器身份及 macOS ps 位于 attach 写事务中暂不调整。它们会增加人工核对或最多两次 1 秒探测的锁等待；不再造成无入口锁死，也不影响本轮 pre-API 失败的安全回退。平台身份来源与进程探测移出事务需单独做 owner 快照重验，避免拿过时的探测结果抢占新 owner。
- 宿主配置热更新完整卸载顺序和所有多进程交错仍属发布前验证项；当前没有重启共享服务或迁移在运行的研究。
