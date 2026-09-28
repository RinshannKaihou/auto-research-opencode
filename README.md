# Auto Research · OpenCode V2 自主研究插件

给出一个研究目标，插件在 OpenCode V2 里驱动模型自己完成规划、派发节点、做实验、综合结果和收尾；你可以随时插话指导、暂停或停止。研究过程中的节点、发布材料、知识和依据都记在项目目录的研究账本里，可以接续、可以追查。

完整的使用说明见 [plugin/README.md](plugin/README.md)。

## 目录

| 路径 | 内容 |
|---|---|
| `plugin/` | OpenCode 插件（TypeScript）：推进引擎、研究工具、节点内专家、看板和状态栏 |
| `src/auto_research/` | 研究账本（Python 3.11+）：插件通过私有进程调用，负责 SQLite 和文件事务，不调用模型 |
| `tests/` | 账本的 Python 测试 |

## 安装

需要 OpenCode V2（`@opencode/plugin` 2.0.18）、Python 3.11 以上和 Bun。

1. 安装插件依赖：

   ```bash
   cd plugin && bun install
   ```

2. 在 OpenCode 配置（`~/.config/opencode/opencode.json`）里加上插件目录：

   ```json
   { "plugins": ["/绝对路径/auto-research-opencode/plugin"] }
   ```

3. 在想放研究项目的目录里启动 OpenCode，输入 `/research auto <研究目标>`。

插件只影响研究会话；普通会话的请求和装插件之前完全一样。

## 开发

```bash
make test          # Python 测试 + 插件测试 + 类型检查
make test-python   # 只跑 Python 测试
make test-plugin   # 只跑插件测试（假宿主 + 真实 Python）
```

## 和 DSH 版的关系

研究账本的代码从 DSH 版（[auto-research-dsh](https://github.com/RinshannKaihou/auto-research-dsh) 0.6.11）复制而来，数据格式相同（schema 9）。所以 `/research takeover` 能接管 DSH 建的项目。DSH 版已经冻结，两边的代码从此各自维护。
