"""Probe isolated OpenCode session creation and synthetic-message identities.

Starts a private server with empty XDG directories and no model request.  The
probe never connects to the user's shared OpenCode service or research data.
"""
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from uuid import uuid4


def request(base, path, payload=None, *, password):
    data = json.dumps(payload).encode() if payload is not None else None
    auth = base64.b64encode(f"opencode:{password}".encode()).decode()
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def main():
    with tempfile.TemporaryDirectory(prefix="ari-host-probe-") as temporary:
        root = Path(temporary)
        workspace = root / "project"
        workspace.mkdir()
        env = os.environ.copy()
        for name in ("DATA", "CONFIG", "STATE", "CACHE"):
            directory = root / name.lower()
            directory.mkdir()
            env[f"XDG_{name}_HOME"] = str(directory)
        password = uuid4().hex
        env["OPENCODE_SERVER_PASSWORD"] = password
        env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
        plugin_dir = root / "probe-plugin"
        plugin_dir.mkdir()
        trace = root / "plugin-trace.jsonl"
        env["ARI_HOST_PROBE_TRACE"] = str(trace)
        (plugin_dir / "package.json").write_text(json.dumps({"name": "ari-host-probe", "type": "module", "exports": {".": "./index.js"}}))
        plugin = plugin_dir / "index.js"
        plugin.write_text('''import {appendFileSync} from "node:fs";
import {randomUUID} from "node:crypto";
const key=Symbol.for("ari.isolated.host.probe");
const epoch=globalThis[key] ??= randomUUID();
function note(phase, directory) {appendFileSync(process.env.ARI_HOST_PROBE_TRACE, JSON.stringify({phase, directory, pid:process.pid, epoch})+"\\n");}
export default {id:"ari-host-probe", async setup(ctx) {note("setup",ctx.location.directory); return () => note("cleanup",ctx.location.directory);}};
''')
        config_dir = root / "config" / "opencode"
        config_dir.mkdir()
        (config_dir / "opencode.jsonc").write_text(json.dumps({"plugins": [str(plugin_dir)]}))
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        with (root / "server.log").open("w+") as log:
            process = subprocess.Popen(["opencode", "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                                       cwd=workspace, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        active_status, _ = request(base, "/api/session/active", password=password)
                        if active_status == 200:
                            break
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError(f"The isolated OpenCode server did not become ready ({active_status})")
                        time.sleep(0.1)
                    except (OSError, urllib.error.URLError):
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("The isolated OpenCode server did not start")
                        time.sleep(0.1)
                suffix = uuid4().hex
                session_id = f"ses_probe_{suffix}"
                message_id = f"msg_probe_{suffix}"
                session = {"id": session_id, "title": "isolated host probe", "location": {"directory": str(workspace)}}
                first_create, _ = request(base, "/api/session", session, password=password)
                second_create, _ = request(base, "/api/session", session, password=password)
                message = {"id": message_id, "text": "isolated probe", "resume": False}
                first_send, first_result = request(base, f"/api/session/{session_id}/synthetic", message, password=password)
                second_send, second_result = request(base, f"/api/session/{session_id}/synthetic", message, password=password)
                context_status, context = request(base, f"/api/session/{session_id}/context", password=password)
                inbox_status, inbox = request(base, f"/api/session/{session_id}/inbox", password=password)
                messages = context.get("data", [])
                queued = inbox.get("data", [])
                reload_deadline = time.monotonic() + 10
                while not trace.exists() and time.monotonic() < reload_deadline:
                    time.sleep(0.1)
                before = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
                plugin.write_text(plugin.read_text() + "\n// reload probe\n")
                while time.monotonic() < reload_deadline:
                    entries = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
                    if sum(item["phase"] == "setup" for item in entries) > sum(item["phase"] == "setup" for item in before):
                        break
                    time.sleep(0.1)
                entries = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
                setups = [item for item in entries if item["phase"] == "setup" and item["directory"] == str(workspace)]
                result = {
                    "active_endpoint_status": active_status,
                    "create_statuses": [first_create, second_create],
                    "synthetic_statuses": [first_send, second_send],
                    "context_status": context_status,
                    "matching_messages": sum(item.get("id") == message_id for item in messages),
                    "context_types": [item.get("type") for item in messages],
                    "inbox_status": inbox_status,
                    "inbox_items": len(queued),
                    "same_returned_inbox_id": first_result.get("data", {}).get("id") == second_result.get("data", {}).get("id"),
                    "plugin_setups": len(setups),
                    "reload_preserved_pid_and_epoch": len(setups) >= 2 and len({(item["pid"], item["epoch"]) for item in setups}) == 1,
                    "models_invoked": False,
                }
                print(json.dumps(result, sort_keys=True))
                if first_create >= 300 or first_send >= 300 or context_status >= 300:
                    raise SystemExit(1)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
