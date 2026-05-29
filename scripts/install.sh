#!/usr/bin/env bash
# TELOS SDK installer
#   curl -fsSL https://raw.githubusercontent.com/learningCatHD/telos-sdk/main/scripts/install.sh | bash
#
# Supports: Linux / macOS / WSL2 / Android (Termux)
# Env vars:
#   TELOS_VERSION   pin a specific version (default: latest)
#   TELOS_NO_INIT=1 skip the post-install `telos init`
#   TELOS_METHOD    force install backend: pipx | pip-user | pip

set -eu

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YLW=$'\033[0;33m'; CYN=$'\033[0;36m'; DIM=$'\033[2m'; RST=$'\033[0m'
log()  { printf '%s[telos]%s %s\n' "$CYN" "$RST" "$*"; }
warn() { printf '%s[telos]%s %s\n' "$YLW" "$RST" "$*" >&2; }
die()  { printf '%s[telos]%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

PKG="telos-sdk"
if [ "${TELOS_VERSION:-}" != "" ]; then
  PKG="telos-sdk==${TELOS_VERSION}"
fi

detect_os() {
  local u; u=$(uname -s 2>/dev/null || echo unknown)
  case "$u" in
    Linux*)
      if [ -n "${PREFIX:-}" ] && [ -d "${PREFIX}/bin" ] && command -v termux-info >/dev/null 2>&1; then
        echo "termux"
      elif grep -qi microsoft /proc/version 2>/dev/null; then
        echo "wsl"
      else
        echo "linux"
      fi
      ;;
    Darwin*) echo "macos" ;;
    *) echo "unknown" ;;
  esac
}

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

OS=$(detect_os)
log "platform: ${OS}"

PY=$(pick_python || true)
if [ -z "${PY:-}" ]; then
  case "$OS" in
    macos)  die "Python 3.10+ not found. Install it first: 'brew install python' or https://www.python.org/downloads/" ;;
    linux|wsl) die "Python 3.10+ not found. Install via your package manager (e.g. 'sudo apt install python3 python3-pip python3-venv')." ;;
    termux) die "Python 3.10+ not found. Install with: 'pkg install python'" ;;
    *) die "Python 3.10+ not found." ;;
  esac
fi
log "python: $($PY -c 'import sys; print(sys.executable, sys.version.split()[0])')"

decide_method() {
  if [ "${TELOS_METHOD:-}" != "" ]; then
    echo "$TELOS_METHOD"; return
  fi
  if command -v pipx >/dev/null 2>&1; then echo "pipx"; return; fi
  # Externally-managed environments (PEP 668) reject `pip install` outside a venv.
  # Prefer --user there; if that's also restricted, fall back to pipx auto-install.
  if "$PY" -c 'import sys, sysconfig; p=sysconfig.get_paths(); import os; print(os.path.dirname(p["stdlib"]))' >/dev/null 2>&1; then
    echo "pip-user"; return
  fi
  echo "pip"
}

ensure_pip() {
  if "$PY" -m pip --version >/dev/null 2>&1; then return 0; fi
  warn "pip is missing; bootstrapping with ensurepip"
  "$PY" -m ensurepip --upgrade >/dev/null 2>&1 || \
    die "pip is not available and 'ensurepip' failed. Install pip and re-run."
}

install_with_pipx() {
  log "installing via pipx: $PKG"
  pipx install --force "$PKG"
}

install_with_pip_user() {
  ensure_pip
  log "installing via pip --user: $PKG"
  if ! "$PY" -m pip install --user --upgrade "$PKG" 2>/tmp/.telos-pip.err; then
    if grep -q "externally-managed-environment" /tmp/.telos-pip.err 2>/dev/null; then
      warn "environment is externally managed; retrying with --break-system-packages --user"
      "$PY" -m pip install --user --upgrade --break-system-packages "$PKG"
    else
      cat /tmp/.telos-pip.err >&2 || true
      die "pip install failed"
    fi
  fi
}

install_with_pip() {
  ensure_pip
  log "installing via pip: $PKG"
  "$PY" -m pip install --upgrade "$PKG"
}

METHOD=$(decide_method)
case "$METHOD" in
  pipx)     install_with_pipx ;;
  pip-user) install_with_pip_user ;;
  pip)      install_with_pip ;;
  *) die "unknown TELOS_METHOD: $METHOD" ;;
esac

# Resolve where the `telos` entrypoint landed and warn if it isn't on PATH.
TELOS_BIN=""
if command -v telos >/dev/null 2>&1; then
  TELOS_BIN=$(command -v telos)
else
  USER_BASE=$("$PY" -c 'import site; print(site.getuserbase())' 2>/dev/null || echo "")
  for cand in "$USER_BASE/bin/telos" "$HOME/.local/bin/telos" "$HOME/Library/Python/*/bin/telos"; do
    for f in $cand; do
      if [ -x "$f" ]; then TELOS_BIN="$f"; break 2; fi
    done
  done
fi

# Whether $TELOS_BIN's directory is already on the caller's PATH.
# Note: even if it is, the *currently-running* interactive shell may have a
# stale command hash (zsh/bash cache "telos: not found" once and don't recheck
# until rehash). We always print a "if `telos` still says not found, run …" hint.
BIN_DIR=""
ON_PATH=0
if [ -n "$TELOS_BIN" ]; then
  BIN_DIR=$(dirname "$TELOS_BIN")
  case ":$PATH:" in
    *":$BIN_DIR:"*) ON_PATH=1 ;;
  esac
fi

if [ -z "$TELOS_BIN" ]; then
  warn "installed, but the 'telos' command isn't on PATH yet. Try opening a new shell."
elif [ "$ON_PATH" -eq 0 ]; then
  warn "installed at $TELOS_BIN — but $BIN_DIR is not on PATH. Add this to your shell rc:"
  printf '    %sexport PATH="%s:$PATH"%s\n' "$DIM" "$BIN_DIR" "$RST" >&2
fi

log "${GRN}installed${RST} $("${TELOS_BIN:-telos}" --version 2>/dev/null || echo "$PKG")"

if [ -n "$TELOS_BIN" ] && [ "$ON_PATH" -eq 1 ]; then
  log "${DIM}if your current shell still says 'telos: command not found', run:${RST} hash -r   ${DIM}(zsh/bash) or open a new terminal${RST}"
fi

if [ "${TELOS_NO_INIT:-0}" = "1" ]; then
  log "skipping 'telos init' (TELOS_NO_INIT=1)"
else
  if [ -t 0 ] && [ -t 1 ]; then
    log "running 'telos init' to auto-connect detected harnesses..."
    "${TELOS_BIN:-telos}" init || warn "'telos init' returned non-zero; you can re-run it manually."
  else
    log "non-interactive shell; skipping 'telos init'. Run it yourself:"
    printf '    %stelos init%s\n' "$DIM" "$RST"
  fi
fi

cat <<EOF

  ${GRN}Next${RST}
    telos init         ${DIM}# auto-connect claude-code / codex / openclaw / hermes${RST}
    telos dashboard    ${DIM}# open the local savings dashboard${RST}

  Docs: https://github.com/learningCatHD/telos-sdk
EOF
