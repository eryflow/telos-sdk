"""Codex installer.

Codex supports custom model providers in ``~/.codex/config.toml``. TELOS uses
that hook to add a provider that points at the local gateway's OpenAI upstream
route. The current gateway optimizes OpenAI ChatCompletions traffic; Codex's
default Responses API traffic is passed through so direct ``codex`` launches can
still be observed and routed consistently.

Codex has two authentication modes (recorded in ``~/.codex/auth.json``):

- ``apikey``  — uses ``OPENAI_API_KEY``; requests go to ``api.openai.com/v1/responses``.
- ``chatgpt`` — uses a ChatGPT JWT; requests must go to
  ``chatgpt.com/backend-api/codex/responses``. ``api.openai.com`` rejects this
  JWT (403 "Missing scopes"), so a proxy that blindly forwards to api.openai.com
  breaks ``codex`` for Codex.app users.

The installer reads ``auth.json`` and picks the matching upstream slug:

- ``apikey``  → ``base_url = <proxy>/upstreams/openai/v1`` (existing default).
- ``chatgpt`` → ``base_url = <proxy>/upstreams/codex-chatgpt`` (no ``/v1``),
  and registers a ``codex-chatgpt`` upstream pointing to
  ``https://chatgpt.com/backend-api/codex`` in ``~/.telos/config.json``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from telos.config import (
    UpstreamConfig,
    load_config,
    revert_upstreams_owned_by,
    save_config,
    telos_home,
)
from telos.codex_tracing import HOOK_EVENTS
from telos.init.base import AgentInstaller, InstallResult

_ROOT_BEGIN = "# >>> telos managed codex root\n"
_ROOT_END = "# <<< telos managed codex root\n"
_PROVIDER_BEGIN = "# >>> telos managed codex provider\n"
_PROVIDER_END = "# <<< telos managed codex provider\n"
_PREV_PREFIX = "# telos_previous_model_provider = "
_ABSENT = "<absent>"

_CHATGPT_SLUG = "codex-chatgpt"
_CHATGPT_UPSTREAM_URL = "https://chatgpt.com/backend-api/codex"

# Slug under which a custom (e.g. cc-switch-written) Codex provider relay is
# captured so the gateway forwards to it instead of api.openai.com.
_CODEX_RELAY_SLUG = "codex-upstream"
_OFFICIAL_OPENAI_HINTS = ("api.openai.com", "chatgpt.com")

_PROVIDER_NAME_RE = re.compile(r'model_provider\s*=\s*"([^"]+)"')

_TRACE_PLUGIN_VERSION = "1"
_TRACE_PLUGIN_NAME = "telos-tracing"
_TRACE_MARKETPLACE_NAME = "telos-local"
_TRACE_HOOK_COMMAND = "telos trace-hook codex"
_FALLBACK_DESCRIPTION = "TELOS Codex tracing compatibility hooks."


def _trace_hook_handler(event: str) -> dict[str, object]:
    return {
        "type": "command",
        "command": _TRACE_HOOK_COMMAND,
        "timeout": 2,
        "async": event not in {"UserPromptSubmit", "SessionEnd", "Interrupt"},
    }


def _trace_hook_events() -> dict[str, list[dict[str, object]]]:
    return {
        event: [{"hooks": [_trace_hook_handler(event)]}]
        for event in HOOK_EVENTS
    }


def _trace_plugin_files() -> dict[Path, str]:
    manifest = {
        "name": _TRACE_PLUGIN_NAME,
        "version": _TRACE_PLUGIN_VERSION,
        "description": "Local TELOS Agent Trace capture for Codex",
        "hooks": "./hooks/hooks.json",
    }
    hook_file = {
        "description": "TELOS tracing hooks; observation only and fail-open.",
        "hooks": _trace_hook_events(),
    }
    return {
        Path(".codex-plugin/plugin.json"): json.dumps(
            manifest, ensure_ascii=False, indent=2
        ) + "\n",
        Path("hooks/hooks.json"): json.dumps(
            hook_file, ensure_ascii=False, indent=2
        ) + "\n",
    }


def _trace_marketplace_manifest(plugin_path: Path) -> str:
    return json.dumps({
        "name": _TRACE_MARKETPLACE_NAME,
        "plugins": [{
            "name": _TRACE_PLUGIN_NAME,
            "source": {"source": "local", "path": f"./{plugin_path.name}"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Engineering",
        }],
    }, ensure_ascii=False, indent=2) + "\n"


def _write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except FileNotFoundError:
        pass
    _atomic_write(path, text)
    return True


def _is_telos_hook_handler(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") == "command"
        and value.get("command") == _TRACE_HOOK_COMMAND
    )


def _merge_trace_hooks(data: object) -> tuple[dict[str, object], bool]:
    """Add one TELOS handler per event without changing existing handlers."""
    if not isinstance(data, dict):
        raise ValueError("hooks.json root must be an object")
    merged: dict[str, object] = json.loads(json.dumps(data))
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json 'hooks' must be an object")
    changed = False
    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"hooks.json event {event!r} must be an array")
        found = False
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"hooks.json event {event!r} contains an invalid matcher group")
            if any(_is_telos_hook_handler(handler) for handler in group["hooks"]):
                found = True
        if not found:
            groups.append({"hooks": [_trace_hook_handler(event)]})
            changed = True
    return merged, changed


def _remove_trace_hooks(data: object) -> tuple[dict[str, object], bool]:
    """Remove only exact TELOS command handlers; preserve every other value."""
    if not isinstance(data, dict):
        raise ValueError("hooks.json root must be an object")
    cleaned: dict[str, object] = json.loads(json.dumps(data))
    hooks = cleaned.get("hooks")
    if not isinstance(hooks, dict):
        return cleaned, False
    changed = False
    for event in HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        remaining_groups: list[object] = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                remaining_groups.append(group)
                continue
            remaining_handlers = [
                handler for handler in group["hooks"]
                if not _is_telos_hook_handler(handler)
            ]
            if len(remaining_handlers) == len(group["hooks"]):
                remaining_groups.append(group)
                continue
            changed = True
            if remaining_handlers or set(group) != {"hooks"}:
                group["hooks"] = remaining_handlers
                remaining_groups.append(group)
        if remaining_groups:
            hooks[event] = remaining_groups
        else:
            hooks.pop(event, None)
    return cleaned, changed


def _extract_provider_base_url(text: str, provider: str) -> str | None:
    """Best-effort: read ``base_url`` from a ``[model_providers.<provider>]``
    table in raw config.toml text, without a TOML parser (Python 3.10 has none
    in stdlib). Returns ``None`` if the table or its base_url is absent.
    """
    if not provider:
        return None
    lines = text.splitlines()
    headers = (f"[model_providers.{provider}]", f'[model_providers."{provider}"]')
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped in headers:
            in_table = True
            continue
        if in_table:
            if stripped.startswith("["):  # next table → stop
                break
            m = re.match(r'base_url\s*=\s*"([^"]+)"', stripped)
            if m:
                return m.group(1)
    return None



def _default_config_path() -> Path:
    return _codex_home() / "config.toml"


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def _detect_auth_mode(auth_json_path: Path) -> str:
    """Return ``"chatgpt"`` / ``"apikey"`` / ``"unknown"`` based on auth.json.

    Missing file or unreadable JSON → ``"unknown"``; we then fall back to API
    key wiring (the safe default that matches the pre-1.0 installer behavior).
    """
    if not auth_json_path.exists():
        return "unknown"
    try:
        data = json.loads(auth_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown"
    mode = str(data.get("auth_mode") or "").strip().lower()
    if mode in ("chatgpt", "apikey"):
        return mode
    # Older codex builds don't set auth_mode explicitly; infer from the fields.
    if data.get("OPENAI_API_KEY"):
        return "apikey"
    if isinstance(data.get("tokens"), dict):
        return "chatgpt"
    return "unknown"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class _PreparedConfig:
    text: str
    previous_model_provider: str | None


def _find_marker(text: str, marker: str, *, start: int = 0) -> int:
    """Find a marker line; tolerant of a missing trailing newline at EOF.

    Codex itself rewrites ``config.toml`` periodically (project trust,
    settings UI, plugin install) and at least one of those code paths drops
    the final ``\\n``. The original markers all end in ``\\n``; without this
    fallback, ``text.find(marker)`` then returns -1 and the installer raises
    ``"found a TELOS managed Codex block without its end marker"``, which
    silently no-ops the codex install for the rest of the session.
    """
    idx = text.find(marker, start)
    if idx >= 0:
        return idx
    bare = marker.rstrip("\n")
    idx = text.find(bare, start)
    if idx >= 0 and idx + len(bare) == len(text):
        return idx
    return -1


def _strip_block(text: str, begin: str, end: str) -> tuple[str, str]:
    """Remove a managed block; return ``(remaining_text, block_inner_text)``.

    ``block_inner_text`` is the text **between** the begin and end markers
    (exclusive), so callers can recover any foreign lines that an external
    rewriter accidentally wedged inside the managed region.
    """
    start = _find_marker(text, begin)
    if start < 0:
        return text, ""
    inner_start = start + len(begin)
    stop = _find_marker(text, end, start=inner_start)
    if stop < 0:
        raise RuntimeError("found a TELOS managed Codex block without its end marker")
    inner = text[inner_start:stop]
    # consume the actual length of the end marker we matched (may be missing \n at EOF)
    end_len = len(end) if text[stop:stop + len(end)] == end else len(end.rstrip("\n"))
    stop += end_len
    if stop < len(text) and text[stop] == "\n":
        stop += 1
    return text[:start] + text[stop:], inner


def _strip_provider_table_from(inside: str) -> str:
    """Remove our own ``[model_providers.telos]`` table from a block's inner
    text. Returns whatever foreign content was left in the block.

    A TOML table runs from its ``[header]`` line to the next bracketed header
    or to end-of-input — that's the unit we strip. Everything before the
    table header and everything from the next ``[header]`` onward is foreign
    and must be preserved (codex sometimes reorders unrelated tables into
    our managed region; we don't want to delete the user's projects, mcp
    servers, or shell-env policy by accident).
    """
    lines = inside.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip() == "[model_providers.telos]":
            i += 1
            while i < n:
                stripped = lines[i].lstrip()
                # next table header (single [ or double [[) ends ours
                if stripped.startswith("[") and not stripped.startswith("#"):
                    break
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "".join(out)


def _remove_top_level_model_provider(text: str) -> _PreparedConfig:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    previous: str | None = None
    in_top_level = True
    for line in lines:
        stripped = line.strip()
        if in_top_level and stripped.startswith("["):
            in_top_level = False
        if (
            in_top_level
            and previous is None
            and stripped.startswith("model_provider")
            and "=" in stripped
            and not stripped.startswith("#")
        ):
            previous = line.rstrip("\n")
            continue
        out.append(line)
    return _PreparedConfig(text="".join(out), previous_model_provider=previous)


def _extract_previous_model_provider(root_block: str) -> str | None:
    for line in root_block.splitlines():
        if line.startswith(_PREV_PREFIX):
            value = line[len(_PREV_PREFIX):].strip()
            return None if value == _ABSENT else value
    return None


def _extract_block(text: str, begin: str, end: str) -> str | None:
    start = _find_marker(text, begin)
    if start < 0:
        return None
    stop = _find_marker(text, end, start=start + len(begin))
    if stop < 0:
        raise RuntimeError("found a TELOS managed Codex block without its end marker")
    end_len = len(end) if text[stop:stop + len(end)] == end else len(end.rstrip("\n"))
    return text[start:stop + end_len]


class CodexInstaller(AgentInstaller):
    name = "codex"

    def __init__(
        self,
        *,
        proxy_url: str = "http://127.0.0.1:7171",
        config_path: Path | None = None,
        auth_json_path: Path | None = None,
        trace_plugin_path: Path | None = None,
        hooks_path: Path | None = None,
        codex_executable: str = "codex",
        register_trace_plugin: bool | None = None,
    ) -> None:
        super().__init__(proxy_url=proxy_url)
        using_default_config = config_path is None
        self.config_path = config_path or _default_config_path()
        # auth.json lives next to config.toml; tests pass a tmp path so they
        # don't depend on the developer's real Codex login.
        self.auth_json_path = (
            auth_json_path if auth_json_path is not None
            else self.config_path.parent / "auth.json"
        )
        self.trace_plugin_path = trace_plugin_path or (
            telos_home() / "integrations/codex/telos-tracing"
            if using_default_config
            else self.config_path.parent / ".telos-tracing-plugin"
        )
        self.hooks_path = hooks_path or self.config_path.parent / "hooks.json"
        self.codex_executable = codex_executable
        self.register_trace_plugin = (
            using_default_config
            if register_trace_plugin is None
            else register_trace_plugin
        )
        self.trace_marketplace_path = self.trace_plugin_path.parent
        self.trace_marketplace_manifest = (
            self.trace_marketplace_path / ".agents/plugins/marketplace.json"
        )

    def _run_codex_json(self, arguments: list[str]) -> dict[str, object]:
        try:
            completed = subprocess.run(
                [self.codex_executable, *arguments],
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Codex plugin command failed: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
            raise RuntimeError(f"Codex plugin command failed: {detail}")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Codex plugin command returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Codex plugin command returned a non-object")
        return value

    def _register_trace_plugin(self, result: InstallResult) -> bool:
        if not self.register_trace_plugin:
            return False
        marketplace_text = _trace_marketplace_manifest(self.trace_plugin_path)
        if _write_if_changed(self.trace_marketplace_manifest, marketplace_text):
            result.changed_files.append(self.trace_marketplace_manifest)
        try:
            marketplaces = self._run_codex_json([
                "plugin", "marketplace", "list", "--json",
            ]).get("marketplaces", [])
            own = next(
                (item for item in marketplaces
                 if isinstance(item, dict) and item.get("name") == _TRACE_MARKETPLACE_NAME),
                None,
            )
            root = str(self.trace_marketplace_path.resolve())
            if own is not None and str(own.get("root")) != root:
                raise RuntimeError(
                    f"Codex marketplace {_TRACE_MARKETPLACE_NAME!r} already points to "
                    f"{own.get('root')!r}"
                )
            if own is None:
                self._run_codex_json([
                    "plugin", "marketplace", "add", root, "--json",
                ])

            installed = self._run_codex_json([
                "plugin", "list", "--json",
            ]).get("installed", [])
            plugin_id = f"{_TRACE_PLUGIN_NAME}@{_TRACE_MARKETPLACE_NAME}"
            if not any(
                isinstance(item, dict)
                and item.get("pluginId") == plugin_id
                and item.get("enabled") is True
                for item in installed
            ):
                self._run_codex_json([
                    "plugin", "add", plugin_id, "--json",
                ])
            verified = self._run_codex_json([
                "plugin", "list", "--json",
            ]).get("installed", [])
            if not any(
                isinstance(item, dict)
                and item.get("pluginId") == plugin_id
                and item.get("enabled") is True
                for item in verified
            ):
                raise RuntimeError("Codex did not report the TELOS plugin as enabled")
        except RuntimeError as error:
            result.notes.append(
                f"native Codex plugin registration unavailable ({error}); using hooks.json compatibility mode"
            )
            return False
        result.notes.append(
            f"registered and enabled Codex plugin {plugin_id}"
        )
        return True

    def _install_trace_plugin(self, result: InstallResult) -> bool:
        changed = False
        for relative_path, text in _trace_plugin_files().items():
            path = self.trace_plugin_path / relative_path
            if _write_if_changed(path, text):
                result.changed_files.append(path)
                changed = True
        result.notes.append(
            f"prepared Codex tracing plugin bundle v{_TRACE_PLUGIN_VERSION} at "
            f"{self.trace_plugin_path}"
        )
        return changed

    def _install_hook_fallback(self, result: InstallResult) -> bool:
        existed = self.hooks_path.exists()
        try:
            data: object = (
                json.loads(self.hooks_path.read_text(encoding="utf-8"))
                if existed
                else {"description": _FALLBACK_DESCRIPTION, "hooks": {}}
            )
            merged, changed = _merge_trace_hooks(data)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            result.notes.append(
                f"tracing compatibility fallback not enabled: {self.hooks_path}: {error}"
            )
            return False
        if changed:
            if existed:
                backup = self.hooks_path.with_suffix(self.hooks_path.suffix + ".telos.bak")
                if not backup.exists():
                    shutil.copy2(self.hooks_path, backup)
                    result.backups.append(backup)
            _atomic_write(
                self.hooks_path,
                json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            )
            result.changed_files.append(self.hooks_path)
        result.notes.append(
            f"tracing compatibility fallback enabled in {self.hooks_path}; "
            "existing hooks were preserved"
        )
        return changed

    def _uninstall_trace_plugin(self, result: InstallResult) -> None:
        if self.register_trace_plugin:
            plugin_id = f"{_TRACE_PLUGIN_NAME}@{_TRACE_MARKETPLACE_NAME}"
            try:
                installed = self._run_codex_json([
                    "plugin", "list", "--json",
                ]).get("installed", [])
                if any(
                    isinstance(item, dict) and item.get("pluginId") == plugin_id
                    for item in installed
                ):
                    self._run_codex_json([
                        "plugin", "remove", plugin_id, "--json",
                    ])
                marketplaces = self._run_codex_json([
                    "plugin", "marketplace", "list", "--json",
                ]).get("marketplaces", [])
                if any(
                    isinstance(item, dict)
                    and item.get("name") == _TRACE_MARKETPLACE_NAME
                    and str(item.get("root")) == str(self.trace_marketplace_path.resolve())
                    for item in marketplaces
                ):
                    self._run_codex_json([
                        "plugin", "marketplace", "remove",
                        _TRACE_MARKETPLACE_NAME, "--json",
                    ])
            except RuntimeError as error:
                result.notes.append(f"could not unregister native Codex plugin: {error}")
        for relative_path in _trace_plugin_files():
            path = self.trace_plugin_path / relative_path
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            result.changed_files.append(path)
        # Remove only directories that became empty; foreign files survive.
        for path in (
            self.trace_plugin_path / ".codex-plugin",
            self.trace_plugin_path / "hooks",
            self.trace_plugin_path,
        ):
            try:
                path.rmdir()
            except (FileNotFoundError, OSError):
                pass
        try:
            if self.trace_marketplace_manifest.read_text(encoding="utf-8") == _trace_marketplace_manifest(self.trace_plugin_path):
                self.trace_marketplace_manifest.unlink()
                result.changed_files.append(self.trace_marketplace_manifest)
        except FileNotFoundError:
            pass
        for path in (
            self.trace_marketplace_manifest.parent,
            self.trace_marketplace_manifest.parent.parent,
        ):
            try:
                path.rmdir()
            except (FileNotFoundError, OSError):
                pass

    def _uninstall_hook_fallback(self, result: InstallResult) -> None:
        if not self.hooks_path.exists():
            return
        try:
            original = json.loads(self.hooks_path.read_text(encoding="utf-8"))
            cleaned, changed = _remove_trace_hooks(original)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            result.notes.append(
                f"could not remove tracing fallback from {self.hooks_path}: {error}"
            )
            return
        if not changed:
            return
        hooks = cleaned.get("hooks")
        only_telos_scaffold = (
            cleaned.get("description") == _FALLBACK_DESCRIPTION
            and isinstance(hooks, dict)
            and not hooks
            and set(cleaned) == {"description", "hooks"}
        )
        if only_telos_scaffold:
            self.hooks_path.unlink()
        else:
            _atomic_write(
                self.hooks_path,
                json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n",
            )
        result.changed_files.append(self.hooks_path)

    def _provider_url(self, auth_mode: str) -> str:
        """Pick the gateway slug + path suffix per Codex auth mode.

        - ``chatgpt``: codex must talk to ``chatgpt.com/backend-api/codex/responses``;
          the chatgpt path has no ``/v1`` prefix, so the codex ``base_url`` is
          ``<gateway>/upstreams/codex-chatgpt`` and codex appends ``/responses``.
        - ``apikey`` / ``unknown``: classic OpenAI Responses API on
          ``api.openai.com/v1/responses``; ``base_url = <gateway>/upstreams/openai/v1``.
        """
        proxy = self.proxy_url.rstrip("/")
        if auth_mode == "chatgpt":
            return f"{proxy}/_h/codex/upstreams/{_CHATGPT_SLUG}"
        return f"{proxy}/_h/codex/upstreams/openai/v1"

    def _ensure_chatgpt_upstream(self, result: InstallResult) -> None:
        """Register the ``codex-chatgpt`` slug in ``~/.telos/config.json`` so the
        gateway forwards ``/upstreams/codex-chatgpt/responses`` →
        ``chatgpt.com/backend-api/codex/responses``. Idempotent — only writes
        when the entry is missing or stale.
        """
        try:
            cfg = load_config()
        except RuntimeError as e:
            result.notes.append(
                f"could not read ~/.telos/config.json ({e}); the gateway must "
                f"have a {_CHATGPT_SLUG!r} upstream pointing to "
                f"{_CHATGPT_UPSTREAM_URL} for Codex chat-gpt auth to work."
            )
            return
        desired = UpstreamConfig(
            url=_CHATGPT_UPSTREAM_URL,
            engine="openai",
            protocol="openai-chat",
            via="codex",
        )
        if cfg.upstreams.get(_CHATGPT_SLUG) == desired:
            return
        cfg.upstreams[_CHATGPT_SLUG] = desired
        path = save_config(cfg)
        result.changed_files.append(path)
        result.notes.append(
            f"registered telos upstream {_CHATGPT_SLUG!r} → {_CHATGPT_UPSTREAM_URL}"
        )

    def _ensure_codex_upstream(self, relay_url: str, result: InstallResult) -> None:
        """Capture a custom Codex provider relay (e.g. one cc-switch wrote) under
        :data:`_CODEX_RELAY_SLUG` so the gateway forwards to it instead of
        api.openai.com. Idempotent. The relay's API key is not stored — it rides
        the request header through the gateway.
        """
        try:
            cfg = load_config()
        except RuntimeError as e:
            result.notes.append(
                f"could not read ~/.telos/config.json ({e}); the gateway needs a "
                f"{_CODEX_RELAY_SLUG!r} upstream → {relay_url} for this provider."
            )
            return
        normalized = re.sub(r"/v\d+$", "", relay_url.rstrip("/"))
        desired = UpstreamConfig(
            url=normalized,
            engine="openai",
            protocol="openai-chat",
            via=self.name,
        )
        if cfg.upstreams.get(_CODEX_RELAY_SLUG) == desired:
            return
        cfg.upstreams[_CODEX_RELAY_SLUG] = desired
        path = save_config(cfg)
        result.changed_files.append(path)
        result.notes.append(f"captured Codex provider relay as upstream {_CODEX_RELAY_SLUG!r} → {normalized}")

    def install(self) -> InstallResult:
        auth_mode = _detect_auth_mode(self.auth_json_path)
        original = (
            self.config_path.read_text(encoding="utf-8")
            if self.config_path.exists()
            else ""
        )
        root_block = _extract_block(original, _ROOT_BEGIN, _ROOT_END)
        previous = (
            _extract_previous_model_provider(root_block)
            if root_block is not None
            else None
        )
        text, root_inner = _strip_block(original, _ROOT_BEGIN, _ROOT_END)
        text, provider_inner = _strip_block(text, _PROVIDER_BEGIN, _PROVIDER_END)
        # Any foreign content codex wedged inside our provider block (e.g. a
        # `notify =` array or a `[projects."…"]` table moved by codex's own
        # config rewriter) survives as orphaned text we re-attach below.
        provider_orphans = _strip_provider_table_from(provider_inner)
        # The root block should only ever contain the previous-marker comment
        # plus `model_provider = "telos"`. Anything else is foreign and worth
        # preserving for the same reason.
        root_orphans = "".join(
            line for line in root_inner.splitlines(keepends=True)
            if not line.startswith(_PREV_PREFIX)
            and line.strip() not in ("", 'model_provider = "telos"')
        )

        # Re-inject orphans into the outer text so they survive the rewrite.
        orphan_text = root_orphans + provider_orphans
        if orphan_text:
            if text and not text.startswith("\n"):
                text = orphan_text + ("\n" if not orphan_text.endswith("\n") else "") + text
            else:
                text = orphan_text + text

        prepared = _remove_top_level_model_provider(text)
        if root_block is None:
            previous = prepared.previous_model_provider
        text = prepared.text

        # In API-key mode, if the previous provider had a custom (non-OpenAI)
        # base_url — e.g. a relay cc-switch wrote — capture it so the gateway
        # forwards there instead of api.openai.com. Best-effort: any parse miss
        # falls back to the fixed openai upstream (no regression).
        relay_url: str | None = None
        if auth_mode != "chatgpt" and previous:
            m = _PROVIDER_NAME_RE.search(previous)
            if m:
                candidate = _extract_provider_base_url(original, m.group(1))
                if candidate and not any(h in candidate for h in _OFFICIAL_OPENAI_HINTS):
                    relay_url = candidate

        if auth_mode == "chatgpt":
            provider_url = self._provider_url(auth_mode)
        elif relay_url is not None:
            provider_url = f"{self.proxy_url.rstrip('/')}/_h/codex/upstreams/{_CODEX_RELAY_SLUG}/v1"
        else:
            provider_url = self._provider_url(auth_mode)
        prev_marker = previous if previous is not None else _ABSENT
        root = (
            _ROOT_BEGIN +
            f"{_PREV_PREFIX}{prev_marker}\n"
            'model_provider = "telos"\n' +
            _ROOT_END
        )
        provider = (
            _PROVIDER_BEGIN +
            "[model_providers.telos]\n"
            'name = "TELOS Gateway"\n'
            f'base_url = "{provider_url}"\n'
            'wire_api = "responses"\n'
            "requires_openai_auth = true\n" +
            _PROVIDER_END
        )
        new_text = root + ("\n" if text and not text.startswith("\n") else "") + text
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += "\n" + provider
        if not new_text.endswith("\n"):
            new_text += "\n"

        result = InstallResult(agent=self.name, action="install")
        tracing_changed = self._install_trace_plugin(result)
        native_plugin = self._register_trace_plugin(result)
        if native_plugin:
            self._uninstall_hook_fallback(result)
        else:
            tracing_changed |= self._install_hook_fallback(result)
        # For chatgpt mode, the gateway also needs the matching upstream slug.
        # Do this even if config.toml is already current, so a fresh `telos
        # init codex` on a machine that lost ~/.telos/config.json self-heals.
        if auth_mode == "chatgpt":
            self._ensure_chatgpt_upstream(result)
        elif relay_url is not None:
            self._ensure_codex_upstream(relay_url, result)

        if orphan_text:
            result.notes.append(
                "recovered foreign content from inside the TELOS managed region "
                "(likely moved there by codex's own config rewriter); "
                "re-emitting it outside the managed block."
            )

        if new_text == original:
            result.already_installed = not tracing_changed
            result.notes.insert(
                0,
                f"already connected to the TELOS gateway ({provider_url}); no action"
            )
            return result

        if self.config_path.exists():
            backup = self.config_path.with_suffix(self.config_path.suffix + ".telos.bak")
            if not backup.exists():
                shutil.copy2(self.config_path, backup)
                result.backups.append(backup)

        _atomic_write(self.config_path, new_text)
        result.changed_files.append(self.config_path)
        result.notes.append(
            f"wrote Codex model_provider=telos with base_url={provider_url}"
        )
        if auth_mode == "chatgpt":
            result.notes.append(
                "detected ChatGPT login (auth.json auth_mode=chatgpt); "
                "routing through chatgpt.com/backend-api/codex."
            )
            result.notes.append(
                "if your ChatGPT token later expires (Codex shows a "
                "'token_invalidated' / 'sign in again' error), the re-login "
                "MUST be done with the telos provider removed — the "
                "'Sign in with ChatGPT' bootstrap probes non-API paths that, "
                "routed through the gateway, return an HTML page and crash "
                "Codex with \"Unexpected token '<', \"<!DOCTYPE\"...\". Workflow: "
                "`telos uninstall --harness codex` → sign in → `telos init codex`."
            )
        elif auth_mode == "unknown":
            result.notes.append(
                "could not detect Codex auth mode (auth.json missing or unreadable); "
                "assuming OPENAI_API_KEY. If you're on Codex.app with a ChatGPT "
                "login, run `codex login` first, then re-run `telos init codex`."
            )
        result.notes.append(
            "Codex uses the Responses API by default; the gateway currently passes that path through."
        )
        return result

    def uninstall(self) -> InstallResult:
        result = InstallResult(agent=self.name, action="uninstall")
        self._uninstall_hook_fallback(result)
        self._uninstall_trace_plugin(result)
        # Always try to undo telos-side upstream registrations first, so a
        # half-finished previous install (config.toml missing but
        # ~/.telos/config.json polluted) still gets cleaned up.
        saved, changes = revert_upstreams_owned_by(self.name)
        if saved is not None:
            result.changed_files.append(saved)
            result.notes.extend(changes)

        if not self.config_path.exists():
            result.notes.append(f"{self.config_path} does not exist; no action")
            return result

        original = self.config_path.read_text(encoding="utf-8")
        root_block = _extract_block(original, _ROOT_BEGIN, _ROOT_END)
        if root_block is None:
            result.notes.append("config.toml has no TELOS Codex marker; no action")
            return result

        previous = _extract_previous_model_provider(root_block)
        text, root_inner = _strip_block(original, _ROOT_BEGIN, _ROOT_END)
        text, provider_inner = _strip_block(text, _PROVIDER_BEGIN, _PROVIDER_END)
        # Preserve any foreign content the same way install() does, so
        # uninstall is non-destructive even if codex rewrote our block.
        provider_orphans = _strip_provider_table_from(provider_inner)
        root_orphans = "".join(
            line for line in root_inner.splitlines(keepends=True)
            if not line.startswith(_PREV_PREFIX)
            and line.strip() not in ("", 'model_provider = "telos"')
        )
        orphan_text = root_orphans + provider_orphans
        if orphan_text:
            text = orphan_text + ("\n" if not orphan_text.endswith("\n") else "") + text.lstrip("\n")
            result.notes.append(
                "recovered foreign content from inside the TELOS managed region"
            )
        if previous is not None:
            text = previous + "\n" + text.lstrip("\n")
            result.notes.append(f"restored {previous}")
        else:
            result.notes.append("removed TELOS model_provider override")
        if text and not text.endswith("\n"):
            text += "\n"
        _atomic_write(self.config_path, text)
        result.changed_files.append(self.config_path)
        return result

    def status(self) -> InstallResult:
        result = InstallResult(agent=self.name, action="status")
        expected_plugin_files = _trace_plugin_files()
        plugin_current = all(
            (self.trace_plugin_path / relative_path).is_file()
            for relative_path in expected_plugin_files
        )
        result.notes.append(
            "Codex tracing plugin bundle is prepared"
            if plugin_current
            else "Codex tracing plugin bundle is not prepared"
        )
        fallback_current = False
        try:
            hooks_data = json.loads(self.hooks_path.read_text(encoding="utf-8"))
            hooks = hooks_data.get("hooks") if isinstance(hooks_data, dict) else None
            fallback_current = isinstance(hooks, dict) and all(
                isinstance(hooks.get(event), list)
                and any(
                    isinstance(group, dict)
                    and isinstance(group.get("hooks"), list)
                    and any(_is_telos_hook_handler(handler) for handler in group["hooks"])
                    for group in hooks[event]
                )
                for event in HOOK_EVENTS
            )
        except (OSError, json.JSONDecodeError):
            pass
        result.notes.append(
            "Codex tracing hooks.json fallback is enabled"
            if fallback_current
            else "Codex tracing hooks.json fallback is not enabled"
        )
        if not self.config_path.exists():
            result.notes.append(f"{self.config_path} does not exist")
            return result
        text = self.config_path.read_text(encoding="utf-8")
        auth_mode = _detect_auth_mode(self.auth_json_path)
        provider_url = self._provider_url(auth_mode)
        if _ROOT_BEGIN in text and _PROVIDER_BEGIN in text and provider_url in text:
            result.already_installed = True
            result.notes.append(f"connected to the TELOS gateway: {provider_url}")
            result.notes.append("wire_api=responses is currently gateway passthrough")
        elif _ROOT_BEGIN in text or _PROVIDER_BEGIN in text:
            result.notes.append("TELOS Codex markers exist, but the gateway URL differs")
        else:
            result.notes.append("config.toml has no TELOS Codex provider")
        return result
