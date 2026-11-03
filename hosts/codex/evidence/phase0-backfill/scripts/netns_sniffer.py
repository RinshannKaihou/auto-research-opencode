#!/usr/bin/env python3
"""S12 observer, runs as root INSIDE the isolated netns (only lo, `local default` route => every IPv4 dst is local).
 1) AF_PACKET sniffer on lo: logs every TCP SYN and every UDP datagram (dst ip:port) => sees ALL connect attempts.
 2) DNS server on 0.0.0.0:53 (resolv.conf nameserver 10.0.0.2 is on lo): logs qname, answers A=127.0.0.1, AAAA empty.
 3) sinks on :80/:443: log Host header / TLS SNI, then close."""
import socket, struct, threading, json, time, sys
LOG = sys.argv[1]
lock = threading.Lock()
def lg(o):
    with lock, open(LOG, "a") as f: f.write(json.dumps({"t": round(time.time(), 3), **o}) + "\n")
def sniff():
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003)); s.bind(("lo", 0))
    while True:
        pkt, addr = s.recvfrom(65535)
        if addr[2] == socket.PACKET_OUTGOING: continue   # each lo pkt seen twice; keep incoming copy
        eth = struct.unpack("!H", pkt[12:14])[0]
        if eth == 0x0800:
            ihl = (pkt[14] & 15) * 4; proto = pkt[23]; src = socket.inet_ntoa(pkt[26:30]); dst = socket.inet_ntoa(pkt[30:34]); o = 14 + ihl
        elif eth == 0x86DD:
            proto = pkt[20]; src = socket.inet_ntop(socket.AF_INET6, pkt[22:38]); dst = socket.inet_ntop(socket.AF_INET6, pkt[38:54]); o = 54
        else: continue
        if proto == 6:
            sp, dp = struct.unpack("!HH", pkt[o:o+4]); fl = pkt[o+13]
            if fl & 0x02 and not fl & 0x10: lg({"src": "sniff", "proto": "tcp-syn", "dst": f"{dst}:{dp}", "sport": sp})
        elif proto == 17:
            sp, dp = struct.unpack("!HH", pkt[o:o+4])
            if sp != 53: lg({"src": "sniff", "proto": "udp", "dst": f"{dst}:{dp}"})
def qname(b, i=12):
    parts = []
    while b[i]: parts.append(b[i+1:i+1+b[i]].decode(errors="replace")); i += 1 + b[i]
    return ".".join(parts), i + 1
def dns():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("0.0.0.0", 53))
    while True:
        q, a = s.recvfrom(4096)
        try:
            name, i = qname(q); qtype = struct.unpack("!H", q[i:i+2])[0]
            lg({"src": "dns", "qname": name, "qtype": qtype})
            hdr = q[:2] + b"\x81\x80" + q[4:6] + (b"\x00\x01" if qtype == 1 else b"\x00\x00") + b"\x00\x00\x00\x00"
            ans = (b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04" + socket.inet_aton("127.0.0.1")) if qtype == 1 else b""
            s.sendto(hdr + q[12:i+4] + ans, a)
        except Exception as e: lg({"src": "dns", "err": str(e)})
def sni(data):
    try:
        if data[0] != 0x16: return None
        p = 5 + 4 + 2 + 32; p += 1 + data[p]; p += 2 + struct.unpack("!H", data[p:p+2])[0]; p += 1 + data[p]
        end = p + 2 + struct.unpack("!H", data[p:p+2])[0]; p += 2
        while p < end:
            t, l = struct.unpack("!HH", data[p:p+4]); p += 4
            if t == 0: return data[p+5:p+l].decode()
            p += l
    except Exception: return None
def sink(port):
    L = socket.socket(); L.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); L.bind(("0.0.0.0", port)); L.listen(64)
    while True:
        c, a = L.accept(); c.settimeout(3)
        try: d = c.recv(4096)
        except Exception: d = b""
        host = sni(d) if port == 443 else next((l.split(b":", 1)[1].strip().decode() for l in d.split(b"\r\n") if l.lower().startswith(b"host:")), None)
        lg({"src": f"sink{port}", "host": host, "peer": a[0]}); c.close()
for f, a in ((sniff, ()), (dns, ()), (sink, (80,)), (sink, (443,))): threading.Thread(target=f, args=a, daemon=True).start()
lg({"src": "sniffer", "event": "ready"})
while True: time.sleep(3600)
