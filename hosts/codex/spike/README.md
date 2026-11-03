# Codex app-server spike

Throwaway feasibility spike, not production code: codex-cli 0.166.0 `app-server` driving OpenCode Go `deepseek-v4.1-flash` over `/responses` (result GO; see REPORT.md).
Scripts under `scripts/` are the probes from that run, not a supported interface.
Re-run with codex-cli 0.166.0 at `npm/node_modules/.bin/codex` (`npm i --prefix npm @openai/codex@0.166.0`) and `OPENCODE_API_KEY` set. Create local `work/` and `logs/` beside `scripts/`.
`home/config.toml` must exist: the committed file is key-free (`env_key` only names `OPENCODE_API_KEY`). If it is missing, copy the toml block from REPORT.md into `home/config.toml`.
Start `python3 scripts/proxy.py` first and leave it running, because that config's `base_url` is `http://127.0.0.1:18080/v1`.
Then run `python3 scripts/s1_basic.py`.
