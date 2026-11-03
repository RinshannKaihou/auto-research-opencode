# P0.B backfill evidence (S2, S3, S4, S11, S12)

这些是一次性验证脚本，需要本地 codex-cli 和 OpenCode Go key，不进 CI；已测试版本 codex-cli 0.166.0（只是记录，不固定版本）。

结论、未验证项和对计划有影响的发现见 [REPORT.md](REPORT.md)。`scripts/` 不在 pytest 的 `testpaths`（`tests/`）里，`make test` 不会收集它们。

本目录是当时 `/workspace/codex-spike/backfill/` 的入库副本。`scripts/`、`logs/`、`run/` 相对本目录。日志和脚本里的 `/workspace/codex-spike/...` 是运行时原样记录。`home/`（CODEX_HOME）和 `work/` 没有入库。文中的 spike 日志代理是 [`hosts/codex/spike/scripts/proxy.py`](../../spike/scripts/proxy.py)。
