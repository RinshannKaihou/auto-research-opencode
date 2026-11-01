# E1/E2 工程验证

本记录区分后端测试、插件 fakeHost、实际 OpenCode 和旧项目副本。评审处理及保留意见见[第一轮](review-response.md)和[第二轮](review-response-round2.md)；[第一轮源码摘要](source-manifest.json)记录对应源码哈希。以下均为工程验证，不替代用户验收或真实模型效果比较。

- **后端和插件回归**：Python 全量 691 项、Bun 全量 163 项通过；TypeScript 与 PR 范围 diff 检查通过。实际输出见 [Python](runs/pr11-pytest.txt)、[Bun](runs/pr11-bun-test.txt) 和 [命令记录](runs/pr11-checks.json)。fakeHost 场景包括过期与有效页混合、材料跨请求读取、失效/关闭唤醒及未物化意图恢复。
- **实际 OpenCode，模拟 provider**：[完整 E1/E2](runs/pr11-host-full.json) 登记两个候选、首次探针不足后转向，读取三页材料并以 qualified 状态结项；[排队探针失效](runs/pr11-host-stale.json) 和 [关闭探索](runs/pr11-host-off.json) 均验证结果、通知、唤醒和后续收尾；[冷重启](runs/pr11-host-restart.json) 保留未知执行及排队探针，显式恢复后没有重复创建节点或意图。这些文件是脚本的结构化汇总，不是原始事件或 RPC 日志。场景使用隔离 OpenCode 和本地脚本，不调用外部模型。
- **独立发布包**：[打包 OpenCode 场景](runs/pr11-packed-e1e2.json) 从独立 tarball 解压，删除构建源码目录，确认后端从包内加载后运行完整 E1/E2。此前的 [基础节点宿主结果](runs/packed-host.json) 仅证明 propose/dispatch/finish，不作为本次完整 E1/E2 的证据。
- **旧项目独立副本**：[迁移输出](local-project-compatibility.json) 对 5 个可访问的 schema 10 项目复制 DB/WAL 和对象，逐级迁移至 schema 15，核对历史列和原始 DB/WAL 哈希。另有 3 个项目不可访问，未测试；没有接管或重新运行原研究。
- **历史范围的体积边界**：[匿名体积测量](project-evidence-sizing.json) 对相同副本读取现有报告引用或最新 claim，比较结构视图与不含实际风险判断的占位决策包。5 个样本的结构视图为 34,726–160,479 字节，占位包为 15,230–64,856 字节；其中 2 个占位包已超过 32,000 字节。真实判断还会占空间，宽范围可能需要用户/协调者显式缩小；这些值不是 token，也不是已经准备的正式草案。

可使用 [副本迁移脚本](../../../scripts/e1e2_compatibility.py)、[体积测量脚本](../../../scripts/e1e2_evidence_sizing.py)、[实际宿主脚本](../../../scripts/probe_e1e2_run.py) 和 [独立包脚本](../../../scripts/probe_packed_release.py)复现。副本脚本的输入是本机路径清单，输出只含匿名编号与计数，不写源路径或内容。

## 第二轮验证

第二轮 Python 全量 695 项、Bun 全量 172 项与 TypeScript 检查通过；日志见 [Python](runs/pr11-round2-pytest.txt) 和 [Bun](runs/pr11-round2-bun-test.txt)。[命令记录](runs/pr11-round2-checks.json)和[源码摘要](source-manifest-round2.json)记录完整回归与版本对应关系；[完整宿主](runs/pr11-round2-host-full.json)及[独立发布包](runs/pr11-round2-packed-e1e2.json)也重新验证通过。[材料失效场景汇总](runs/pr11-round2-host-material.json)和[该场景选取的合成工具结果](runs/pr11-round2-host-material-trace.json)分别记录结论与实际返回内容；后者不是完整事件流。测试覆盖有效／失效页混合、批内索引转换、后端失败重试、响应乱序、旧草案、显式风险分组和逐项审计。

[更新后的历史副本占位测量](project-evidence-sizing-round2.json)中 5 项分别为 15,230、26,062、59,960、23,548、39,531 UTF-8 字节；两项仍超过 32,000 字节。此测量不含实际风险判断，不代表真实草案或模型 token。

[真实模型评估准备](evaluation/README.md)中的任务是公开样题；尚未开展真实模型质量或成本对照。阅读范围回执只证明宿主确认交付，不证明模型理解材料或科学结论正确。
