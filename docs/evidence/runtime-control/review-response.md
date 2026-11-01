# PR #9／#10 审阅处理

2026-11-01。优先核对 [Claude 对 #9 的审阅](https://github.com/RinshannKaihou/auto-research-opencode/pull/9#issuecomment-6552441128)及 [Claude 对 #10 的审阅](https://github.com/RinshannKaihou/auto-research-opencode/pull/10#issuecomment-6552444614)，并交叉检查 Grok、GLM。以下区分已修复的问题、保留的安全语义和后续工作。验证只使用临时项目和隔离服务，没有升级共享服务或迁移真实研究项目。

## PR #9

| Claude 意见 | 处理及验证 |
|---|---|
| H1：finish 所在轮失败后永久冲突 | finish 身份包含 attempt 与执行轮。失败／中断原子取消该轮 pending finish，保留历史 receipt；再次请求时也能修复旧版本留下的失败轮 pending。Python、假宿主覆盖失败及中断后完成同一个任务 |
| H2：PID 复用／存活／EPERM 缺恢复入口 | Linux／macOS 尽可能记录进程启动身份；已证明旧进程 incarnation 消失才走 cold。存活或不可探测时登记候选，不自动替换 owner；显式接管对原 owner 的 epoch／binding／fence 做 CAS，保留未知执行并暂停项目。测试 PID 复用、存活、不可探测和过期接管 |
| H3：多轮、暂停节点重绑定误判 | 读取当前 execution 绑定的消息，而非原 kickoff。已经结束且没有未绑定新授权的轮次保持原状态；终态已落盘而 finish 尚未结算时，由 ready 后的后继控制器完成结算。覆盖第二轮、暂停后、handoff 中途和迟到重复事件 |
| H4：eventGap 禁用 stop 且不可恢复 | 控制事件按 session 排队，持久化错误最多三次重试，同一事件沿用执行身份。无关遥测不查归属、不堵塞控制链。失败仅限制相关新工作；pause／stop／只读查询仍可用。resume 先暂停调度、重订阅／回放／核对，再显式恢复；同时核对主协调与节点漏掉的终态。重复终态不会污染下一轮 |
| M5：未调用宿主也记为未知 | 只有宿主 API 之前的失败带 not_sent 证明。事务核对同一 launch、当前任务和 reservation 未绑定／未提交后，才撤回并退还确定未发送的轮次：原节点任务排队、专家未完成、通知可重试。真正的 API 错误仍未知；过期 proof 不能覆盖 stop 或后续执行 |
| M6：stop／restart 恢复步骤太多 | 明确未发送不再要求人工核对；中断 RPC 失败会明确显示。保留真正未知执行的人工核对，取消 wall-clock 路径仅凭任意原生终态就解除未知的逻辑。原生会话结束不证明 GPU／shell 后台进程已退出，不采用批量自动确认退出的建议 |
| M7：专家意外错误不停止子会话 | 当前 owner 对已创建的子会话在意外错误、超时、取消时都请求中断，并等候退出；无法验证则继续保留未知。测试 host 接受 kickoff 后丢响应的清理路径 |
| M8：control_revision 没有用作 fence | 修正文档：它是可观察状态版本，不是独立执行授权。实际许可由事务内状态检查、owner fence、generation、消息与执行轮绑定共同决定。不能在每次 revision 增加时撤销软暂停允许的当前轮收尾；已有竞态测试覆盖实际拒绝点 |
| M9：宿主版本证据不匹配 | 将最低宿主明确提升至 OpenCode 2.2.5，SDK 仍为 2.0.18；历史 2.0.18 测试不再表示当前协议已验证该宿主 |
| L：归属 RPC／workspaceID／结束提示 | 非成员负缓存有容量与一秒期限，registry／WAL 改变即失效；遥测不触发普通会话归属查询。同一 ledger 不因 workspaceID 别名产生两个进程内 owner。segment_complete 不再提示 resume 能重开已结束工作段 |
| L：durable.seq 跨重启 | 扩展真实宿主探针，实测 2.2.5 从重启前 seq 21 递增至重启后 24，三次本地模型调用均结束成功 |

## 保留与后续

- 同一 PID／不同 epoch 可能是同一进程的不同 realm；不能把它直接当成旧执行已死。未知本机身份及其他平台走显式接管，不自动抢占。
- 真实 2.2.5 探针中，同 ID `resume:true` 重发返回 200，额外模型调用为零，但额外产生 execution.started／succeeded。它不是无副作用的重试。因此不自动重发结果未知的 kickoff／wake；确定 API 尚未调用的重试不依赖此假设。
- 进程内旧 attachment 的状态有意跨重绑定保存；已结束执行的事件去重列表限制为每会话 32 条。完整会话缓存的长期回收策略另做，不在本次修复中删除可能被在途调用使用的状态。
- 内部分页达到安全上限会抛错，并非静默截断。未修改成无限循环，也未放宽未知控制状态。
- 保留两个依赖 PR，分别审阅运行控制与主协调权限。#9 不能单独作为完整事故修复发布；两者通过审阅后一起升级。不因审阅建议擅自合并 PR 或重写已有提交。

## PR #10 的范围决定

主协调是持久角色：manual、stopped、complete 也只能使用明确列出的原生 read／glob／grep，规划和研究记录工具仍按各自运行状态授权。主协调不得用 websearch、webfetch、todo 或未分类 MCP 工具代替节点开展工作。要在普通会话中使用这些能力，应 detach 或新建未关联会话；不会为了方便而开放暂停时执行。

#10 补充上述说明、各状态与旧请求的回归、按角色区分的拒绝信息。#9 提供控制器核对期间的只读研究工具通路；research_read_input 仍由角色和服务端 lineage 校验约束，不因此开放其他研究写工具。

验证数量及本轮实际运行的宿主探针见同目录 review-verification.json；旧版探针记录保留作历史，不能代替新提交的验证。
