# E3 真实模型效果评估设施

当前仅完成可执行任务和设施自测，**没有真实模型配对结果**。自测中的答案由脚本构造，测量端是合成研究任务，既不调用模型 provider，也不证明 E3 提高研究质量。旧 E1/E2 的文本样题继续保留，本目录补足真实可调用的测量入口、隔离配对目录、运行记录和独立评分。

## 任务与隔离

`scripts/e3_evaluation.py` 提供四题，每题生成的项目只有该题 `TASK.md`、通用测量客户端和能力令牌；单路径题提供有失败测试的实际 Python 程序。隐藏测量真值、组别映射及评分钥匙保存在 campaign 的 `private/`，位于各研究项目目录外，不复制到研究者项目或上下文。

| 任务 | 可执行对象 | 应检验的行为 |
|---|---|---|
| E3-01-combination | A、B、AB 的同条件测量 | 组合必须实测；退化后恢复单路线，不相加收益 |
| E3-02-reactivation | 旧 steady 记录及新 burst 测量 | 保留旧适用范围，因条件改变恢复暂缓路线 |
| E3-03-shared-async | 一次公共校准、A/B 快慢 HTTP 返回 | 公共校准记账一次；迟到证据可改变临时选择 |
| E3-04-direct | `delivery.py` 与现有 unittest | 直接修复和验证，不要求建立额外路线 |

测量 broker 在 `127.0.0.1` 上工作，按每个 run 的令牌、固定条件和额度提供结果。校准及每种测量按内容身份去重；并发预留在锁内完成，等待在锁外，因此 A/B 能真实重叠并异步返回。返回固定基线/数据引用、metric/unit、environment、重复和不确定性，原始事件保存在私有 `measurements.json`。broker 重启时未完成的测量保持 unknown 和已预留额度，不盲目重跑。

这些是**受控合成任务**，不是生产性能实验。文件系统隔离并非安全沙箱；正式运行应在评估专用机器/容器内限制研究者读取 campaign 的 `private/` 和仓库评分源文件，不能只依赖“不要读”的提示来声称评分盲法已经成立。当前工程自测只验证评分文件未复制，不声称实现了 OS 访问隔离。

## 冻结配对与运行

先复制并填写 [配置样例](configuration.example.json)。其中模型和 E3 版本是占位符，command 为 null，不能直接启动模型。固定实际已授权 provider/model/variant、全部工具、上下文策略、每任务模型/测量预算及两组精确软件版本。默认每题每组 3 次；相同题和重复次数共享种子与输入，组别运行顺序随机化。三个样本只用于描述性观察，不保证统计显著。

```bash
python3 scripts/e3_evaluation.py freeze \
  --configuration /private/tmp/e3-eval-config.json \
  --destination /private/tmp/e3-eval-campaign --repeats 3 --seed 17

python3 scripts/e3_evaluation.py serve \
  --manifest /private/tmp/e3-eval-campaign/private/manifest.json
```

broker 不调用模型。它把本次 localhost 地址写入各项目的 `measurement.json`，可先用普通节点执行 `python3 measure.py calibrate`，再 `python3 measure.py measure --variant A`。没有启动 broker 时客户端明确报错，不退到外部服务。

两组应分别加载冻结的 E1/E2 包与 E3 包，不能只用同一个 E3 实现改变任务文字来声称测得持久多路线机制的贡献。配置中的 arm `command` 是 argv 数组，允许 `{project}`、`{provider}`、`{model}`、`{variant}` 占位；建议调用评估者自己的包装器，负责加载对应包、启用相同 R1 读取和 evidence-v1、导入任务并强制模型请求/token/费用上限。可用 `host_config` 显式指定该组的独立宿主配置。

```bash
# 只有此处显式执行被冻结的命令；准备/评分不会调用模型。
python3 scripts/e3_evaluation.py run --execute \
  --manifest /private/tmp/e3-eval-campaign/private/manifest.json --run-id run-001
```

runner 给每个 run 单独的 XDG config/data/state/cache，清除继承的 OpenCode 服务地址和配置覆盖，保留 stdout/stderr、argv、墙钟、退出状态。它不安装插件、不重启共享宿主、不选择默认 provider。任意包装器仍可能忽略 XDG，正式评估前须核验实际宿主/插件清单。runner 只执行墙钟超时；模型请求/token/费用阈值必须由包装器或服务端落实，不能把这份 manifest 当作费用硬上限。超时保留 interrupted 和外部退出 unknown，不自动重试同一 run。

已有宿主也可在生成的独立项目运行，然后导入独立侧的观测 JSON：

```json
{
  "state": "interrupted",
  "observed_model": {"provider": "实际provider", "model": "实际model", "variant": "实际variant"},
  "observed_software_revision": "实际源码或包manifest",
  "usage": {"tokens": null, "cost": null, "model_requests": null},
  "notes": "保留中断、重试、宿主日志路径和观测缺口"
}
```

```bash
python3 scripts/e3_evaluation.py record \
  --manifest /private/tmp/e3-eval-campaign/private/manifest.json \
  --run-id run-001 --result /private/tmp/run-001-observations.json
```

录入保留原始观测版本，核对实际模型/软件与冻结值；缺失为 unknown，配置不匹配的运行保留但不能混入匹配效果估计。失败、中断和预算拒绝不剔除；不得重跑同一 ID 只留下较好结果。

## 评分与解释

研究者写 `answer.json`，引用实际收到的 measurement IDs；正常框架账本和报告一并保留。评分命令不把答案判定或组别注入研究会话：

```bash
python3 scripts/e3_evaluation.py score \
  --manifest /private/tmp/e3-eval-campaign/private/manifest.json \
  --run-id run-001 --output /private/tmp/run-001-blind-score.json
```

自动检查只覆盖题目固定选择、范围字段、测量引用身份、必需组合/条件测量、测量额度和单路径真实测试。它不通过关键词判断科学推理；`manual_quality_score` 始终为空，评分者按 [独立评分口径](scoring.md)先评分，再揭示 manifest 的组别。比较同题同种子的配对质量、无效投入、模型成本和墙钟分布；分支数和修订数不计正分。缺失实际用量时报告覆盖，不能填零或算节省比例。

即使合成任务上的真实模型配对结果良好，也只能支持这些任务/模型/额度下的观察。不能从本目录的工程自测、实际 OpenCode 的模拟 provider 或测量 broker 的固定答案推导真实模型探索提升。

## 本轮可复现自测

```bash
python3 scripts/e3_evaluation.py selftest \
  --output docs/evidence/e3-multiroute/evaluation/selftest.json
```

[本轮输出](selftest.json)与[实际合成测量及事件](selftest-measurements.json)覆盖四题、两组共八个独立目录：实际 subprocess 客户端、localhost HTTP、共享校准去重、A/B 同时在途、配对输入/样本一致、预算拒绝、伪造引用拒绝、未显式执行拒绝、独立 XDG 的无模型 stub 和观测配置偏差/缺失用量保留。所有答案由脚本填写，`real_model_effect=not_run`，不是模型效果样本。
