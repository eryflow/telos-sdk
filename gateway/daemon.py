"""gateway daemon management: background start/stop, PID / state files, idempotent.

``proxy.server.run()`` is blocking (aiohttp ``run_app``), so running it in the
background relies on ``subprocess.Popen`` to spawn a ``python -m telos.proxy``
child process, detached from the controlling terminal via ``start_new_session=True``
(a portable ``setsid``).

The state file ``~/.telos/gateway.json`` is the authoritative source
(pid/host/port/mode/…); ``gateway.pid`` is kept only by convention.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from telos.config import TelosConfig, load_config, telos_home

_READY_TIMEOUT_S = 5.0
_STOP_GRACE_S = 5.0


def _pid_file() -> Path:
    return telos_home() / "gateway.pid"


def _state_file() -> Path:
    return telos_home() / "gateway.json"


def _log_file() -> Path:
    return telos_home() / "gateway.log"


@dataclass
class GatewayState:
    pid: int
    host: str
    port: int
    mode: str
    usage_log: str
    started_at: float

    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def dashboard_url(self) -> str:
        return f"{self.base_url()}/dashboard"


# ---------------------------------------------------------------------------
# Process liveness probe
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` liveness check. PID reuse is a known low-probability race (see plan)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just not owned by the current user
    return True


def read_state() -> GatewayState | None:
    """Read the state file; returns ``None`` and cleans up if the file is missing / corrupt / the process is dead."""
    path = _state_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = GatewayState(
            pid=int(data["pid"]),
            host=str(data["host"]),
            port=int(data["port"]),
            mode=str(data.get("mode", "telos")),
            usage_log=str(data.get("usage_log", "")),
            started_at=float(data.get("started_at", 0.0)),
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        _clear_state()
        return None
    if not _pid_alive(state.pid):
        _clear_state()  # stale: process has exited
        return None
    return state


def is_running() -> bool:
    return read_state() is not None


def _write_state(state: GatewayState) -> None:
    home = telos_home()
    home.mkdir(parents=True, exist_ok=True)
    _state_file().write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _pid_file().write_text(f"{state.pid}\n", encoding="utf-8")


def _clear_state() -> None:
    for p in (_state_file(), _pid_file()):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Port readiness probe
# ---------------------------------------------------------------------------

def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_ready(host: str, port: int, deadline: float) -> bool:
    while time.time() < deadline:
        if _port_open(host, port):
            return True
        time.sleep(0.15)
    return False


# ---------------------------------------------------------------------------
# start / stop / restart
# ---------------------------------------------------------------------------

def start_detached(
    *,
    host: str | None = None,
    port: int | None = None,
    mode: str | None = None,
    usage_log: Path | None = None,
    config: TelosConfig | None = None,
) -> GatewayState:
    """Start the gateway in the background (idempotent: if already running, return the current state as-is)."""
    cfg = config or load_config()
    host = host or cfg.gateway.host
    port = port or cfg.gateway.port
    mode = mode or cfg.mode
    usage_log = usage_log or cfg.gateway.resolved_usage_log()

    existing = read_state()
    if existing is not None:
        return existing

    # The port is already taken by another process —— not our gateway, refuse.
    if _port_open(host, port):
        raise RuntimeError(
            f"port {host}:{port} is already in use, but not by a telos-managed gateway. "
            f"Use a different --port, or free that port first."
        )

    home = telos_home()
    home.mkdir(parents=True, exist_ok=True)
    log_fp = open(_log_file(), "a", encoding="utf-8")  # noqa: SIM115 — held by the child process

    cmd = [
        sys.executable, "-m", "telos.proxy",
        "--host", host,
        "--port", str(port),
        "--mode", mode,
        "--usage-log", str(usage_log),
    ]
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=log_fp,
        stderr=log_fp,
        start_new_session=True,  # detach from the controlling terminal, equivalent to setsid
    )
    log_fp.close()

    if not _wait_ready(host, port, time.time() + _READY_TIMEOUT_S):
        # Startup failed: possibly a port conflict / import error; the log is in gateway.log.
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        raise RuntimeError(
            f"gateway did not become ready within {_READY_TIMEOUT_S:.0f}s. See the log: {_log_file()}"
        )

    state = GatewayState(
        pid=proc.pid, host=host, port=port, mode=mode,
        usage_log=str(usage_log), started_at=time.time(),
    )
    _write_state(state)
    return state


def stop() -> bool:
    """Stop the gateway. Returns ``False`` if already stopped, ``True`` if successfully stopped."""
    state = read_state()
    if state is None:
        return False
    try:
        os.kill(state.pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_state()
        return False

    deadline = time.time() + _STOP_GRACE_S
    while time.time() < deadline:
        if not _pid_alive(state.pid):
            break
        time.sleep(0.15)
    else:
        # did not exit within the grace period → force kill
        try:
            os.kill(state.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    _clear_state()
    return True


def restart(
    *,
    host: str | None = None,
    port: int | None = None,
    mode: str | None = None,
    usage_log: Path | None = None,
    config: TelosConfig | None = None,
) -> GatewayState:
    stop()
    # give the OS a moment to release the port
    time.sleep(0.3)
    return start_detached(
        host=host, port=port, mode=mode, usage_log=usage_log, config=config,
    )


def status_text() -> str:
    """A human-readable block of status text."""
    state = read_state()
    if state is None:
        return "gateway: not running (start it with telos gateway start)"
    age = time.time() - state.started_at
    return (
        f"gateway: running\n"
        f"  pid        {state.pid}\n"
        f"  listen     {state.base_url()}\n"
        f"  mode       {state.mode}\n"
        f"  usage log  {state.usage_log or '(none)'}\n"
        f"  uptime     {age:.0f}s\n"
        f"  dashboard  {state.dashboard_url()}"
    )
