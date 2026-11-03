#!/usr/bin/env python3
"""TCP<->unix bridge. in:  listen tcp 127.0.0.1:PORT (inside netns) -> unix SOCK
                    out: listen unix SOCK (host)                 -> tcp 127.0.0.1:PORT"""
import socket, sys, threading, os
mode, sock_path, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
def pump(a, b):
    try:
        while (d := a.recv(65536)): b.sendall(d)
    except Exception: pass
    finally:
        for s in (a, b):
            try: s.shutdown(socket.SHUT_RDWR)
            except Exception: pass
def handle(c):
    if mode == "in": u = socket.socket(socket.AF_UNIX); u.connect(sock_path)
    else: u = socket.create_connection(("127.0.0.1", port))
    threading.Thread(target=pump, args=(c, u), daemon=True).start(); pump(u, c)
if mode == "in":
    L = socket.socket(); L.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); L.bind(("127.0.0.1", port))
else:
    if os.path.exists(sock_path): os.unlink(sock_path)
    L = socket.socket(socket.AF_UNIX); L.bind(sock_path)
L.listen(64)
while True:
    c, _ = L.accept(); threading.Thread(target=handle, args=(c,), daemon=True).start()
