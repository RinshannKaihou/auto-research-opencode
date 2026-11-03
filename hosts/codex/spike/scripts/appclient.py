"""Minimal JSON-RPC (newline-delimited, over stdio) client for `codex app-server`."""
import json, os, subprocess, threading, time, queue, itertools
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODEX = f"{ROOT}/npm/node_modules/.bin/codex"
KEY = os.environ.get("OPENCODE_API_KEY") or "\x00never"
def redact(s): return s.replace(KEY, "<REDACTED>") if KEY else s

class AppServer:
    def __init__(self, log_name, extra_args=(), env_extra=None):
        env = dict(os.environ, CODEX_HOME=f"{ROOT}/home", RUST_LOG=os.environ.get("RUST_LOG", "warn"))
        env.update(env_extra or {})
        self.logf = open(f"{ROOT}/logs/{log_name}.jsonl", "a")
        self.errf = open(f"{ROOT}/logs/{log_name}.stderr", "a")
        self.p = subprocess.Popen([CODEX, "app-server", *extra_args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=self.errf, env=env, cwd=f"{ROOT}/work", text=True, bufsize=1)
        self.ids = itertools.count(1); self.pending = {}; self.notes = []; self.cv = threading.Condition()
        self.server_request_handler = None
        threading.Thread(target=self._reader, daemon=True).start()
    def _log(self, d, o):
        self.logf.write(redact(json.dumps({"t": round(time.time(), 3), "dir": d, **o})) + "\n"); self.logf.flush()
    def _reader(self):
        for line in self.p.stdout:
            try: m = json.loads(line)
            except Exception: continue
            self._log("<", m)
            if "id" in m and ("result" in m or "error" in m):
                with self.cv: self.pending[m["id"]] = m; self.cv.notify_all()
            elif "id" in m and "method" in m:  # server -> client request
                res = self.server_request_handler(m) if self.server_request_handler else self._default_srv(m)
                self.send({"jsonrpc": "2.0", "id": m["id"], "result": res})
            else:
                with self.cv: self.notes.append(m); self.cv.notify_all()
        with self.cv: self.cv.notify_all()
    def _default_srv(self, m):
        # SPIKE ONLY — unsafe. Auto-accepts every approval and elicitation.
        # Do not copy this into daily or production use.
        meth = m["method"]
        if "pproval" in meth: return {"decision": "accept"}
        if "elicitation" in meth.lower(): return {"action": "accept", "content": {}}
        return {}
    def send(self, o):
        self._log(">", o); self.p.stdin.write(json.dumps(o) + "\n"); self.p.stdin.flush()
    def request(self, method, params=None, timeout=120, raise_err=True):
        i = next(self.ids); self.send({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}})
        end = time.time() + timeout
        with self.cv:
            while i not in self.pending:
                if time.time() > end: raise TimeoutError(method)
                if self.p.poll() is not None: raise RuntimeError("app-server exited")
                self.cv.wait(0.5)
            m = self.pending.pop(i)
        if "error" in m and raise_err: raise RuntimeError(f"{method}: {m['error']}")
        return m.get("result", m)
    def notify(self, method, params=None): self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})
    def init(self, name="spike", experimental=True):
        r = self.request("initialize", {"clientInfo": {"name": name, "version": "0.1"},
                                        "capabilities": {"experimentalApi": experimental}})
        self.notify("initialized"); return r
    def wait_note(self, pred, timeout=300, start=0):
        end = time.time() + timeout
        with self.cv:
            while True:
                for n in self.notes[start:]:
                    if pred(n): return n
                if time.time() > end: raise TimeoutError("wait_note")
                if self.p.poll() is not None: raise RuntimeError("app-server exited")
                self.cv.wait(0.5)
    def turn_completed(self, thread_id, turn_id, timeout=300):
        return self.wait_note(lambda n: n.get("method") == "turn/completed" and n["params"].get("threadId") == thread_id
                              and (n["params"].get("turn") or {}).get("id") == turn_id, timeout)
    def notes_for(self, thread_id=None, turn_id=None):
        out = []
        for n in list(self.notes):
            p = n.get("params") or {}
            if thread_id and p.get("threadId") not in (thread_id, None): continue
            if turn_id and p.get("turnId", (p.get("turn") or {}).get("id")) not in (turn_id, None): continue
            out.append(n)
        return out
    def run_turn(self, thread_id, text, timeout=300, **kw):
        start = len(self.notes)
        r = self.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}], **kw})
        tid = r["turn"]["id"]
        done = self.turn_completed(thread_id, tid, timeout)
        return tid, done, self.notes[start:]
    def close(self):
        try: self.p.stdin.close()
        except Exception: pass
        try: self.p.wait(15)
        except Exception: self.p.kill()

def final_text(notes, thread_id=None):
    msgs = [n["params"]["item"] for n in notes if n.get("method") == "item/completed"
            and n["params"]["item"].get("type") == "agentMessage" and (thread_id is None or n["params"].get("threadId") == thread_id)]
    return msgs[-1].get("text") if msgs else None
def methods(notes):
    c = {}
    for n in notes: c[n.get("method")] = c.get(n.get("method"), 0) + 1
    return c
