"""S3: multi-turn continuity on one thread; two concurrent threads in one app-server process."""
import os, json, sys, threading, time; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
a = AppServer("s3"); a.init()
th = a.request("thread/start", {"cwd": f"{ROOT}/work"})["thread"]["id"]
_, d1, n1 = a.run_turn(th, "Remember this codeword for later: ZEBRA-4417. Reply only OK.")
_, d2, n2 = a.run_turn(th, "Call the fixture tool lookup with key beta. Reply only with its label.")
_, d3, n3 = a.run_turn(th, "What was the codeword I gave you, and what label did lookup return? Reply as CODE=<..> LABEL=<..>")
print("multi-turn:", [d["params"]["turn"]["status"] for d in (d1, d2, d3)], "| t1:", final_text(n1), "| t2:", final_text(n2), "| t3:", final_text(n3))
# concurrent threads
A = a.request("thread/start", {"cwd": f"{ROOT}/work"})["thread"]["id"]
B = a.request("thread/start", {"cwd": f"{ROOT}/work"})["thread"]["id"]
res = {}
def go(th, word, start_i):
    p = (f"Your secret word is {word}. Call the fixture tool step with i={start_i}, then call step again with the returned next value, "
         f"three step calls total, sequentially. Then reply: WORD={word} TOKENS=<tokens comma-separated>.")
    t0 = time.time(); tid, done, notes = a.run_turn(th, p, timeout=600)
    res[th] = (done["params"]["turn"]["status"], round(time.time() - t0, 1), final_text(notes, th),
               [ (n["params"]["item"].get("arguments")) for n in notes if n.get("method") == "item/completed"
                 and n["params"].get("threadId") == th and n["params"]["item"]["type"] == "mcpToolCall"])
ts = [threading.Thread(target=go, args=(A, "APPLE", 10)), threading.Thread(target=go, args=(B, "BANANA", 50))]
t0 = time.time(); [t.start() for t in ts]; [t.join() for t in ts]
print("concurrent wall:", round(time.time() - t0, 1))
for k, v in res.items(): print("thread", k[-6:], v)
exp = {A: ",".join(f"T{i*7%97:02d}" for i in (10, 11, 12)), B: ",".join(f"T{i*7%97:02d}" for i in (50, 51, 52))}
print("expected:", {k[-6:]: v for k, v in exp.items()})
print("loaded threads:", a.request("thread/loaded/list", {}))
a.close()
