"""Trust hooks by writing [hooks.state."<key>"].trusted_hash = currentHash (what /hooks does), from hooks/list."""
import os, re, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *

_HEADER = re.compile(r'^\[hooks\.state\."(.*)"\]$')

def write_hook_trust(path, hooks):
    """Write each [hooks.state."<key>"] table once. A later run replaces that table instead of appending another."""
    try:
        text = open(path).read()
    except FileNotFoundError:
        text = ""
    wanted = {}
    order = []
    for h in hooks:
        if h["key"] not in wanted:
            order.append(h["key"])
        wanted[h["key"]] = h["currentHash"]
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        m = _HEADER.fullmatch(lines[i].strip())
        if m and m.group(1) in wanted:
            i += 1
            while i < len(lines):
                s = lines[i].strip()
                if s.startswith("[") and s.endswith("]"):
                    break
                i += 1
            continue
        out.append(lines[i])
        i += 1
    while out and out[-1].strip() == "":
        out.pop()
    for key in order:
        if out:
            out.append("")
        out.append(f'[hooks.state."{key}"]')
        out.append(f'trusted_hash = "{wanted[key]}"')
    body = ("\n".join(out) + "\n") if out else ""
    if body != text:
        with open(path, "w") as f:
            f.write(body)

def main():
    a = AppServer("s7list", env_extra={"CODEX_HOME": f"{ROOT}/home_hooks"}); a.init()
    hs = a.request("hooks/list", {"cwds": [f"{ROOT}/work"]})["data"][0]["hooks"]; a.close()
    write_hook_trust(f"{ROOT}/home_hooks/config.toml", hs)
    a = AppServer("s7list", env_extra={"CODEX_HOME": f"{ROOT}/home_hooks"}); a.init()
    print([(h["eventName"], h["trustStatus"]) for h in a.request("hooks/list", {"cwds": [f"{ROOT}/work"]})["data"][0]["hooks"]]); a.close()

if __name__ == "__main__":
    main()
