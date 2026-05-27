"""Codex installer.

Codex supports custom model providers in ``~/.codex/config.toml``. TELOS uses
that hook to add a provider that points at the local gateway's OpenAI upstream
route. The current gateway optimizes OpenAI ChatCompletions traffic; Codex's
default Responses API traffic is passed through so direct ``codex`` launches can
still be observed and routed consistently.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from telos.init.base import AgentInstaller, InstallResult

_ROOT_BEGIN = "# >>> telos managed codex root\n"
_ROOT_END = "# <<< telos managed codex root\n"
_PROVIDER_BEGIN = "# >>> telos managed codex provider\n"
_PROVIDER_END = "# <<< telos managed codex provider\n"
_PREV_PREFIX = "# telos_previous_model_provider = "
_ABSENT = "<absent>"


def _default_config_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class _PreparedConfig:
    text: str
    previous_model_provider: str | None


def _strip_block(text: str, begin: str, end: str) -> str:
    start = text.find(begin)
    if start < 0:
        return text
    stop = text.find(end, start)
    if stop < 0:
        raise RuntimeError("found a TELOS managed Codex block without its end marker")
    stop += len(end)
    if stop < len(text) and text[stop] == "\n":
        stop += 1
    return text[:start] + text[stop:]


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
    start = text.find(begin)
    if start < 0:
        return None
    stop = text.find(end, start)
    if stop < 0:
        raise RuntimeError("found a TELOS managed Codex block without its end marker")
    return text[start:stop + len(end)]


class CodexInstaller(AgentInstaller):
    name = "codex"

    def __init__(
        self,
        *,
        proxy_url: str = "http://127.0.0.1:7171",
        config_path: Path | None = None,
    ) -> None:
        super().__init__(proxy_url=proxy_url)
        self.config_path = config_path or _default_config_path()

    def install(self) -> InstallResult:
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
        text = _strip_block(original, _ROOT_BEGIN, _ROOT_END)
        text = _strip_block(text, _PROVIDER_BEGIN, _PROVIDER_END)
        prepared = _remove_top_level_model_provider(text)
        if root_block is None:
            previous = prepared.previous_model_provider
        text = prepared.text

        provider_url = f"{self.proxy_url.rstrip('/')}/upstreams/openai/v1"
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
        if new_text == original:
            result.already_installed = True
            result.notes.append(f"already connected to the TELOS gateway ({provider_url}); no action")
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
        result.notes.append(
            "Codex uses the Responses API by default; the gateway currently passes that path through."
        )
        return result

    def uninstall(self) -> InstallResult:
        result = InstallResult(agent=self.name, action="uninstall")
        if not self.config_path.exists():
            result.notes.append(f"{self.config_path} does not exist; no action")
            return result

        original = self.config_path.read_text(encoding="utf-8")
        root_block = _extract_block(original, _ROOT_BEGIN, _ROOT_END)
        if root_block is None:
            result.notes.append("config.toml has no TELOS Codex marker; no action")
            return result

        previous = _extract_previous_model_provider(root_block)
        text = _strip_block(original, _ROOT_BEGIN, _ROOT_END)
        text = _strip_block(text, _PROVIDER_BEGIN, _PROVIDER_END)
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
        provider_url = f"{self.proxy_url.rstrip('/')}/upstreams/openai/v1"
        if _ROOT_BEGIN in text and _PROVIDER_BEGIN in text and provider_url in text:
            result.already_installed = True
            result.notes.append(f"connected to the TELOS gateway: {provider_url}")
            result.notes.append("wire_api=responses is currently gateway passthrough")
        elif _ROOT_BEGIN in text or _PROVIDER_BEGIN in text:
            result.notes.append("TELOS Codex markers exist, but the gateway URL differs")
        else:
            result.notes.append("config.toml has no TELOS Codex provider")
        return result
