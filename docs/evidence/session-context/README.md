# OpenCode 2.2.5 `ctx.session.context` for a text, tool, text turn

Captured on OpenCode 2.2.5 with model `opencode-go/deepseek-v4.1-flash`. The plugin called `ctx.session.context({sessionID})` when `session.execution.succeeded` fired. `GET /api/session/:id/context` matched that result. `probe_session_context.py` is the script that ran the capture. It reads `OPENCODE_API_KEY` from the environment and does not write the key. The word in `note.txt` is the probe's fixture, not a repository secret.

`session-context-tool-turn.json` holds that context array and the event trace.

The transcript shows:

- Tool calls are `{type:"tool", ...}` parts inside an assistant message's `content`.
- Each model step is its own assistant item.
- The last item is `{type:"idle", outcome}`.
