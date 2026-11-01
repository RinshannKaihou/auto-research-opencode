# E3 后续契约核验与修复

日期：2026-11-01（Asia/Shanghai）。本轮在已有 E3 未提交实现上继续开发；基线分支及 HEAD 为 `codex/e1-e2-exploration` / `84262f6`。首轮工程与宿主记录保留在[上一级目录](../README.md)，本目录记录此后修改与新的实际运行。开发计划和兼容决定见 [E3 详细计划](<../../../E3 detailed development plan auto-research-opencode.md>)。

## 本轮修复

1. **工作输入依据**：新意图的 `scope-work-v2` 指纹包含直接输入、前驱输入、anchor 和 question 及其支撑；在物化前或启动认领前发生相关撤回时，工作结清为 stale，保留结果和通知并释放明确未使用的预留。未关联输入变化、无关知识变化和新增未选候选不应取消工作。
2. **旧意图兼容**：旧意图的输入若不在原授权快照根中，无法还原授权时的输入状态，未启动时要求重新选择；已有输入包含在旧快照根中的意图继续使用旧指纹。已授权或退出未知的执行继续走原控制与核实路径，历史决策和成功请求不重写。
3. **异步缺失结果**：上一条选择中尚缺的预期结果自动进入下一条选择。取消、失败或未核实仍是缺失；迟到 publication 到达后重新评估。明确 defer／propose_stop 会记录处置决策与理由，后续其他工作可以继续，缺失仍进入结项包及附录。
4. **比较条件**：显式未知、空值与空容器不能因双方相同而判为可比；缺失不确定性也不可直接排名。仅识别常见明确标记，不声称理解所有自然语言条件。
5. **共享输入及多范围审阅**：执行指纹排除审阅义务进入／退出范围的管理事件，仍跟踪实质审阅、挑战、处置、知识版本和当前风险。项目审阅协调汇总所有本代 scope 的投入／采用路线，并与既有项目、结项审阅范围合并。

## 工程回归

| 检查 | 实际结果 | 原始输出 |
|---|---|---|
| Python 全量 | 748 passed | [pytest-final.txt](pytest-final.txt) |
| Bun 全量 | 181 pass，0 fail，903 断言 | [bun-test-final.txt](bun-test-final.txt) |
| TypeScript | 通过 | [typecheck-final.txt](typecheck-final.txt) |
| 相关证据／探索／运行控制测试 | 123 passed | [targeted.txt](targeted.txt) |
| 插件 Python 与源码 | 逐文件一致 | [source-manifest.json](source-manifest.json) |

新增回归覆盖直接、前驱与 anchor 输入在物化前／认领前的依据撤回；question 更新；旧有意图在两个阶段的兼容；共享输入的快路线完成后慢路线仍能启动；新的实质审阅挑战仍阻止启动；两 scope 的项目审阅义务同时保留；未到、已取消、已暂缓及迟到结果的衔接；明确未知条件不可排名。

首次隔离宿主运行暴露了共享输入兄弟节点被审阅范围事件误判为 stale 的缺陷；随后的剧本因缺少慢路线结果而出现索引错误。[失败汇总](host-full-initial-failure.json)和[当时的工具记录](host-full-initial-failure-trace.json)保留。修复后重新验证，而非放宽证据撤回或挑战的检查。

## 隔离 OpenCode 与独立包

使用 OpenCode v2.2.5、临时项目、独立 XDG、独立进程及本地剧本 provider；未调用真实模型。完整、双 scope 和独立包三次运行分别加载 76 个相同运行时文件，摘要均为 `63bed14108c3396c3051631c40fcd041168c6b04f0c42c7f07c75e1d0e8e38b2`。

- [完整异步场景](host-full.json)及[工具记录](host-full-trace.json)：共享一次探针，快慢工作使用同一固定输入；临时选择后 provider 不重复传入 expected_results，慢路线迟到后后端仍保留并处理该预期；随后组合退化、固定输入恢复并结项。记录还核验组合意图持久化了 `scope-work-v2` 及其输入引用。
- [两个活跃 scope](host-multi.json)及[工具记录](host-multi-trace.json)：分别选路、共享总预算并全范围结项。
- [独立包汇总](packed.json)、[完整场景](host-packed-full.json)及[工具记录](host-packed-full-trace.json)：确认后端从解包目录加载，构建树已删除，再执行相同异步场景。
- E1/E2 宿主回归：[选路依据失效](e1e2-stale.json)、[关闭探索](e1e2-off.json)、[冷重启](e1e2-restart.json)及[材料故障恢复](e1e2-material-failure.json)均通过；[材料工具记录](e1e2-material-trace.json)保留实际合成调用结果。

## 复现

```bash
python3 -m pytest tests/ -q
cd plugin
bun run sync:python
bun test
bun run typecheck
```

以下命令从仓库根目录执行，要求本地 OpenCode 2.2.5、项目固定 SDK 及可用的 localhost 监听：

```bash
python3 scripts/probe_e3_run.py --scenario full --output /private/tmp/e3-round2-full.json
python3 scripts/probe_e3_run.py --scenario multi --output /private/tmp/e3-round2-multi.json
python3 scripts/probe_packed_release.py --e3 --e3-output /private/tmp/e3-round2-packed.json
python3 scripts/probe_e1e2_run.py --scenario stale
python3 scripts/probe_e1e2_run.py --scenario off
python3 scripts/probe_e1e2_run.py --scenario restart
python3 scripts/probe_e1e2_run.py --scenario material-failure --trace-output /private/tmp/e3-round2-material-trace.json
```

命令状态与来源摘要另见 [checks.json](checks.json) 和 [source-manifest.json](source-manifest.json)。本轮未重跑旧项目副本；首轮五个副本的迁移证据仍见[原记录](../local-project-compatibility.json)，本轮全量测试包含 schema16 只读、备份和迁移回滚回归。

真实模型配对效果仍未验证；模拟 provider 和脚本化测量只验证工程契约。共同预算是计划 `work_units`，实际 token／费用仍是可能不完整的观测；命名资源只协调声明的受管任务。未提交、推送、合并、发布或切换共享服务。

验证记录中的个人目录已在公开副本脱敏；原始本地输出保留在工作区。文中提交与发布状态以验证时点为准，PR 当前状态以 GitHub 为准。
