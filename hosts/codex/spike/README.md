# Codex app-server spike

Throwaway feasibility spike, not production code: codex-cli 0.166.0 `app-server` driving OpenCode Go `deepseek-v4.1-flash` over `/responses` (result GO; see REPORT.md).
Scripts under `scripts/` are the probes from that run, not a supported interface.
Re-run with codex-cli 0.166.0 at `npm/node_modules/.bin/codex` (`npm i --prefix npm @openai/codex@0.166.0`), `OPENCODE_API_KEY` set, and local `home/`, `work/`, and `logs/` beside `scripts/`: `python3 scripts/s1_basic.py`.
