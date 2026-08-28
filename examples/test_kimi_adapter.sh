#!/usr/bin/env bash
set -euo pipefail

gateway="${TELOS_GATEWAY_URL:-http://127.0.0.1:7171}"
telos_bin="${TELOS_BIN:-telos}"
kimi_bin="${KIMI_BIN:-$(command -v kimi || true)}"
if [[ -z "$kimi_bin" && -x "$HOME/.kimi-code/bin/kimi" ]]; then
  kimi_bin="$HOME/.kimi-code/bin/kimi"
fi
if [[ -z "$kimi_bin" ]]; then
  echo "error: kimi executable not found; set KIMI_BIN" >&2
  exit 1
fi

marker="TELOS_KIMI_ADAPTER_SMOKE_$(date +%s)"
"$telos_bin" init --harness kimi-code
"$kimi_bin" -p \
  "调用 Shell 工具执行 pwd，然后只回复命令输出。测试标识：$marker" \
  --output-format text

trace_id="$(curl -fsS \
  "$gateway/__telos/api/v1/traces?harness=kimi-code&limit=1" |
  python3 -c 'import json,sys; rows=json.load(sys.stdin)["items"]; assert rows, "no kimi-code trace"; print(rows[0]["id"])')"

curl -fsS "$gateway/__telos/api/v1/traces/$trace_id" |
  python3 -c 'import json,sys
d=json.load(sys.stdin); trace=d["trace"]; spans=d["spans"]
assert trace["harness"] == "kimi-code", trace
assert trace["status"] == "ok", trace
assert any(s["type"] == "tool" and s["status"] == "ok" for s in spans), spans
print(f"PASS: trace={trace['"'"'id'"'"']} tool_spans={sum(s['"'"'type'"'"'] == '"'"'tool'"'"' for s in spans)}")'
