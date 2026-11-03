#!/bin/bash
# S4 zero-model-call check: run commands directly under `codex sandbox` with the ro_env permissions profile (Linux only).
BF=/workspace/codex-spike/backfill; C=/workspace/codex-spike/npm/node_modules/.bin/codex
export CODEX_HOME=$BF/home; cd $BF/work/s4
for cmd in "cat .env" "cat readme.txt" "touch shell_write.txt" "echo x > shell_write2.txt"; do
  echo "== -c default_permissions=\"ro_env\": $cmd"; $C sandbox -c 'default_permissions="ro_env"' -- sh -c "$cmd" 2>&1 | sed 's/canary-[a-z0-9-]*/<CANARY-READ>/'; echo "exit=${PIPESTATUS[0]}"
done
echo "== control: -c sandbox_mode=\"read-only\" (no deny rule): cat .env"; $C sandbox -c 'sandbox_mode="read-only"' -- cat .env 2>&1 | sed 's/canary-[a-z0-9-]*/<CANARY-READ>/'; echo "exit=${PIPESTATUS[0]}"
ls -a
