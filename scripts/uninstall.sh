#!/usr/bin/env bash
# TELOS SDK uninstaller
#   curl -fsSL https://raw.githubusercontent.com/learningCatHD/telos-sdk/main/scripts/uninstall.sh | bash
#
# Reverses `install.sh`:
#   1. Stops the local gateway daemon          (telos gateway stop)
#   2. Undoes claude-code / codex / openclaw / hermes injections
#                                              (telos uninstall)
#   3. Uninstalls the Python package           (pipx / pip)
#   4. Optionally removes ~/.telos/ state
#
# Env vars:
#   TELOS_PURGE=1       also delete ~/.telos/ (usage.jsonl, gateway.json, ...)
#   TELOS_KEEP_STATE=1  force-keep ~/.telos/ (overrides interactive prompt)
#   TELOS_METHOD        force uninstall backend: pipx | pip-user | pip

set -eu

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YLW=$'\033[0;33m'; CYN=$'\033[0;36m'; DIM=$'\033[2m'; RST=$'\033[0m'
log()  { printf '%s[telos]%s %s\n' "$CYN" "$RST" "$*"; }
warn() { printf '%s[telos]%s %s\n' "$YLW" "$RST" "$*" >&2; }

pick_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
        echo "$c"; return 0
      fi
    fi
  done
  return 1
}

PY=$(pick_python || true)

# Resolve the telos entrypoint without requiring it on PATH.
TELOS_BIN=""
if command -v telos >/dev/null 2>&1; then
  TELOS_BIN=$(command -v telos)
elif [ -n "${PY:-}" ]; then
  USER_BASE=$("$PY" -c 'import site; print(site.getuserbase())' 2>/dev/null || echo "")
  for cand in "$USER_BASE/bin/telos" "$HOME/.local/bin/telos" "$HOME/Library/Python/"*"/bin/telos"; do
    for f in $cand; do
      if [ -x "$f" ]; then TELOS_BIN="$f"; break 2; fi
    done
  done
fi

# 1) stop gateway daemon (best-effort)
if [ -n "$TELOS_BIN" ]; then
  log "stopping gateway daemon"
  "$TELOS_BIN" gateway stop >/dev/null 2>&1 || warn "gateway was not running (or failed to stop)"
else
  warn "'telos' command not found — skipping gateway stop"
fi

# 2) undo harness injections (claude-code / codex / openclaw / hermes)
if [ -n "$TELOS_BIN" ]; then
  log "undoing harness injections"
  "$TELOS_BIN" uninstall || warn "'telos uninstall' returned non-zero; some harnesses may need manual cleanup"
else
  warn "'telos' command not found — skipping harness uninstall"
fi

# 3) uninstall the Python package
detect_method() {
  if [ "${TELOS_METHOD:-}" != "" ]; then echo "$TELOS_METHOD"; return; fi
  if command -v pipx >/dev/null 2>&1 && pipx list --short 2>/dev/null | grep -qx 'telos-sdk'; then
    echo "pipx"; return
  fi
  if [ -n "${PY:-}" ]; then
    # `pip show` exits 0 if installed regardless of --user.
    if "$PY" -m pip show telos-sdk >/dev/null 2>&1; then
      if "$PY" -m pip show -f telos-sdk 2>/dev/null | grep -q "$($PY -c 'import site; print(site.getuserbase())' 2>/dev/null)"; then
        echo "pip-user"; return
      fi
      echo "pip"; return
    fi
  fi
  echo "none"
}

METHOD=$(detect_method)
case "$METHOD" in
  pipx)
    log "removing package via pipx"
    pipx uninstall telos-sdk || warn "pipx uninstall failed"
    ;;
  pip-user)
    log "removing package via pip --user"
    "$PY" -m pip uninstall -y telos-sdk 2>/tmp/.telos-pip.err || {
      if grep -q "externally-managed-environment" /tmp/.telos-pip.err 2>/dev/null; then
        "$PY" -m pip uninstall -y --break-system-packages telos-sdk || warn "pip uninstall failed"
      else
        cat /tmp/.telos-pip.err >&2 || true
        warn "pip uninstall failed"
      fi
    }
    ;;
  pip)
    log "removing package via pip"
    "$PY" -m pip uninstall -y telos-sdk || warn "pip uninstall failed"
    ;;
  none)
    warn "telos-sdk does not appear to be installed by pipx or pip; skipping"
    ;;
esac

# 4) optionally purge ~/.telos/
STATE_DIR="$HOME/.telos"
if [ ! -d "$STATE_DIR" ]; then
  :
elif [ "${TELOS_KEEP_STATE:-0}" = "1" ]; then
  log "keeping ${STATE_DIR} (TELOS_KEEP_STATE=1)"
elif [ "${TELOS_PURGE:-0}" = "1" ]; then
  log "removing ${STATE_DIR}"
  rm -rf -- "$STATE_DIR"
elif [ -t 0 ] && [ -t 1 ]; then
  printf '%s[telos]%s also delete %s (usage.jsonl, gateway.json, config.json)? [y/N] ' "$CYN" "$RST" "$STATE_DIR"
  read -r ans </dev/tty || ans=""
  case "$ans" in
    y|Y|yes|YES) rm -rf -- "$STATE_DIR"; log "removed ${STATE_DIR}" ;;
    *) log "keeping ${STATE_DIR}" ;;
  esac
else
  log "keeping ${STATE_DIR} (non-interactive; set TELOS_PURGE=1 to delete)"
fi

cat <<EOF

  ${GRN}TELOS uninstalled.${RST}
  ${DIM}Reinstall any time:${RST}
    curl -fsSL https://raw.githubusercontent.com/learningCatHD/telos-sdk/main/scripts/install.sh | bash
EOF
