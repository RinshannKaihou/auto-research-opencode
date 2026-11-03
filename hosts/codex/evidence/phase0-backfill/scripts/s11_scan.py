"""S11 leak scan. Compares file bytes against $OPENCODE_API_KEY in memory; prints ONLY counts/paths, never the value.
Also counts the key's base64 form and the '<REDACTED>' marker (appclient redaction would hide a leak in *.jsonl logs)."""
import os, sys, base64, collections
key = os.environ.get("OPENCODE_API_KEY")
assert key and len(key) >= 16, "key env missing"
pats = {"raw": key.encode(), "b64": base64.b64encode(key.encode())}
roots = sys.argv[1:] or ["/workspace/codex-spike/backfill"]
def bucket(p):
    for tag in ("sessions", "shell_snapshots", "logs", "work", "run", "scripts", "schema", "tmp"):
        if f"/{tag}/" in p: return tag
    if ".sqlite" in p: return "sqlite"
    return "other"
for root in roots:
    stats = collections.defaultdict(lambda: [0, 0, 0, 0])  # files, raw_hits, b64_hits, redacted_markers
    hitfiles = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in ("node_modules", "npm")]
        for f in files:
            p = os.path.join(d, f)
            try: b = open(p, "rb").read()
            except Exception: continue
            s = stats[(root, bucket(p))]; s[0] += 1
            r, b6 = b.count(pats["raw"]), b.count(pats["b64"]); s[1] += r; s[2] += b6; s[3] += b.count(b"<REDACTED>")
            if r or b6: hitfiles.append((p, r, b6))
    print(f"# root {root}")
    for (rt, bk), (n, r, b6, red) in sorted(stats.items()): print(f"  {bk:16s} files={n:5d} key_raw_hits={r} key_b64_hits={b6} redacted_markers={red}")
    print("  files with hits:", hitfiles if hitfiles else "none")
