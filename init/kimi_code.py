"""Kimi Code installer: add hooks and route API-key providers through TELOS."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from telos.config import (
    UpstreamConfig,
    load_config,
    revert_upstreams_owned_by,
    save_config,
    telos_home,
)
from telos.init.base import AgentInstaller, InstallResult
from telos.kimi_tracing import HOOK_EVENTS


_BEGIN = "# >>> telos managed kimi-code tracing\n"
_END = "# <<< telos managed kimi-code tracing\n"
_HOOK_COMMAND = "telos trace-hook kimi-code"
_RELAY_SLUG = "kimi-code-upstream"
_STATE_VERSION = 1
_TABLE_RE = re.compile(r"(?m)^\s*\[([^\[\]\n]+)\]\s*(?:#.*)?$")
_STRING_RE = r'(?P<quote>["\'])(?P<value>.*?)(?P=quote)'


def _default_config_path() -> Path:
    home = os.environ.get("KIMI_CODE_HOME")
    return (Path(home) if home else Path.home() / ".kimi-code") / "config.toml"


def _default_state_path() -> Path:
    return telos_home() / "installer-state" / "kimi-code.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _strip_managed_hooks(text: str) -> tuple[str, bool]:
    start = text.find(_BEGIN)
    if start < 0:
        return text, False
    end = text.find(_END, start + len(_BEGIN))
    if end < 0:
        raise RuntimeError("Kimi Code config has a TELOS begin marker without its end marker")
    end += len(_END)
    return text[:start] + text[end:], True


def _hook_block() -> str:
    rows = []
    for event in HOOK_EVENTS:
        rows.extend((
            "[[hooks]]",
            f"event = {json.dumps(event)}",
            f"command = {json.dumps(_HOOK_COMMAND)}",
            "timeout = 2",
            "",
        ))
    return _BEGIN + "\n".join(rows) + _END


def _toml_string(text: str, key: str) -> str | None:
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*{_STRING_RE}\s*(?:#.*)?$", text)
    return match.group("value") if match else None


def _table_name(raw: str, prefix: str) -> str | None:
    match = re.fullmatch(
        rf'{re.escape(prefix)}\.(?:"(?P<quoted>[^"\\]*(?:\\.[^"\\]*)*)"|(?P<bare>[A-Za-z0-9_-]+))',
        raw.strip(),
    )
    if not match:
        return None
    quoted = match.group("quoted")
    return bytes(quoted, "utf-8").decode("unicode_escape") if quoted is not None else match.group("bare")


def _table_bounds(text: str, prefix: str, name: str) -> tuple[int, int] | None:
    matches = list(_TABLE_RE.finditer(text))
    for index, match in enumerate(matches):
        if _table_name(match.group(1), prefix) == name:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            return match.end(), end
    return None


def _table_string(text: str, prefix: str, name: str, key: str) -> str | None:
    bounds = _table_bounds(text, prefix, name)
    if bounds is None:
        return None
    return _toml_string(text[bounds[0]:bounds[1]], key)


def _replace_table_string(
    text: str, prefix: str, name: str, key: str, value: str,
) -> str:
    bounds = _table_bounds(text, prefix, name)
    if bounds is None:
        raise RuntimeError(f"Kimi Code config has no [{prefix}.{name!r}] table")
    start, end = bounds
    section = text[start:end]
    pattern = re.compile(
        rf"(?m)^(?P<head>\s*{re.escape(key)}\s*=\s*){_STRING_RE}(?P<tail>\s*(?:#.*)?)$"
    )
    if not pattern.search(section):
        raise RuntimeError(f"Kimi Code [{prefix}.{name!r}] has no string {key!r}")
    replacement = lambda match: match.group("head") + json.dumps(value) + match.group("tail")
    return text[:start] + pattern.sub(replacement, section, count=1) + text[end:]


def _active_provider(text: str) -> tuple[str, str]:
    model = _toml_string(text, "default_model")
    if not model:
        raise RuntimeError("Kimi Code config has no default_model")
    provider = _table_string(text, "models", model, "provider")
    if not provider:
        raise RuntimeError(f"Kimi Code model {model!r} has no provider")
    base_url = _table_string(text, "providers", provider, "base_url")
    if not base_url:
        raise RuntimeError(f"Kimi Code provider {provider!r} has no base_url")
    return provider, base_url


def _looks_like_telos_route(url: str) -> bool:
    return "/_h/kimi-code/upstreams/" in url


class KimiCodeInstaller(AgentInstaller):
    name = "kimi-code"

    def __init__(
        self,
        *,
        proxy_url: str = "http://127.0.0.1:7171",
        config_path: Path | None = None,
        state_path: Path | None = None,
    ) -> None:
        super().__init__(proxy_url=proxy_url)
        self.config_path = config_path or _default_config_path()
        self.state_path = state_path or _default_state_path()

    @property
    def route(self) -> str:
        return f"{self.proxy_url.rstrip('/')}/_h/{self.name}/upstreams/{_RELAY_SLUG}/v1"

    def _read(self) -> str:
        if not self.config_path.exists():
            raise RuntimeError(
                f"{self.config_path} does not exist; start Kimi Code and sign in first"
            )
        return self.config_path.read_text(encoding="utf-8")

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _ensure_upstream(self, original_url: str, result: InstallResult) -> None:
        cfg = load_config()
        normalized = re.sub(r"/v\d+$", "", original_url.rstrip("/"))
        desired = UpstreamConfig(
            url=normalized,
            engine="openai",
            protocol="openai-chat",
            via=self.name,
        )
        if cfg.upstreams.get(_RELAY_SLUG) == desired:
            return
        cfg.upstreams[_RELAY_SLUG] = desired
        result.changed_files.append(save_config(cfg))
        result.notes.append(f"captured active provider as upstream {_RELAY_SLUG!r} → {normalized}")

    def install(self) -> InstallResult:
        result = InstallResult(agent=self.name, action="install")
        original = self._read()
        without_hooks, _ = _strip_managed_hooks(original)
        provider, current_url = _active_provider(without_hooks)
        managed_oauth = provider.startswith("managed:")
        state = self._read_state()
        previous_url = state.get("previous_base_url") if state.get("provider") == provider else None
        if _looks_like_telos_route(current_url):
            if not isinstance(previous_url, str) or not previous_url:
                raise RuntimeError(
                    "Kimi Code already points at a TELOS route but installer state is missing; "
                    "restore the provider base_url before reinstalling"
                )
        else:
            previous_url = current_url

        assert isinstance(previous_url, str)
        self._ensure_upstream(previous_url, result)
        if managed_oauth:
            updated = without_hooks
            if _looks_like_telos_route(current_url):
                updated = _replace_table_string(
                    updated, "providers", provider, "base_url", previous_url
                )
        else:
            updated = _replace_table_string(
                without_hooks, "providers", provider, "base_url", self.route
            )
        updated = updated.rstrip() + "\n\n" + _hook_block()

        if updated != original:
            backup = self.config_path.with_suffix(self.config_path.suffix + ".telos.bak")
            if not backup.exists():
                shutil.copy2(self.config_path, backup)
                result.backups.append(backup)
            _atomic_write(self.config_path, updated)
            result.changed_files.append(self.config_path)

        desired_state = {
            "version": _STATE_VERSION,
            "provider": provider,
            "previous_base_url": previous_url,
            "routed": not managed_oauth,
        }
        if state != desired_state:
            _atomic_write_json(self.state_path, desired_state)
            result.changed_files.append(self.state_path)

        result.already_installed = updated == original and state == desired_state
        if managed_oauth:
            result.notes.append(
                f"preserved OAuth provider {provider!r}; installed {len(HOOK_EVENTS)} "
                "fail-open tracing hooks"
            )
        else:
            result.notes.append(
                f"provider {provider!r} routes through {self.route}; "
                f"installed {len(HOOK_EVENTS)} fail-open tracing hooks"
            )
        return result

    def uninstall(self) -> InstallResult:
        result = InstallResult(agent=self.name, action="uninstall")
        if not self.config_path.exists():
            result.notes.append(f"config does not exist: {self.config_path}")
            return result
        original = self._read()
        updated, removed_hooks = _strip_managed_hooks(original)
        state = self._read_state()
        provider = state.get("provider")
        previous_url = state.get("previous_base_url")
        if isinstance(provider, str) and isinstance(previous_url, str):
            current = _table_string(updated, "providers", provider, "base_url")
            if current and _looks_like_telos_route(current):
                updated = _replace_table_string(
                    updated, "providers", provider, "base_url", previous_url
                )
        if updated != original:
            _atomic_write(self.config_path, updated.rstrip() + "\n")
            result.changed_files.append(self.config_path)
        if self.state_path.exists():
            self.state_path.unlink()
            result.changed_files.append(self.state_path)
        saved, notes = revert_upstreams_owned_by(self.name)
        if saved is not None:
            result.changed_files.append(saved)
        result.notes.extend(notes)
        if not removed_hooks and updated == original:
            result.notes.append("TELOS owns no Kimi Code config block")
        else:
            result.notes.append("removed TELOS hooks and restored any TELOS-routed provider URL")
        return result

    def status(self) -> InstallResult:
        result = InstallResult(agent=self.name, action="status")
        try:
            text = self._read()
            _without, has_hooks = _strip_managed_hooks(text)
            provider, base_url = _active_provider(text)
        except RuntimeError as exc:
            result.notes.append(str(exc))
            return result
        managed_oauth = provider.startswith("managed:")
        route_ok = not _looks_like_telos_route(base_url) if managed_oauth else base_url == self.route
        result.already_installed = has_hooks and route_ok
        if result.already_installed:
            mode = "hooks" if managed_oauth else "gateway + hooks"
            result.notes.append(f"Kimi Code provider {provider!r} is connected via {mode}")
        else:
            result.notes.append(
                f"Kimi Code integration incomplete (provider={provider!r}, "
                f"route={route_ok}, hooks={has_hooks})"
            )
        return result
