"""Recognition layer for `cc-switch <https://github.com/farion1231/cc-switch>`_.

cc-switch is a desktop manager that, on every provider switch, hot-writes the
*same* live agent-config files telos writes (``~/.claude/settings.json`` →
``env.ANTHROPIC_BASE_URL`` + ``ANTHROPIC_AUTH_TOKEN``, ``~/.codex/config.toml``,
``~/.openclaw/openclaw.json``, ``~/.hermes/config.yaml``). The two tools are
*composable*: cc-switch picks **which upstream relay**, telos is a
token-optimizing gateway that sits **in front of any upstream**. The desired
chain is::

    Claude Code ──▶ telos gateway ──▶ cc-switch's chosen relay

telos achieves this by capturing whatever relay is live in each harness's config
into an owned upstream slug and re-pointing the harness at its gateway route (see
each installer's ``install()``). The relay's auth token is never stored — it
rides the request header through the gateway.

This module is the **detection / reporting** half ("识别"): is cc-switch present,
which provider does it have active, and is telos currently chained in front of
each harness. It reads only live files (never cc-switch's SQLite DB), so it stays
immune to cc-switch's internal schema changes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def ccswitch_home() -> Path:
    """The ``~/.cc-switch`` directory (override with ``CC_SWITCH_HOME`` in tests)."""
    env = os.environ.get("CC_SWITCH_HOME")
    return Path(env) if env else Path.home() / ".cc-switch"


def is_installed() -> bool:
    """Whether cc-switch appears installed on this machine.

    Looks for the cc-switch home dir (its mere presence is signal enough; the
    SQLite store added in v3.8.0 and the device settings.json are stronger hints
    but not required).
    """
    return ccswitch_home().is_dir()


@dataclass
class DeviceSettings:
    """The interesting subset of cc-switch's device-level ``settings.json``."""

    current_provider_claude: str | None = None
    current_provider_codex: str | None = None
    raw: dict[str, Any] | None = None


def read_device_settings() -> DeviceSettings:
    """Parse ``~/.cc-switch/settings.json`` for the active provider ids.

    Returns empty fields (never raises) when the file is missing or malformed —
    detection must never block on a peer tool's data.
    """
    path = ccswitch_home() / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DeviceSettings()
    if not isinstance(data, dict):
        return DeviceSettings()
    return DeviceSettings(
        current_provider_claude=data.get("currentProviderClaude"),
        current_provider_codex=data.get("currentProviderCodex"),
        raw=data,
    )


# Per-harness provider-state classification --------------------------------

#: state values returned by :func:`classify_harness`.
TELOS_CHAINED = "telos-chained"     # telos gateway is injected in front
RELAY_ACTIVE = "relay-active"       # a third-party relay (e.g. cc-switch's) is live, telos not chained
OFFICIAL = "official"               # points at the vendor's own API
ABSENT = "absent"                   # harness not configured / no live file


@dataclass
class HarnessState:
    name: str
    state: str
    live_base_url: str | None = None
    note: str = ""


_OFFICIAL_HINTS = ("api.anthropic.com", "api.openai.com", "chatgpt.com")


def _read_claude_base_url() -> str | None:
    path = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    env = data.get("env") if isinstance(data, dict) else None
    if isinstance(env, dict):
        v = env.get("ANTHROPIC_BASE_URL")
        return v if isinstance(v, str) else None
    return None


def classify_harness(name: str, *, proxy_url: str = "http://127.0.0.1:7171") -> HarnessState:
    """Classify a harness's current provider state from its live config.

    Uses the harness's own installer ``status()`` to decide *telos-chained*
    (authoritative), then falls back to reading the live base_url to tell a
    third-party relay apart from the vendor's official endpoint.
    """
    from telos.init import INSTALLERS

    chained = False
    factory = INSTALLERS.get(name)
    if factory is not None:
        try:
            chained = factory(proxy_url=proxy_url).status().already_installed
        except Exception:  # noqa: BLE001 — a broken config shouldn't crash detection
            chained = False

    live = _read_claude_base_url() if name == "claude-code" else None

    if chained:
        return HarnessState(name, TELOS_CHAINED, live, "telos gateway is chained in front")
    if live is None:
        return HarnessState(name, ABSENT, None, "no telos injection detected")
    if any(h in live for h in _OFFICIAL_HINTS):
        return HarnessState(name, OFFICIAL, live, "points at the vendor's official API")
    return HarnessState(name, RELAY_ACTIVE, live,
                        "a third-party relay is live; run `telos ccswitch sync` to chain telos in front")
