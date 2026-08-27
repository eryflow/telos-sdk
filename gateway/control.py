"""gateway control plane: hot-update a running gateway over localhost HTTP.

Uses only the standard-library ``urllib`` —— no extra dependencies. The control
endpoint listens on loopback only, and the gateway side also accepts only
loopback origins (see ``proxy/server.py``).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

_CONTROL_PATH = "/__telos/control/mode"
_RESET_PATH = "/__telos/control/reset"
_DEVELOPER_PATH = "/__telos/developer.json"
_TRACING_BATCH_PATH = "/__telos/tracing/v1/batch"
_TIMEOUT_S = 3.0


def _control_url(host: str, port: int) -> str:
    return f"http://{host}:{port}{_CONTROL_PATH}"


def dashboard_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/dashboard"


def get_mode(host: str, port: int) -> str:
    """Read the current default mode of a running gateway."""
    req = urllib.request.Request(_control_url(host, port), method="GET")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
        data = json.loads(resp.read().decode("utf-8"))
    return str(data.get("mode", ""))


def get_developer_summary(host: str, port: int) -> dict:
    """Read a running gateway's live session summary (``/__telos/developer.json``).

    Returns the parsed JSON (``session_count`` + per-session ``calls`` / ``harness``
    / ``model``). Raises ``RuntimeError`` on failure so callers can degrade to a
    "no live data" message rather than crash.
    """
    req = urllib.request.Request(
        f"http://{host}:{port}{_DEVELOPER_PATH}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"gateway returned HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot connect to gateway: {e.reason}") from e
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"cannot read gateway developer summary: {e}") from e


def post_mode(host: str, port: int, label: str) -> str:
    """Hot-switch the default mode of a running gateway; return the mode the
    gateway confirmed.

    Raises ``RuntimeError`` on failure (gateway not running / rejected / invalid
    label).
    """
    body = json.dumps({"mode": label}).encode("utf-8")
    req = urllib.request.Request(
        _control_url(host, port), data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"gateway rejected the mode switch (HTTP {e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot connect to gateway: {e.reason}") from e
    return str(data.get("mode", label))


def post_reset(host: str, port: int, *, keep_backup: bool = True) -> dict:
    """Clear a running gateway's usage_log → zero the savings dashboard.

    Returns the gateway's JSON response (``status`` / ``lines_cleared`` /
    ``backup``). Raises ``RuntimeError`` on failure (gateway not running /
    rejected / no usage_log configured).
    """
    body = json.dumps({"keep_backup": keep_backup}).encode("utf-8")
    req = urllib.request.Request(
        f"http://{host}:{port}{_RESET_PATH}", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"gateway rejected the reset (HTTP {e.code}): {detail}") from e
    except TimeoutError as e:
        raise RuntimeError(
            f"gateway did not respond within {_TIMEOUT_S:.0f}s "
            "(it may predate the reset endpoint — try: telos gateway restart)"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot connect to gateway: {e.reason}") from e
    except OSError as e:
        raise RuntimeError(f"cannot connect to gateway: {e}") from e


def post_trace_batch(
    host: str,
    port: int,
    payload: dict,
    *,
    token: str,
) -> dict:
    """Send an idempotent Trace/Span upsert batch to the local gateway."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://{host}:{port}{_TRACING_BATCH_PATH}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"gateway rejected tracing batch (HTTP {e.code}): {detail}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot connect to gateway: {e.reason}") from e
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"cannot send tracing batch: {e}") from e
