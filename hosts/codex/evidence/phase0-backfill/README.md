# P0.B backfill evidence (S2, S3, S4, S11, S12)

这些是一次性验证脚本，需要本地 codex-cli 和 OpenCode Go key，不进 CI；已测试版本 codex-cli 0.166.0（只是记录，不固定版本）。

结论、未验证项和对计划有影响的发现见 [REPORT.md](REPORT.md)。`scripts/` 不在 pytest 的 `testpaths`（`tests/`）里，`make test` 不会收集它们。

本目录是当时 `/workspace/codex-spike/backfill/` 的入库副本。`scripts/`、`logs/`、`run/` 相对本目录。日志和脚本里的 `/workspace/codex-spike/...` 是运行时原样记录。`home/`（CODEX_HOME）和 `work/` 没有入库。文中的 spike 日志代理是 [`hosts/codex/spike/scripts/proxy.py`](../../spike/scripts/proxy.py)。脚本写死了 `/workspace/codex-spike/...` 等运行时路径，在仓库里不能直接运行；`appclient.py` 在仓库里位于 [`hosts/codex/spike/scripts/appclient.py`](../../spike/scripts/appclient.py)。

入库时把机器标识换成了稳定占位符（行数不变，JSON 仍合法，env 仍是 23 个变量）：`logs/bf_main_p1.jsonl`、`logs/bf_s3_p2.jsonl`、`logs/bf_web_retry.jsonl`、`logs/trust.jsonl` 的 `serverName` 换成 `<HOST>`，这些文件以及 `logs/proxy.jsonl` 里的 installation id 换成 `<INSTALLATION_ID>`；`logs/proxy.jsonl` 的 `agent_name` 换成 `<HOME>`；`logs/bf_main_p1.jsonl` 的 env 输出里 `USER` 和 `LOGNAME` 换成 `<USER>`，`HOME` 换成 `<HOME>`。`logs/bf_main.summary.txt` 里同一段 env 在 `USER` 之前截断，所以只替换了其中的 `LOGNAME` 和 `HOME`。
