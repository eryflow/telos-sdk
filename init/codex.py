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
import shutil
from dataclasses import dataclass
from pathlib import Path

from telos.config import (
    UpstreamConfig,
    load_config,
    revert_upstreams_owned_by,
    save_config,
)
from telos.init.base import AgentInstaller, InstallResult

_ROOT_BEGIN = "# >>> telos managed codex root\n"
_ROOT_END = "# <<< telos managed codex root\n"
_PROVIDER_BEGIN = "# >>> telos managed codex provider\n"
_PROVIDER_END = "# <<< telos managed codex provider\n"
_PREV_PREFIX = "# telos_previous_model_provider = "
_ABSENT = "<absent>"

_CHATGPT_SLUG = "codex-chatgpt"
_CHATGPT_UPSTREAM_URL = "https://chatgpt.com/backend-api/codex"


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
    ) -> None:
        super().__init__(proxy_url=proxy_url)
        self.config_path = config_path or _default_config_path()
        # auth.json lives next to config.toml; tests pass a tmp path so they
        # don't depend on the developer's real Codex login.
        self.auth_json_path = (
            auth_json_path if auth_json_path is not None
            else self.config_path.parent / "auth.json"
        )

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
        # For chatgpt mode, the gateway also needs the matching upstream slug.
        # Do this even if config.toml is already current, so a fresh `telos
        # init codex` on a machine that lost ~/.telos/config.json self-heals.
        if auth_mode == "chatgpt":
            self._ensure_chatgpt_upstream(result)

        if orphan_text:
            result.notes.append(
                "recovered foreign content from inside the TELOS managed region "
                "(likely moved there by codex's own config rewriter); "
                "re-emitting it outside the managed block."
            )

        if new_text == original:
            result.already_installed = True
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
