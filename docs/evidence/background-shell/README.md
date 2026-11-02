# PR #13 Review 处置与验证

本页保留首轮处置（`698722a`）的历史记录；最新真实后端投递绑定修复见 [第三轮处置](round3/README.md)，前一轮补查修复见 [第二轮处置](round2/README.md)。

日期：2026-11-02（Asia/Shanghai）。审阅基线：`d21ccd2`。两条 Review 原文及链接见 [reviews.md](reviews.md)。

本轮在隔离工作树修复并验证。共享研究宿主未切换，PR 未合并。宿主探针使用临时目录、本地脚本化 provider，不调用外部模型；它们验证执行协议，未评估真实模型的研究质量。

## 逐项处置

| Review 项 | 处置与依据 |
| --- | --- |
| 中文 B1：暂停收尾轮不能启动 shell | 接受。before hook 传当前 execution token；失败返回带控制 metadata 的 ToolError。回归覆盖暂停后启动 shell，以及在途调用期间 resume。 |
| 中文 B2／英文 1：中断后无法恢复或核实 | 接受。非正常终态、显式 resume 都同步核实意图和任务，返回具体 task ID 与恢复命令；核实退出入口立即可用。遗漏 after hook 的用例不依赖重载脱困。 |
| 英文 2：普通前台错误被当作后台不确定性 | 接受，并用真实宿主核实。OpenCode 2.2.5 的前台中断及经权限配置使 shell 不可用的场景不触发 execute.after，而发出 session.tool.failed；该事件现已纳入有序订阅并结清前台临时记录。中断探针确认测试进程已退出。主动请求后台而缺少终态见证时继续保留核实要求。 |
| 中文 S1：inline 判定过早 | 接受。以入队时的 execution token 与投递时的实际执行比较；跨轮投递补做预算预留和绑定。排队的原生完成通知会阻止发送重复的人工续轮。测试直接检查节点预留账本。 |
| 中文 S2／英文 3：未匹配、迟到、格式变化的通知 | 接受。已核实退出、终态冲突和未匹配通知返回 ignored，不改任务状态。插件跳过缺少有效 inbox ID 的事件；不认识的字符串状态规范为 failed 并保存 raw_outcome，非法 exit 不作为成功证明。恢复按 job 隔离错误。续轮授权失败暂停并报告，保留已有终态证据。 |
| 英文 4：通知早于 execute.after | 接受。在 execute.before 登记跨附件共享的待完成调用，通知有界等待该 session 的结果登记。回归将通知放在 after hook 调用之前，另保留结果 RPC 延迟的回归。 |
| 中文 S3／英文 5：无限等待与绕过无进展检查 | 接受补查与计数修复。每 15 秒扫描等待任务的 transcript；遗漏终态会补登，无法核对原生唤醒授权时转入可恢复的核实状态。启动 shell 的回合仍累计 idle，单纯等待才冻结；反复启动任务不能绕过限制。**保留显式预算契约**：没有配置 seconds 时不新增隐藏超时，长实验可继续；需要时间上界时使用持久 max_wall_seconds。等待时长显示在看板。 |
| 中文 S4：热重载使后台任务失败 | 接受同进程情形。warm 生命周期保留原生运行状态及在途调用；冷启动仍保守核实。无终态并不能单独证明进程仍在运行，因此不据此跨宿主进程自动信任执行。实际 warm 行为由同进程 fake-host 回归覆盖。 |
| 中文 S5：resume 范围、反馈及 revision | 接受。可恢复会话没有局部 pause_reason 时也清零持久 idle；保留已处理 turn 去重和预算。revision 竞争重读并重试一次；仍受阻时报告原因，任务不静默跳过。已知仍在执行的 starting 调用不被误标为未知。 |
| 中文 S6／英文存储建议 | 接受。前台终态删除临时 native_shell 行，幂等回执保留在既有请求账本；去重增加 session 条件。增加 pending 及 shell identity 辅助索引，查询计划回归确认索引使用。既有可写初始化会重复安装缺失索引；只读路径不写库，无字段或数据格式变更，schema 保持 16。 |
| 中文 Nit 1／英文 context fallback | 接受。改用 recoveryMessages，读取失败只保留只读工具并使用中性提示；仅对已确认的后台唤醒移除全部工具。处理最近 64 条输入；宿主 context 接口本身仍返回完整 transcript。 |
| 中文 Nit 2／英文 pause delivery | 接受。保存暂停后，辅助消息与通知发送 best effort，失败不把任务改成执行未核实；无 turn 时使用唯一暂停键。增加发送失败回归。 |
| 中文 Nit 3：两份恢复原因列表 | 接受轻量方案。增加 Python／TypeScript 集合一致性回归。 |
| 中文 Nit 4：工具名范围 | 明确协议边界。后台跟踪只支持原生 shell；bash/python/terminal 的通用执行观测仍保留。未支持工具没有可核对的原生 shellID 协议，不将其假定为后台 shell。README 已说明。 |
| 中文 Nit 5：暂停原生唤醒的预算 | 已明确限制。宿主可能在暂停期间调用一次模型；插件不给工具权限且不发受管续轮，该模型调用没有自主 turn 预留，仍可能产生模型用量。 |
| 中文 Nit 6：message ID 校验 | 接受。插件检查 msg_ 前缀与长度；缺失 ID 不进入持久终态处理。 |
| 中文 Nit 7：执行观测早于后台完成 | 接受。后台 shell 的 evidence-v1 执行观测移至匹配的终态通知；启动观测另带 background 标志。观测失败记录诊断，不改变任务终态。 |
| 中文 Nit 8：通知错挂旧任务及错误文案 | 接受。核实通知显式绑定 shell 记录的 task ID，类型为 paused；通用通知选择 session 最新任务。 |
| 中文 Nit 9／英文无 task 的记录 | 接受。begin 必须存在活动节点任务；无 task 时拒绝启动，不生成无法使用任务恢复入口的记录。 |
| 中文 Nit 10／英文 CHANGELOG 与证据 | 接受。更新 CHANGELOG、使用说明，附本轮完整测试输出、四个宿主生命周期 trace 与独立打包汇总。 |
| 英文 sessionState 错误处理 | 接受。后台状态读取失败不再令整个 session RPC 失败。 |

## 验证

- [Python 全量输出](pytest.txt)：797 passed。
- [插件全量输出](bun.txt)：200 pass，0 fail。
- [TypeScript 输出](typecheck.txt)：通过。
- [独立打包宿主输出](packed-host.txt)：后端来自解包文件，构建树已删除；原生后台 shell 完成，节点任务 finished。
- [源码摘要及环境](validation.json)：供核对本轮受测实现与文件内容。

| 真实 OpenCode 2.2.5 场景 | 证据与观测 |
| --- | --- |
| 原生后台任务 | [trace](host-background.json)：execute.after 返回 running/shellID，完成消息入队并投递。 |
| 前台 exit 7 | [trace](host-foreground-error.json)：execute.after 为 completed，metadata.exit=7，没有后台唤醒。 |
| 前台中断 | [trace](host-foreground-interrupt.json)：无 execute.after，session.tool.failed 为 aborted，测试 PID 已退出。 |
| 经权限配置使 shell 工具不可用 | [trace](host-foreground-denied.json)：无 execute.after，session.tool.failed 为 tool.execution，未启动测试进程。 |

复现命令：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -B -m pytest -q -p no:cacheprovider
(cd plugin && bun test)
(cd plugin && npm run typecheck)
python3 -B scripts/probe_packed_release.py --background-node
python3 -B scripts/probe_background_wait.py --scenario background
python3 -B scripts/probe_background_wait.py --scenario foreground-error
python3 -B scripts/probe_background_wait.py --scenario foreground-interrupt
python3 -B scripts/probe_background_wait.py --scenario foreground-denied
```

宿主 trace 的临时根路径用 `<tmp>` 替换，保留事件顺序、字段与测试命令。前台中断探针证明测试进程退出，不证明任意脱离宿主管理的子进程退出。辅助索引不消除既有请求幂等回执的增长；本轮没有清除历史回执或外部执行记录。
