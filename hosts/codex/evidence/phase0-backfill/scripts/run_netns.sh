#!/bin/bash
# Usage: run_netns.sh <python-script> [args]  -- runs the script as uid box inside netns cxbf, with S12 sniffer active.
set -e
BF=/workspace/codex-spike/backfill; NS=cxbf
sudo -n ip netns list | grep -q "^$NS" || sudo -n ip netns add $NS
sudo -n ip netns exec $NS sh -c 'ip link set lo up; ip route replace local default dev lo; ip addr add 10.0.0.2/32 dev lo 2>/dev/null || true'
# host side: proxy.py (logs to backfill/logs/proxy.jsonl) on 18091 and unix bridge
pgrep -f "PROXY_TAG=bf" >/dev/null || (PROXY_PORT=18091 PROXY_LOG=$BF/logs/proxy.jsonl nohup python3 /workspace/codex-spike/scripts/proxy.py PROXY_TAG=bf >$BF/logs/proxy.stderr 2>&1 &)
pgrep -f "bridge.py out" >/dev/null || (nohup python3 $BF/scripts/bridge.py out $BF/run/proxy.sock 18091 >/dev/null 2>&1 &)
# netns side: sniffer (root in netns) and bridge in (as box)
sudo -n ip netns pids $NS | xargs -r ps -o args= -p | grep -q netns_sniffer || (sudo -n ip netns exec $NS nohup python3 $BF/scripts/netns_sniffer.py $BF/logs/s12_netns.jsonl >/dev/null 2>&1 &)
sudo -n ip netns pids $NS | xargs -r ps -o args= -p | grep -q "bridge.py in" || (sudo -n -E ip netns exec $NS setpriv --reuid=$(id -u) --regid=$(id -g) --init-groups nohup python3 $BF/scripts/bridge.py in $BF/run/proxy.sock 18080 >/dev/null 2>&1 &)
sleep 1.5
exec sudo -n -E ip netns exec $NS setpriv --reuid=$(id -u) --regid=$(id -g) --init-groups env HOME=$HOME python3 "$@"
