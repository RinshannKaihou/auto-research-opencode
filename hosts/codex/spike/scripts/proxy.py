#!/usr/bin/env python3
"""Logging reverse proxy: Codex -> 127.0.0.1:PORT -> opencode.ai/zen/go/v1.
Logs one JSONL record per request (status, session header, model, #input items,
input item types, tools count, reasoning items echoed back, usage from
response.completed, errors). NEVER logs Authorization."""
import http.client, http.server, json, os, ssl, sys, time, threading, socketserver
UP_HOST = os.environ.get("UP_HOST", "opencode.ai")
UP_PREFIX = os.environ.get("UP_PREFIX", "/zen/go/v1")
PORT = int(os.environ.get("PROXY_PORT", "18080"))
LOG = os.environ.get("PROXY_LOG", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "proxy.jsonl"))
DUMP = os.environ.get("PROXY_DUMP")  # optional dir for full redacted bodies
lock = threading.Lock()
ctr = [0]
def log(rec):
    with lock:
        with open(LOG, "a") as f: f.write(json.dumps(rec) + "\n")
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self): self.handle_any("GET")
    def do_POST(self): self.handle_any("POST")
    def handle_any(self, method):
        with lock:
            ctr[0] += 1; n = ctr[0]
        t0 = time.time()
        ln = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(ln) if ln else b""
        rec = {"n": n, "t": round(t0, 3), "method": method, "path": self.path,
               "session": self.headers.get("x-opencode-session"),
               "content_encoding": self.headers.get("Content-Encoding"),
               "hdr_names": sorted(k.lower() for k in self.headers.keys()),
               "id_hdrs": {k.lower(): v for k, v in self.headers.items() if any(w in k.lower() for w in ("session", "conversation", "thread", "turn", "window"))}}
        try:
            j = json.loads(body) if body else {}
            inp = j.get("input") or []
            rec.update(model=j.get("model"), stream=j.get("stream"),
                       n_input=len(inp) if isinstance(inp, list) else None,
                       input_types=[i.get("type") or i.get("role") for i in inp] if isinstance(inp, list) else None,
                       n_tools=len(j.get("tools") or []),
                       tool_names=[t.get("name") or t.get("type") for t in (j.get("tools") or [])],
                       reasoning=j.get("reasoning"), include=j.get("include"),
                       prev_id=j.get("previous_response_id"), store=j.get("store"),
                       instr_len=len(j.get("instructions") or ""),
                       parallel_tool_calls=j.get("parallel_tool_calls"))
            if DUMP:
                with open(os.path.join(DUMP, f"req{n:04d}.json"), "w") as f: json.dump(j, f)
        except Exception as e:
            rec["parse_err"] = repr(e)[:200]
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "accept-encoding", "connection")}
        conn = http.client.HTTPSConnection(UP_HOST, context=ssl.create_default_context(), timeout=600)
        path = UP_PREFIX + (self.path[len("/v1"):] if self.path.startswith("/v1") else self.path)
        try:
            conn.request(method, path, body=body, headers={**hdrs, "Accept-Encoding": "identity", "Content-Length": str(len(body))})
            r = conn.getresponse()
        except Exception as e:
            rec.update(status="UPSTREAM_EXC", err=repr(e)[:300]); log(rec)
            self.send_error(502); return
        rec["status"] = r.status
        self.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() in ("transfer-encoding", "content-length", "connection", "content-encoding"): continue
            self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked"); self.send_header("Connection", "close")
        self.end_headers()
        buf = b""; events = {}; usage = None; out_types = []; errtxt = None
        try:
            while True:
                chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
                if not chunk: break
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n"); self.wfile.flush()
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.startswith(b"data:"):
                        try:
                            ev = json.loads(line[5:].strip())
                        except Exception: continue
                        typ = ev.get("type"); events[typ] = events.get(typ, 0) + 1
                        if typ in ("response.completed", "response.failed", "response.incomplete"):
                            resp = ev.get("response") or {}
                            usage = resp.get("usage"); out_types = [o.get("type") for o in resp.get("output") or []]
                            if resp.get("error"): errtxt = json.dumps(resp.get("error"))[:300]
                        if typ == "error": errtxt = json.dumps(ev)[:300]
            self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
        except Exception as e:
            rec["stream_err"] = repr(e)[:200]
        if r.status != 200 and not events:
            errtxt = buf[:400].decode("utf8", "replace")
        rec.update(dt=round(time.time() - t0, 2), events=events, usage=usage, out_types=out_types, err=errtxt)
        log(rec)
class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True
if __name__ == "__main__":
    print(f"proxy on {PORT} -> https://{UP_HOST}{UP_PREFIX}", flush=True)
    S(("127.0.0.1", PORT), H).serve_forever()
