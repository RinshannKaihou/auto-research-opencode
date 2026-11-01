# E3 本地开发与隔离验证

日期：2026-11-01（Asia/Shanghai）。开发基线 `codex/e1-e2-exploration` / `84262f6`，工作区包含尚未提交的 E3 实现，schema16。本页保留首轮 `source-manifest.json`、`checks.json` 及验证输出；其后的输入依据、异步缺失结果与比较条件修复和最新验证见[后续工程记录](round2/README.md)。开始时已有的文档修改和评审材料保留；未推送、建 PR、合并、发布或切换共享服务。

设计与切片完成清单见 [E3 详细计划](<../../E3 detailed development plan auto-research-opencode.md>)。本记录区分工程正确性、实际宿主与模拟 provider、真实模型效果及发布状态。

## 工程正确性

| 检查 | 本轮结果 | 证据 |
|---|---|---|
| Python 全量 | 726 passed | [实际输出](runs/pytest.txt) |
| Bun 全量 | 181 pass，0 fail，903 断言 | [实际输出](runs/bun-test.txt) |
| TypeScript | 通过 | [实际输出](runs/typecheck.txt) |
| 源码／内置 Python | 完整同步并逐文件摘要核对 | `source-manifest.json` / `checks.json` |
| 增量迁移与只读 | 15→16 备份、失败回滚重试、历史值保留；只读不迁移 | `tests/test_e3_budget.py` 及全量日志 |
| 旧项目副本 | 5 个可访问项目 10→16；历史列值及原 DB/WAL 摘要不变 | [匿名结果](local-project-compatibility.json) |

新增回归覆盖共同额度的并发原子预留、一次共享探针、多路线绑定、重复候选身份／工作 key、资源争用、未知用量不退款、用户核实后释放、分范围轮换、临时选择与迟到修订、不同口径不可比、组合新实验和冻结输入恢复。未物化、排队、已开始和退出未知混合状态在 stop/deadline 下分别结清；普通软暂停保留工作，重复回调不重复结果、通知或预算。

项目最终依据汇入所有本代范围和当前选择；关闭／暂缓范围、移出核心或删除路线的旧科学引用均保留风险路径及理由。局部已批准工作不因无关候选或 scope 改变而失效；相关撤回仍会阻止启动。范围/关系/结果变化会使旧草案失效。显式大包保留完整 UTF-8 页布局和摘要，缺页、错草案、旧 basis 或部分材料不能获得合法结项。

原 PR11 无节点失效回归改为修改已选路线前提，以符合 E3 的局部执行依据；原有 stale、结果、无假 task、通知及重启唤醒断言保留，另增候选不废工作回归。最新迁移幂等检查使用 schema16，旧迁移函数没有放宽到任意未来版本。第一轮失败及对应调整在 `checks.json` 说明，不把旧 695/172 记录当成本轮结果。

## 实际 OpenCode 与独立包

以下场景使用独立临时研究项目、独立 XDG、独立 OpenCode 进程及 localhost 剧本 provider。`used_external_model=false`；不改用户共享 OpenCode 配置或实际研究库。脚本从各节点自己的 kickoff 识别任务，避免并发时误用“最后一个节点”。宿主工具返回保存在对应 `*-trace.json`，汇总文件不是完整 RPC／事件流。

- [完整多路线](runs/host-full.json)及[工具记录](runs/host-full-trace.json)：一个共享探针关联两路线且只分配一次，快慢工作并行；缺失结果下作临时选择，迟到证据修订；组合新实验退化后恢复冻结单路线，最终 answered/resolved。
- [两个活跃范围](runs/host-multi.json)及[工具记录](runs/host-multi-trace.json)：分别读取局部依据与投入，共同预算，最终全范围停止说明和诚实 partial 结项。
- [独立包](runs/packed.json)：复制源码打 tarball、删除构建树、确认从解包内置 Python 加载，再运行相同 E3 全场景；其[场景输出](runs/host-packed-full.json)和[工具记录](runs/host-packed-full-trace.json)单独保存。
- 当前源码的旧契约实际宿主回归位于 `runs/e1e2-*.json`：排队前提失效、关闭探索、冷重启与材料确认故障。它们补充 E3 测试，不是新模型效果样本。

真实宿主中观察到 publication 查询与 finish 之间的新结果令旧决定／prepare CAS 失败。剧本重新读取并处置新增结果，再准备提交；没有放宽后端校验来获得通过。

## 复现

```bash
python3 -m pytest tests/ -q
cd plugin
bun run sync:python
bun test
bun run typecheck
```

在仓库根目录执行以下隔离宿主命令；环境须提供本地 OpenCode 2.2.5 与项目固定 SDK，localhost 监听须可用：

```bash
python3 scripts/probe_e3_run.py --scenario full --output /private/tmp/e3-host-full.json
python3 scripts/probe_e3_run.py --scenario multi --output /private/tmp/e3-host-multi.json
python3 scripts/probe_packed_release.py --e3 --e3-output /private/tmp/e3-host-packed.json
```

旧项目副本复现沿用 `scripts/e1e2_compatibility.py --roots <本机私有路径清单> --output <匿名输出>`。清单不纳入仓库；程序复制 DB/WAL 和对象后才打开副本，前后核对原库摘要，不接管原研究。

## 真实模型效果与限制

[四个可执行评估任务、配对运行与独立评分](evaluation/README.md)已提供；[设施自测](evaluation/selftest.json)及[实际合成测量](evaluation/selftest-measurements.json)验证测量入口、配对隔离和评分身份。八个自测项目由脚本填写答案，人工质量评分为空，真实模型配对效果 **未验证**。本会话未找到已固定且明确授权的 provider/model/总额度，因此未自行启动付费模型。

- 共同额度单位是计划 `work_units`。实际 token/费用仍按既有观测子集报告，未知用量不计零；不能宣称全局费用或设备时间硬封顶。
- 命名资源租约只管声明的受管工作，普通 finish 不证明外部进程退出，需用户核实后释放。工程测试没有测量真实 GPU 干扰，也不证明操作系统级设备隔离。
- 共享工作归属一个 scope，可关联该 scope 的多条路线；跨 scope 共享通过相同固定材料引用实现，复用证据不重复启动探针。候选 key 只去重已声明身份，不自动判断两个科学想法语义相同。
- 可比性检查针对模型声明的条件、固定引用及版本，不验证因果解释。组合收益仍须新实验，未授予确认性资格。
- 默认决策包 32,000 字节；显式分页最多 1 MiB，单页最多 16,384 字节，当前请求须完整交付。超过总量或模型上下文不能完整容纳时显式失败，不能裁掉风险；真实模型下的阅读成本未测。
- 升级前未提交草案可读，但新增全代依据后通常需重新 prepare；旧未启动意图若失效会保存 stale 结果并要求重选。旧成功结项和幂等重放不重写。
- 评估真值在研究目录之外；正式盲评还需操作系统/容器访问隔离。生成目录和不复制评分文件本身不证明安全隔离。

工程与宿主场景通过只证明上述契约和时序；新能力继续显式启用，发布版本仍为 v0.1.1，本地打包不等于已部署。

验证记录中的个人目录已在公开副本脱敏；原始本地输出保留在工作区。文中提交与发布状态以验证时点为准，PR 当前状态以 GitHub 为准。
