"""Claude Code (npm global distribution) installer.

Connection method: write ``ANTHROPIC_BASE_URL=<proxy_url>`` into the ``env`` field
of ``~/.claude/settings.json``; Claude Code automatically injects that field into
the process environment on startup. This is equivalent to the user manually
running ``export ANTHROPIC_BASE_URL=...`` before each launch, but it does not
pollute the shell rc and does not rely on a PATH wrapper.

It does not touch the npm package itself; upgrading Claude Code will not lose the
config.

When the existing ``ANTHROPIC_BASE_URL`` is a *third-party relay* (e.g. one
written by `cc-switch <https://github.com/farion1231/cc-switch>`_ when the user
picks a provider), telos does not throw that relay away: it registers the relay
as a named upstream (``claude-code-upstream``, ``via="claude-code"``) in
``~/.telos/config.json`` and points ``ANTHROPIC_BASE_URL`` at
``<gateway>/_h/claude-code/upstreams/claude-code-upstream`` so the gateway sits
*in front of* that relay. ``ANTHROPIC_AUTH_TOKEN`` is left untouched — Claude
Code sends it as the request auth header and the gateway forwards it verbatim,
so the token never needs to be stored. When the existing value is the official
``api.anthropic.com`` (or absent), the simple ``/_h/claude-code`` route is used,
which forwards to the ``anthropic`` default upstream.

Idempotency:
- Running ``install`` multiple times is equivalent to running it once.
- If a user-defined ``ANTHROPIC_BASE_URL`` already exists, it is moved to
  ``__telos_previous_base_url``, overridden with our value during install, and
  restored on uninstall.
- The full original settings.json is also backed up to ``settings.json.telos.bak``
  (written once on the first install; subsequent installs leave it untouched).

Robustness against co-managers: a peer tool (cc-switch's "backfill from live"
sync has been observed doing this) can rewrite our boolean ``__telos_installed``
marker as the *string* ``"true"``. Marker checks therefore go through
:func:`_is_marked`, which accepts both, and every install rewrites the marker as
a real boolean so a coerced value self-heals.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telos.config import (
    UpstreamConfig,
    load_config,
    revert_upstreams_owned_by,
    save_config,
)
from telos.init.base import AgentInstaller, InstallResult


_TELOS_MARK_KEY = "__telos_installed"
_PREVIOUS_KEY = "__telos_previous_base_url"
_BASE_URL_KEY = "ANTHROPIC_BASE_URL"

# Stable upstream slug under which a captured third-party relay is registered.
_RELAY_SLUG = "claude-code-upstream"

# A base_url is treated as "official" (no relay to capture) when it points at
# Anthropic's own API. Everything else non-telos is a relay we chain in front of.
_OFFICIAL_HOSTS = ("api.anthropic.com",)


def _is_marked(env: dict[str, Any]) -> bool:
    """Whether ``env`` carries our install marker.

    Accepts a real boolean ``True`` as well as the truthy strings a co-manager
    might coerce it to (``"true"`` / ``"1"``). A literal ``False`` / ``"false"``
    / ``0`` is *not* a marker. See module docstring.
    """
    v = env.get(_TELOS_MARK_KEY)
    if v is True:
        return True
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return False


def _is_telos_route(url: str | None) -> bool:
    """Whether ``url`` is one of our own gateway routes (``/_h/claude-code...``)."""
    return isinstance(url, str) and "/_h/claude-code" in url


def _is_official(url: str | None) -> bool:
    """Whether ``url`` is the official Anthropic endpoint (nothing to capture)."""
    return isinstance(url, str) and any(h in url for h in _OFFICIAL_HOSTS)


def _default_settings_path() -> Path:
    # Consistent with the Claude Code docs: ~/.claude/settings.json
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"


@dataclass
class _LoadedSettings:
    path: Path
    data: dict[str, Any]
    existed: bool


def _load(path: Path) -> _LoadedSettings:
    if not path.exists():
        return _LoadedSettings(path=path, data={}, existed=False)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"{path} is not valid JSON ({e}). Please back it up and fix it manually before running telos init."
        ) from e
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} top level must be a JSON object, but is actually {type(data).__name__}")
    return _LoadedSettings(path=path, data=data, existed=True)


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class ClaudeCodeInstaller(AgentInstaller):
    name = "claude-code"

    def __init__(
        self,
        *,
        proxy_url: str = "http://127.0.0.1:7171",
        settings_path: Path | None = None,
    ) -> None:
        super().__init__(proxy_url=proxy_url)
        self.settings_path = settings_path or _default_settings_path()

    # ------------------------------------------------------------------
    # install
    # ------------------------------------------------------------------

    def _simple_route(self) -> str:
        """The plain tagged route → ``anthropic`` default upstream."""
        return f"{self.proxy_url.rstrip('/')}/_h/claude-code"

    def _relay_route(self) -> str:
        """The tagged upstream route → captured third-party relay slug."""
        return f"{self.proxy_url.rstrip('/')}/_h/claude-code/upstreams/{_RELAY_SLUG}"

    def _ensure_relay_upstream(self, relay_url: str, result: InstallResult) -> None:
        """Register the captured relay under :data:`_RELAY_SLUG` in
        ``~/.telos/config.json`` (idempotent — only writes when missing/stale).

        The relay's auth token is *not* recorded here: Claude Code sends
        ``ANTHROPIC_AUTH_TOKEN`` as the request header and the gateway forwards
        it verbatim, so the secret never lands in telos config.
        """
        try:
            cfg = load_config()
        except RuntimeError as e:
            result.notes.append(
                f"could not read ~/.telos/config.json ({e}); the gateway needs a "
                f"{_RELAY_SLUG!r} upstream → {relay_url} for this provider to work."
            )
            return
        normalized = re.sub(r"/v\d+$", "", relay_url.rstrip("/"))
        desired = UpstreamConfig(
            url=normalized,
            engine="anthropic",
            protocol="anthropic-messages",
            via=self.name,
        )
        if cfg.upstreams.get(_RELAY_SLUG) == desired:
            return
        cfg.upstreams[_RELAY_SLUG] = desired
        path = save_config(cfg)
        result.changed_files.append(path)
        result.notes.append(f"captured provider relay as upstream {_RELAY_SLUG!r} → {normalized}")

    def install(self) -> InstallResult:
        loaded = _load(self.settings_path)
        env = loaded.data.setdefault("env", {})
        if not isinstance(env, dict):
            raise RuntimeError(
                f"the env field of {self.settings_path} must be an object, but is actually {type(env).__name__}"
            )

        result = InstallResult(agent=self.name, action="install")
        current = env.get(_BASE_URL_KEY)

        # Already pointed at one of our gateway routes: nothing to re-chain.
        # Refresh the route for the current proxy_url (host may have changed),
        # self-heal the marker (a co-manager may have coerced it to a string)
        # and, for the relay route, re-ensure the upstream slug from the
        # preserved relay so a wiped ~/.telos/config.json repairs itself.
        if _is_telos_route(current):
            is_relay = "/upstreams/" in current
            target = self._relay_route() if is_relay else self._simple_route()
            changed = False
            # If a foreign tool wrote our exact route URL but left no marker,
            # record it so uninstall restores their value rather than dropping it.
            if not _is_marked(env) and _PREVIOUS_KEY not in env:
                env[_PREVIOUS_KEY] = current
                changed = True
                result.notes.append(f"preserved pre-existing {current} into {_PREVIOUS_KEY}")
            if current != target:
                env[_BASE_URL_KEY] = target
                changed = True
                result.notes.append(f"refreshed ANTHROPIC_BASE_URL → {target}")
            if env.get(_TELOS_MARK_KEY) is not True:
                env[_TELOS_MARK_KEY] = True
                changed = True
                result.notes.append("normalized __telos_installed marker to boolean true")
            if is_relay and isinstance(env.get(_PREVIOUS_KEY), str):
                self._ensure_relay_upstream(env[_PREVIOUS_KEY], result)
            if changed:
                _atomic_write(self.settings_path, loaded.data)
                result.changed_files.append(self.settings_path)
            result.already_installed = not changed
            result.notes.append(f"already connected to the TELOS proxy ({env[_BASE_URL_KEY]})")
            return result

        # First patch: back up the original file
        if loaded.existed:
            backup = self.settings_path.with_suffix(self.settings_path.suffix + ".telos.bak")
            if not backup.exists():
                shutil.copy2(self.settings_path, backup)
                result.backups.append(backup)

        # Preserve the user's existing ANTHROPIC_BASE_URL (relay or official).
        #
        # The decision rests solely on "is current one of our own routes": if it
        # is, it was written by telos and must never be copied into _PREVIOUS_KEY
        # (that would destroy the record of the real upstream — exactly the
        # corruption a co-manager's marker coercion used to trigger). The
        # ``_is_telos_route`` guard above already excluded our routes, so any
        # ``current`` reaching here is a foreign value worth preserving.
        if current is not None and _PREVIOUS_KEY not in env:
            env[_PREVIOUS_KEY] = current
            result.notes.append(f"preserved the original ANTHROPIC_BASE_URL ({current}) into {_PREVIOUS_KEY}")

        # A third-party relay (not official Anthropic, not empty) is captured as
        # a named upstream so the gateway forwards to it; otherwise the simple
        # tagged route is used (→ anthropic default upstream).
        if current and not _is_official(current):
            self._ensure_relay_upstream(current, result)
            tagged_url = self._relay_route()
        else:
            tagged_url = self._simple_route()

        env[_BASE_URL_KEY] = tagged_url
        env[_TELOS_MARK_KEY] = True

        _atomic_write(self.settings_path, loaded.data)
        result.changed_files.append(self.settings_path)
        result.notes.append(
            f"wrote to {self.settings_path}: env.ANTHROPIC_BASE_URL = {tagged_url}"
        )
        return result

    # ------------------------------------------------------------------
    # uninstall
    # ------------------------------------------------------------------

    def uninstall(self) -> InstallResult:
        result = InstallResult(agent=self.name, action="uninstall")

        # Drop any relay upstream slug we own, regardless of settings.json state.
        saved, changes = revert_upstreams_owned_by(self.name)
        if saved is not None:
            result.changed_files.append(saved)
            result.notes.extend(changes)

        if not self.settings_path.exists():
            result.notes.append(f"{self.settings_path} does not exist; no action")
            return result

        loaded = _load(self.settings_path)
        env = loaded.data.get("env")
        # Recognize our managed state by either the marker or one of our routes —
        # a co-manager may have dropped/coerced the marker while our route stands.
        if not isinstance(env, dict) or not (_is_marked(env) or _is_telos_route(env.get(_BASE_URL_KEY))):
            result.notes.append("settings.json has no TELOS marker; no action")
            return result

        env.pop(_TELOS_MARK_KEY, None)
        previous = env.pop(_PREVIOUS_KEY, None)
        if previous is not None:
            env[_BASE_URL_KEY] = previous
            result.notes.append(f"restored ANTHROPIC_BASE_URL = {previous}")
        else:
            env.pop(_BASE_URL_KEY, None)
            result.notes.append("removed ANTHROPIC_BASE_URL")

        if not env:
            loaded.data.pop("env", None)

        _atomic_write(self.settings_path, loaded.data)
        result.changed_files.append(self.settings_path)
        return result

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> InstallResult:
        result = InstallResult(agent=self.name, action="status")
        if not self.settings_path.exists():
            result.notes.append(f"{self.settings_path} does not exist")
            return result
        loaded = _load(self.settings_path)
        env = loaded.data.get("env") or {}
        if _is_marked(env) or _is_telos_route(env.get(_BASE_URL_KEY)):
            result.already_installed = True
            result.notes.append(
                f"connected to the TELOS proxy: {env.get(_BASE_URL_KEY)}"
            )
            if _PREVIOUS_KEY in env:
                result.notes.append(f"the original value will be restored on uninstall: {env[_PREVIOUS_KEY]}")
        elif _BASE_URL_KEY in env:
            result.notes.append(
                f"settings.json has set ANTHROPIC_BASE_URL = {env[_BASE_URL_KEY]} (not injected by TELOS)"
            )
        else:
            result.notes.append("settings.json has not injected ANTHROPIC_BASE_URL")
        return result
