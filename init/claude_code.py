"""Claude Code (npm global distribution) installer.

Connection method: write ``ANTHROPIC_BASE_URL=<proxy_url>`` into the ``env`` field
of ``~/.claude/settings.json``; Claude Code automatically injects that field into
the process environment on startup. This is equivalent to the user manually
running ``export ANTHROPIC_BASE_URL=...`` before each launch, but it does not
pollute the shell rc and does not rely on a PATH wrapper.

It does not touch the npm package itself; upgrading Claude Code will not lose the
config.

Idempotency:
- Running ``install`` multiple times is equivalent to running it once.
- If a user-defined ``ANTHROPIC_BASE_URL`` already exists, it is moved to
  ``__telos_previous_base_url``, overridden with our value during install, and
  restored on uninstall.
- The full original settings.json is also backed up to ``settings.json.telos.bak``
  (written once on the first install; subsequent installs leave it untouched).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telos.init.base import AgentInstaller, InstallResult


_TELOS_MARK_KEY = "__telos_installed"
_PREVIOUS_KEY = "__telos_previous_base_url"
_BASE_URL_KEY = "ANTHROPIC_BASE_URL"


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

    def install(self) -> InstallResult:
        loaded = _load(self.settings_path)
        env = loaded.data.setdefault("env", {})
        if not isinstance(env, dict):
            raise RuntimeError(
                f"the env field of {self.settings_path} must be an object, but is actually {type(env).__name__}"
            )

        result = InstallResult(agent=self.name, action="install")

        # URL-tag mechanism: the proxy stamps requests with this agent's
        # identity solely from the URL path, no detection guess-work needed.
        # See proxy/server.py handle_tagged_route and docs/harness-routing.md.
        tagged_url = f"{self.proxy_url.rstrip('/')}/_h/claude-code"

        current = env.get(_BASE_URL_KEY)
        already_ours = env.get(_TELOS_MARK_KEY) is True and current == tagged_url
        if already_ours:
            result.already_installed = True
            result.notes.append(f"already connected to the TELOS proxy ({current}); no action")
            return result

        # First patch: back up the original file
        if loaded.existed:
            backup = self.settings_path.with_suffix(self.settings_path.suffix + ".telos.bak")
            if not backup.exists():
                shutil.copy2(self.settings_path, backup)
                result.backups.append(backup)

        # Preserve the user's existing ANTHROPIC_BASE_URL (if any).
        #
        # Key point: deciding "was this value written by us" can only rely on
        # _TELOS_MARK_KEY, not on `current != tagged_url` —— another tool
        # could perfectly well set base_url to the same URL as ours, and in that
        # case, if we do not record it in _PREVIOUS_KEY, uninstall would delete
        # their config along with ours (data loss).
        # Conversely, if current was installed by an earlier TELOS (the mark is
        # already present), it means _PREVIOUS_KEY has long since saved the true
        # original value, and we must not overwrite it with telos's own old value.
        telos_managed = env.get(_TELOS_MARK_KEY) is True
        if current is not None and not telos_managed and _PREVIOUS_KEY not in env:
            env[_PREVIOUS_KEY] = current
            result.notes.append(f"preserved the original ANTHROPIC_BASE_URL ({current}) into {_PREVIOUS_KEY}")

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
        if not self.settings_path.exists():
            result.notes.append(f"{self.settings_path} does not exist; no action")
            return result

        loaded = _load(self.settings_path)
        env = loaded.data.get("env")
        if not isinstance(env, dict) or env.get(_TELOS_MARK_KEY) is not True:
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
        if env.get(_TELOS_MARK_KEY) is True:
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
