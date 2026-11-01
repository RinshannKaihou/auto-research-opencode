# 第二轮审阅修复验证

本目录 JSON 为本轮隔离宿主探针直接输出，非人工汇总；仅使用临时项目、独立 XDG／注册表及本地剧本模型。未使用共享服务或真实研究数据。

运行方式（从仓库根目录）：

```bash
python3 scripts/probe_research_run.py --pause-node --resume-after-pause --plugin "$PWD/plugin"
python3 scripts/probe_research_run.py --specialist --warm-reload-specialist --plugin "$PWD/plugin"
```

暂停／恢复输出保存在 pause-resume.json，同进程专家接续输出保存在 warm-specialist.json。自动测试与第二轮意见处理另见上一级 review-round2.md；这些探针不等同宿主配置热更新卸载顺序或所有多进程交错验证。
