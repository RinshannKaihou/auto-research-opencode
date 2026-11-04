"""Real OpenCode 2.2.5 run: dump ctx.session.context({sessionID}) after a text -> tool -> text turn.

Isolated XDG dirs under a temp dir in /workspace/fv-shape; private `opencode serve` on a loopback port.
The model key is only inherited from the environment (OPENCODE_API_KEY) and never written anywhere.
"""
import base64, json, os, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path
from uuid import uuid4

OPENCODE = os.path.expanduser("~/.local/opt/opencode-v2/node_modules/.bin/opencode")
OUT = Path("/workspace/fv-shape")
PLUGIN = r'''import {appendFileSync, writeFileSync} from "node:fs";
const T = process.env.FV_TRACE, OUTF = process.env.FV_CONTEXT_OUT;
const log = (o) => appendFileSync(T, JSON.stringify(o) + "\n");
export default {id: "fv-shape-probe", async setup(ctx) {
  let open = true; const stream = ctx.event.subscribe(); const dumped = new Set();
  log({type: "probe.ready", sessionKeys: Object.keys(ctx.session)});
  void (async () => { try { for await (const e of stream) { if (!open) break;
    const sid = e.data?.sessionID; if (!sid) continue;
    log({type: e.type, session: sid, seq: e.durable?.seq, dataKeys: Object.keys(e.data ?? {}), data: e.type.startsWith("session.execution") ? e.data : undefined});
    if (/^session\.execution\.(succeeded|failed|interrupted|cancelled|errored)$/.test(e.type) && !dumped.has(sid)) {
      dumped.add(sid);
      try {
        const context = await ctx.session.context({sessionID: sid});
        writeFileSync(OUTF, JSON.stringify({trigger_event: e.type, sessionID: sid, context}, null, 2));
        log({type: "probe.context_dumped", session: sid, count: Array.isArray(context) ? context.length : null});
      } catch (err) { log({type: "probe.context_error", error: String(err?.stack ?? err)}); }
    }
  } } catch (err) { log({type: "probe.stream_error", error: String(err)}); } })();
  return () => { open = false; };
}};
'''

def req(base, path, pw, payload=None, timeout=10):
    data = json.dumps(payload).encode() if payload is not None else None
    auth = base64.b64encode(f"opencode:{pw}".encode()).decode()
    r = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read(); return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        raw = err.read(); return err.code, (json.loads(raw) if raw else {})

def main():
    root = Path(tempfile.mkdtemp(prefix="run-", dir=OUT))
    ws = root / "project"; ws.mkdir()
    (ws / "note.txt").write_text("The secret word is PINEAPPLE.\n")
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "OPENCODE_API_KEY") if k in os.environ}
    for n in ("DATA", "CONFIG", "STATE", "CACHE"):
        d = root / n.lower(); d.mkdir(); env[f"XDG_{n}_HOME"] = str(d)
    pw = uuid4().hex
    env.update(OPENCODE_SERVER_PASSWORD=pw, FV_TRACE=str(root / "events.jsonl"), FV_CONTEXT_OUT=str(root / "plugin-context.json"))
    pdir = root / "plugin"; pdir.mkdir()
    (pdir / "package.json").write_text(json.dumps({"name": "fv-shape-probe", "type": "module", "exports": {".": "./index.js"}}))
    (pdir / "index.js").write_text(PLUGIN)
    cfg = root / "config" / "opencode"; cfg.mkdir()
    (cfg / "opencode.jsonc").write_text(json.dumps({"plugins": [str(pdir)], "model": "opencode-go/deepseek-v4.1-flash"}))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    log = (root / "server.log").open("w+")
    proc = subprocess.Popen([OPENCODE, "serve", "--hostname", "127.0.0.1", "--port", str(port)], cwd=ws, env=env, stdout=log, stderr=log)
    try:
        dl = time.monotonic() + 30
        while True:
            try:
                if req(base, "/api/session/active", pw)[0] == 200: break
            except OSError: pass
            if proc.poll() is not None or time.monotonic() > dl: raise SystemExit("server did not start")
            time.sleep(0.2)
        sid = "ses_fvshape_" + uuid4().hex
        st, body = req(base, "/api/session", pw, {"id": sid, "title": "fv shape probe", "location": {"directory": str(ws)},
                                                  "model": {"providerID": "opencode-go", "id": "deepseek-v4.1-flash"}})
        print("create", st, json.dumps(body)[:400])
        time.sleep(1.0)
        prompt = ("Do exactly three steps, in this order, in a single turn: "
                  "(1) First write one short sentence saying you will now read note.txt. "
                  "(2) Then call the read tool on the file note.txt in the current directory. "
                  "(3) After the tool result, write one final sentence stating the secret word from the file.")
        st, body = req(base, f"/api/session/{sid}/prompt", pw, {"text": prompt, "resume": True})
        print("prompt", st, json.dumps(body)[:600])
        dl = time.monotonic() + 300
        out = root / "plugin-context.json"
        while time.monotonic() < dl and not out.exists():
            time.sleep(0.5)
        time.sleep(1.0)
        hst, http_ctx = req(base, f"/api/session/{sid}/context", pw)
        (root / "http-context.json").write_text(json.dumps(http_ctx, indent=2))
        print("plugin context dumped:", out.exists(), "http context status", hst)
        print("root", root)
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait()

if __name__ == "__main__":
    main()
