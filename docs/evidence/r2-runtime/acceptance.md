# R2 统一本地验收

开发与内部回归已完成。以下是一次完整的用户验收流程；本轮未更改共享 OpenCode 配置、重启服务或调用真实模型。

## 1. 安装前与旧项目

1. 保留当前插件配置、代码版本和研究项目完整副本。完成旧项目只读检查前，不对原项目执行 attach／status／resume 等可能可写打开的控制操作。
2. 使用 `NativeService.project_read` 或现有只读看板检查 schema 9／10 项目：历史结项、publication、知识版本、来源和 todo 可读；只读能力显示 requires_migration。数据库与 WAL 摘要不应改变。
3. 在副本验证迁移，原项目继续保留。批量复验脚本：

   ```bash
   PYTHONPATH=src python3 scripts/r2_compatibility.py \
     --roots /path/to/local-project-roots.json \
     --output /tmp/r2-compatibility.json
   ```

   roots 文件由本地验收者提供，格式为 `{"projects":[{"root":"/absolute/path/to/research-project"}]}`，也接受原 R1 兼容报告。该脚本使用稳定 DB／WAL 副本，原对象以只读访问方式供查询，不对原项目可写打开。已有 8 项结果见 `local-project-compatibility.json`，完整本机项目路径不随 PR 提交。
4. 首次可写打开副本后应为 schema 11，并有 schema-10 备份；schema 9 还应有 schema-9 备份。旧表、旧 receipt 和结项标签不变，旧 todo 只成为 unknown 候选。结束的项目不自动重开，也不能据静态页面证明运行时能力。

## 2. 在隔离环境加载当前插件

1. 仓库 `plugin/` 执行 `bun install`、`bun run sync:python`、`bun run typecheck`。独立包验证执行 `bun test test/package.test.ts`。
2. 在独立 OpenCode 服务／配置和临时研究目录加载该插件；若改用共享服务，应先安排正在运行的会话，再重启加载。不要只关窗口假定新代码已加载。
3. 查看 capabilities：r2-v1、receipt-cas-v1、candidate-obligation-v1；旧后端应明确报契约不兼容。
4. 使用新测试项目，先 `/research init <小型研究目标>`，配置有限预算，例如 turns 8、nodes 3、seconds 300，再 `/research auto`。模型与工具权限由验收者选择。仅进入这一步后才消耗真实模型额度。

## 3. 一次运行验证

| 操作 | 应观察到 |
|---|---|
| 在主／节点请求查看可用工具 | 五个窄工具可见，旧 research_memory 不重复注入；专家仍只读 |
| context 已完整交付后仅用 state 写 checkpoint | 自动使用该请求的基线；无需重抄当前 revision |
| 同基线并行两次写；或一方追加后另一方提交旧 base | 只有一次新 revision；另一方 checkpoint_conflict，不能自动刷新后写穿 |
| 缺完整交付或进程重启后无基线 | checkpoint_base_required；先读 checkpoint，重新综合后再写 |
| editorial／correct_previous + 显式来源保留 | preset 范围确定；返回检查与 query 一致；空／unlinked 来源不升级 |
| 普通 publication／finish | 增加候选，不把全部历史 pending 当阻断 |
| 主协调 checkpoint 声明 decision_refs／节点引用证据 | 形成当前精确版本／owner／change 的义务；只读筛选不改变范围 |
| 主协调 research_review_scope 声明 refs／reason | 不依赖 checkpoint 基线即可更新采用范围；节点和专家不能代主协调设置 |
| 独立非空报告精确 reviews，complete=true，result=challenges | 对应任务完成，coverage 尚未评估，挑战与未处置风险仍可见 |
| complete=false 的 challenge；随后节点结束／结项／新 generation | challenge 保留；用 research_dispose 的精确 review_event 和证据另行处置，不改写报告历史 |
| 缩小范围再恢复、reopen 后提交旧报告 | out_of_scope 保留记录；重入恢复合理状态；旧报告不能抵消重开 |
| grounded_in／motivated_by；同用途 consume 两次 | 知识关系追加新版本；只有支撑传播；同用途去重而不同用途保留 |
| 连续 note／checkpoint／相同处置，或纯文案修改 | 不算有意义新进展；连续无进展会暂停 |
| 节点源码／实验配置真实改变后继续；随后仅 touch 或改文书 | 实际源码变化可算开发推进；无内容变化、Markdown 文书不刷新计数 |
| 节点含 env／site-packages 或大型代码树；重放 runtime 请求 | 环境不进入摘要；探测有资源上限，unknown 不刷新 idle，后端不读源码；重放返回原响应 |
| 长 checkpoint；deadline 刷新读取失败 | 超过 4,000 字符的 checkpoint 整块省略，科研依据仍可见；旧定时器保留并执行 |
| 达轮次／节点／时限、再 resume／重启 | 不越限继续自主调度，不清账；显式提额并恢复后才继续 |
| 到时仍在运行的节点／专家 | 请求中断，排队取消，退出未证实保持未核实；不声称外部计算已退出 |

自动化复现相同关键路径：`python3 -m pytest -q tests/test_r2_*.py`，以及 `plugin/` 下 `bun test`。宿主模拟验证接口与调度行为，真实宿主验收仍需观察实际事件和后续模型请求。

## 4. 界面、计量与回退

- `/research board` 保持概览／研究／成果三个主入口。“成果 → 依据检查”按 f 切换当前风险／义务／候选／历史／文书；核对 80、132 列的完成任务与未解决风险、报告链接。“研究 → 运行会话”显示预算和未知覆盖。
- 检查 `.research/observations/*.jsonl`：拒绝仍有 start/end，实际重试与业务重放分开，实际模型／variant 可追踪，缺失 token／cost 是 unknown，usage.updated 聚合不重复计量，日志不含命令／参数正文。用 `scripts/r2_measure.py` 汇总。
- Legacy／R1 均保留；切回 Legacy 不关闭 CAS、不降 schema。代码回退前停写并保存升级后完整目录，旧备份只恢复到副本；新记录丢失不称无损回退。
- 本次验收证明功能与兼容性。真实科研收益、单位成本和等预算优劣使用另备的 M4 配对任务评估，不能由页面或单次运行推定。
