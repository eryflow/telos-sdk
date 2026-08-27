"""Installer for the DeepSeek Harness native session-telemetry seam."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Iterable

import yaml

from telos.config import (
    config_path,
    enable_harness_trace,
    load_config,
    save_config,
    telos_home,
)
from telos.init.base import AgentInstaller, InstallResult


_BEGIN = "# >>> telos managed deepseek-harness tracing\n"
_END = "# <<< telos managed deepseek-harness tracing\n"
_PLUGIN_ID = "session-telemetry-telos"
_ASSET_NAME = "telos-session-telemetry.mjs"
_SOURCE_ASSET = Path(__file__).with_name("assets") / "deepseek_harness_telemetry.mjs"


class _DshLoader(yaml.SafeLoader):
    """Safe loader with DeepSeek Harness's scalar ``!!js`` dialect."""


_DshLoader.add_constructor(
    "tag:yaml.org,2002:js",
    lambda loader, node: loader.construct_scalar(node),
)


def _dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME", Path.home() / ".dsh"))


def _load_yaml_rows(path: Path, *, source: str) -> list[dict[str, Any]]:
    if not path.exists():
        raise RuntimeError(f"{source} does not exist: {path}")
    text = path.read_text(encoding="utf-8")
    return _load_rows(text, source=source)


def _compose_effective_rows(
    base_rows: list[dict[str, Any]],
    patch_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply a minimal patch layer to produce effective rows for verification."""

    rows: list[dict[str, Any]] = list(base_rows)
    for row in patch_rows:
        if not isinstance(row, dict):
            continue
        insert_rows = row.get("insert")
        if isinstance(insert_rows, list):
            for item in insert_rows:
                if isinstance(item, dict):
                    rows.append(item)
            continue
        rows.append(row)
    return rows


def _atomic_write(path: Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None and path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        tmp.chmod(mode)
    os.replace(tmp, path)


def _load_rows(text: str, *, source: str) -> list[dict[str, Any]]:
    try:
        value = yaml.load(text, Loader=_DshLoader)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"{source} is not valid DeepSeek Harness YAML: {exc}") from exc
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise RuntimeError(f"{source} must contain a top-level YAML list")
    return value


def _walk_rows(rows: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for row in rows:
        yield row
        nested = row.get("config")
        if row.get("group") is True and isinstance(nested, list):
            yield from _walk_rows(item for item in nested if isinstance(item, dict))


def _telemetry_rows(
    rows: list[dict[str, Any]], *, include_disabled: bool = False,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for row in _walk_rows(rows):
        row_id = str(row.get("id") or "")
        name = str(row.get("name") or "")
        identity = f"{row_id} {name}".lower()
        if row_id == _PLUGIN_ID or (row.get("disabled") is True and not include_disabled):
            continue
        if "session-telemetry" in identity:
            found.append(row)
    return found


def _strip_managed_block(text: str) -> tuple[str, bool]:
    start = text.find(_BEGIN)
    if start < 0:
        return text, False
    end = text.find(_END, start + len(_BEGIN))
    if end < 0:
        raise RuntimeError("DeepSeek Harness patch has a TELOS begin marker without its end marker")
    end += len(_END)
    return text[:start] + text[end:], True


def _render_managed_block(
    *,
    conflicts: list[dict[str, Any]],
    asset_path: Path,
    token_path: Path,
    endpoint: str,
) -> str:
    patches: list[dict[str, Any]] = []
    for row in conflicts:
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id:
            raise RuntimeError(
                "an active session telemetry backend has no stable row id; "
                "TELOS cannot disable and restore it safely"
            )
        patches.append({"id": row_id, "disabled": True})
    patches.append({
        "insert": [{
            "id": _PLUGIN_ID,
            "name": asset_path.resolve().as_uri(),
            "config": {
                "endpoint": endpoint,
                "tokenFile": str(token_path.resolve()),
                "queueSize": 2048,
                "batchSize": 256,
                "maxBatchBytes": 921600,
                "requestTimeoutMs": 2000,
                "shutdownTimeoutMs": 3000,
            },
        }],
    })
    body = yaml.safe_dump(patches, sort_keys=False, allow_unicode=True)
    return _BEGIN + body + _END


def _install_block(base: str, block: str, *, source: str) -> str:
    without, _ = _strip_managed_block(base)
    rows = _load_rows(without, source=source)
    if rows:
        first_data = next(
            (line.lstrip() for line in without.splitlines()
             if line.strip() and not line.lstrip().startswith("#")),
            "",
        )
        if not first_data.startswith("-"):
            raise RuntimeError(
                f"{source} uses a flow-style list; convert it to block-style YAML before installing TELOS"
            )
        prefix = without.rstrip() + "\n"
    else:
        # The shipped profile template is comments plus one standalone ``[]``.
        prefix, count = re.subn(r"(?m)^[ \t]*\[\][ \t]*(?:\n|$)", "", without, count=1)
        if count == 0 and without.strip() and any(
            line.strip() and not line.lstrip().startswith("#")
            for line in without.splitlines()
        ):
            raise RuntimeError(f"cannot safely edit empty patch layer {source}")
        prefix = prefix.rstrip() + "\n" if prefix.rstrip() else ""
    return prefix + block


def _uninstall_block(text: str) -> tuple[str, bool]:
    base, found = _strip_managed_block(text)
    if not found:
        return text, False
    if not _load_rows(base, source="remaining DeepSeek Harness patch"):
        base = base.rstrip() + "\n[]\n"
    elif not base.endswith("\n"):
        base += "\n"
    return base, True


class DeepSeekHarnessInstaller(AgentInstaller):
    """Install one TELOS backend into a named DSH profile patch layer.

    A DeepSeek Harness context accepts exactly one ``sessionTelemetry`` service.
    Therefore an existing backend is never replaced unless the caller opts in.
    """

    name = "deepseek-harness"

    def __init__(
        self,
        *,
        proxy_url: str = "http://127.0.0.1:7171",
        profile: str = "web",
        dsh_executable: str = "dsh",
        dsh_home: Path | None = None,
        asset_path: Path | None = None,
        token_path: Path | None = None,
        replace_telemetry_backend: bool = False,
    ) -> None:
        super().__init__(proxy_url=proxy_url)
        self.profile = profile
        self.dsh_executable = dsh_executable
        self.dsh_home = dsh_home or _dsh_home()
        integration_dir = telos_home() / "integrations" / self.name
        self.asset_path = asset_path or integration_dir / _ASSET_NAME
        self.token_path = token_path or integration_dir / "ingest-token"
        self.patch_path = self.dsh_home / "profiles" / profile / "cordis.patch.yml"
        self.replace_telemetry_backend = replace_telemetry_backend

    @property
    def endpoint(self) -> str:
        return f"{self.proxy_url.rstrip('/')}/__telos/tracing/v1/batch"

    def _fallback_dump(self) -> tuple[str, list[dict[str, Any]]]:
        base_path = self.dsh_home / "profiles" / self.profile / "cordis.yml"
        base = _load_yaml_rows(
            base_path,
            source=f"dsh profile {self.profile!r} base config",
        )
        if self.patch_path.exists():
            patch = _load_yaml_rows(
                self.patch_path,
                source=f"dsh profile {self.profile!r} patch config",
            )
        else:
            patch = []
        merged = _compose_effective_rows(base, patch)
        return yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), merged

    def _profile_uses_bundles(self) -> bool:
        manifest = self.patch_path.with_name("package.json")
        if not manifest.exists():
            return False
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            bundles = data.get("dsh", {}).get("profile", {}).get("bundles", [])
        except (OSError, ValueError, AttributeError):
            return False
        return isinstance(bundles, list) and bool(bundles)

    @staticmethod
    def _looks_like_legacy_profile_error(message: str) -> bool:
        text = message.lower()
        return "unrecognized option" in text and "--profile" in text

    def _dump_config(self) -> tuple[str, list[dict[str, Any]]]:
        command = [self.dsh_executable, "--profile", self.profile, "--dump-config"]
        env = os.environ.copy()
        env["DSH_HOME"] = str(self.dsh_home)
        try:
            completed = subprocess.run(
                command,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"cannot find DeepSeek Harness executable {self.dsh_executable!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("`dsh --dump-config` timed out") from exc

        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
            if self._looks_like_legacy_profile_error(detail):
                if self._profile_uses_bundles():
                    raise RuntimeError(
                        f"{self.dsh_executable!r} is not the DeepSeek Harness CLI; "
                        "the selected profile uses bundle layers and cannot be "
                        "verified from cordis.yml alone. Pass the real CLI with "
                        "--dsh-executable (for a source checkout, use its built "
                        "apps/cli/lib/bin.js)."
                    )
                return self._fallback_dump()
            raise RuntimeError(f"`dsh --profile {self.profile} --dump-config` failed: {detail}")

        dump = completed.stdout.strip()
        try:
            return completed.stdout, _load_rows(dump, source=f"dsh profile {self.profile!r} effective config")
        except Exception:
            # Newer harness builds should print YAML here. If not, assume the local
            # `dsh` is the legacy distributed shell binary and fall back to files.
            if self._looks_like_legacy_profile_error(detail := completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"):
                return self._fallback_dump()
            raise

    def _write_runtime_files(self, token: str, result: InstallResult) -> None:
        if not _SOURCE_ASSET.exists():
            raise RuntimeError(f"packaged DeepSeek Harness adapter is missing: {_SOURCE_ASSET}")
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.token_path.parent.chmod(0o700)
        except OSError:
            pass
        asset = _SOURCE_ASSET.read_text(encoding="utf-8")
        current_asset = self.asset_path.read_text(encoding="utf-8") if self.asset_path.exists() else None
        if current_asset != asset:
            _atomic_write(self.asset_path, asset, mode=0o644)
            result.changed_files.append(self.asset_path)
        current_token = self.token_path.read_text(encoding="utf-8").strip() if self.token_path.exists() else None
        if current_token != token or (
            self.token_path.exists() and stat.S_IMODE(self.token_path.stat().st_mode) != 0o600
        ):
            _atomic_write(self.token_path, token + "\n", mode=0o600)
            result.changed_files.append(self.token_path)

    def _restore_trace_policy(self, previous: dict[str, Any] | None) -> None:
        cfg = load_config()
        if previous is None:
            cfg.trace_harnesses.pop(self.name, None)
        else:
            cfg.trace_harnesses[self.name] = previous
        save_config(cfg)

    def install(self) -> InstallResult:
        result = InstallResult(agent=self.name, action="install")
        _dump, effective = self._dump_config()
        marker = False
        if self.patch_path.exists():
            _base, marker = _strip_managed_block(
                self.patch_path.read_text(encoding="utf-8")
            )
        foreign_owned = any(
            row.get("id") == _PLUGIN_ID for row in _walk_rows(effective)
        ) and not marker
        if foreign_owned:
            raise RuntimeError(f"profile already uses reserved row id {_PLUGIN_ID!r}")
        # On reinstall the existing TELOS block already disabled the previous
        # backend in the effective dump; retain that owned disable patch.
        conflicts = _telemetry_rows(effective, include_disabled=marker)
        if conflicts and not self.replace_telemetry_backend:
            identities = ", ".join(
                str(row.get("id") or row.get("name")) for row in conflicts
            )
            raise RuntimeError(
                "DeepSeek Harness allows one sessionTelemetry backend; "
                f"active backend(s): {identities}. Re-run with explicit "
                "replace_telemetry_backend=True to disable them in the TELOS-owned patch."
            )
        if not self.patch_path.exists():
            raise RuntimeError(
                f"profile patch was not created by dsh: {self.patch_path}; "
                "run `dsh --profile <name> --dump-config` (or ensure legacy-free dsh profile files exist) and retry"
            )

        previous_policy = load_config().trace_harnesses.get(self.name)
        cfg, config_changed = enable_harness_trace(
            self.name, model_span_source="adapter"
        )
        policy = cfg.trace_harnesses[self.name]
        if config_changed:
            result.changed_files.append(config_path())
        token = str(policy["tracing_token"])
        self._write_runtime_files(token, result)

        original = self.patch_path.read_text(encoding="utf-8")
        block = _render_managed_block(
            conflicts=conflicts,
            asset_path=self.asset_path,
            token_path=self.token_path,
            endpoint=self.endpoint,
        )
        updated = _install_block(original, block, source=str(self.patch_path))
        backup = self.patch_path.with_suffix(self.patch_path.suffix + ".telos.bak")
        if updated != original and not backup.exists():
            shutil.copy2(self.patch_path, backup)
            result.backups.append(backup)
        if updated != original:
            _atomic_write(self.patch_path, updated)
            result.changed_files.append(self.patch_path)

        try:
            _verified_text, verified = self._dump_config()
            own = [row for row in _walk_rows(verified) if row.get("id") == _PLUGIN_ID]
            remaining = _telemetry_rows(verified)
            if len(own) != 1 or remaining:
                raise RuntimeError(
                    "dump-config did not resolve exactly one TELOS telemetry backend "
                    f"(telos={len(own)}, other={len(remaining)})"
                )
        except Exception:
            if updated != original:
                _atomic_write(self.patch_path, original)
            self._restore_trace_policy(previous_policy)
            raise

        result.already_installed = updated == original and not any(
            path in result.changed_files for path in (self.asset_path, self.token_path, config_path())
        )
        result.notes.append(
            f"profile {self.profile!r} uses model_span_source=adapter; "
            "dump-config composition verified (runtime activation occurs on DSH load/hot reload)"
        )
        return result

    def uninstall(self) -> InstallResult:
        result = InstallResult(agent=self.name, action="uninstall")
        if not self.patch_path.exists():
            result.notes.append(f"profile patch does not exist: {self.patch_path}")
            return result
        original = self.patch_path.read_text(encoding="utf-8")
        updated, found = _uninstall_block(original)
        if not found:
            result.notes.append("TELOS owns no patch block in this profile")
            return result
        _atomic_write(self.patch_path, updated)
        try:
            _text, verified = self._dump_config()
            if any(row.get("id") == _PLUGIN_ID for row in _walk_rows(verified)):
                raise RuntimeError("dump-config still contains the TELOS telemetry row")
        except Exception:
            _atomic_write(self.patch_path, original)
            raise
        result.changed_files.append(self.patch_path)
        result.notes.append(
            "removed only the TELOS profile patch; shared adapter/token files and trace history were retained"
        )
        return result

    def status(self) -> InstallResult:
        result = InstallResult(agent=self.name, action="status")
        marker = False
        if self.patch_path.exists():
            text = self.patch_path.read_text(encoding="utf-8")
            _base, marker = _strip_managed_block(text)
        _dump, effective = self._dump_config()
        own = [row for row in _walk_rows(effective) if row.get("id") == _PLUGIN_ID]
        conflicts = _telemetry_rows(effective)
        policy = load_config().trace_harnesses.get(self.name) or {}
        configured_token = str(policy.get("tracing_token") or "")
        installed_token = (
            self.token_path.read_text(encoding="utf-8").strip()
            if self.token_path.exists() else ""
        )
        token_secure = self.token_path.exists() and (
            os.name == "nt" or stat.S_IMODE(self.token_path.stat().st_mode) == 0o600
        )
        result.already_installed = (
            marker
            and len(own) == 1
            and not conflicts
            and self.asset_path.exists()
            and token_secure
            and policy.get("enabled") is True
            and policy.get("model_span_source") == "adapter"
            and configured_token == installed_token
        )
        if result.already_installed:
            result.notes.append(f"TELOS tracing is active in DSH profile {self.profile!r}")
        else:
            result.notes.append(
                f"TELOS tracing is incomplete for profile {self.profile!r} "
                f"(patch={marker}, row={len(own)}, conflicts={len(conflicts)}, "
                f"asset={self.asset_path.exists()}, token_secure={token_secure})"
            )
        return result
