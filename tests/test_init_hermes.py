"""HermesInstaller tests.

The installer patches ``~/.hermes/config.yaml``'s top-level
``model.base_url`` to route through telos and mirrors the original URL into
``~/.telos/config.json`` upstreams. Uses a temp config + ``TELOS_HOME``
override to avoid touching real user files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
import pytest

from telos.init.hermes import HermesInstaller


def _sample_hermes_config() -> dict[str, Any]:
    return {
        "model": {
            "default": "deepseek/deepseek-chat",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        },
        "providers": {
            "aiclaudexyz": {
                "model": {
                    "default": "anthropic/claude-opus-4.6",
                    "base_url": "https://api.aiclaude.xyz/",
                    "api_key": "sk-test",
                    "api_mode": "chat_completions",
                },
            },
        },
        "agent": {"max_turns": 90},
    }


def _sample_auth_json(provider: str = "openrouter") -> dict[str, Any]:
    return {
        "version": 1,
        "providers": {},
        "active_provider": provider,
        "credential_pool": {
            provider: [
                {
                    "id": "abc123",
                    "label": "TEST_API_KEY",
                    "auth_type": "api_key",
                    "priority": 0,
                    "source": "env:TEST_KEY",
                    "access_token": "sk-test-key",
                    "base_url": "https://openrouter.ai/api/v1",
                }
            ]
        },
    }


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _make_inst(tmp_path: Path) -> tuple[HermesInstaller, Path, Path]:
    config_path = tmp_path / "hermes" / "config.yaml"
    _write_yaml(config_path, _sample_hermes_config())
    # Write auth.json beside config.yaml so credential-pool patching is exercised.
    auth_path = config_path.parent / "auth.json"
    _write_json(auth_path, _sample_auth_json())
    state_path = tmp_path / "telos-state" / "hermes.json"
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    inst = HermesInstaller(
        proxy_url="http://127.0.0.1:7171",
        config_path=config_path,
        state_path=state_path,
    )
    return inst, config_path, state_path


def _restore_env() -> None:
    os.environ.pop("TELOS_HOME", None)


def test_install_patches_model_base_url(tmp_path: Path) -> None:
    inst, config_path, state_path = _make_inst(tmp_path)
    try:
        r = inst.install()
        assert config_path in r.changed_files
        data = yaml.safe_load(config_path.read_text())
        assert (data["model"]["base_url"]
                == "http://127.0.0.1:7171/_h/hermes/upstreams/openrouter")
        # Untouched fields stay put.
        assert data["model"]["api_mode"] == "chat_completions"
        assert data["model"]["provider"] == "openrouter"
        assert data["providers"]["aiclaudexyz"]["model"]["api_key"] == "sk-test"
        state = json.loads(state_path.read_text())
        assert state["version"] == 2
        recs = state["patched"]
        primary_rec = next(r for r in recs if r["key"] == "__primary__")
        assert primary_rec["previous_base_url"] == "https://openrouter.ai/api/v1"
        assert primary_rec["provider_id"] == "openrouter"
    finally:
        _restore_env()


def test_install_mirrors_url_into_telos_upstreams(tmp_path: Path) -> None:
    inst, _config_path, _state = _make_inst(tmp_path)
    try:
        inst.install()
        telos_cfg_path = Path(os.environ["TELOS_HOME"]) / "config.json"
        assert telos_cfg_path.exists()
        telos_data = json.loads(telos_cfg_path.read_text())
        upstream = telos_data["upstreams"]["openrouter"]
        assert upstream["url"] == "https://openrouter.ai/api"
        assert upstream["protocol"] == "openai-chat"
        assert upstream["engine"] == "deepseek"
        # Phase 2.6: dashboard attribution via slug-tagged identity.
        assert upstream["via"] == "hermes"
    finally:
        _restore_env()


def test_install_is_idempotent(tmp_path: Path) -> None:
    inst, _config_path, _state = _make_inst(tmp_path)
    try:
        inst.install()
        r2 = inst.install()
        assert r2.already_installed is True
        assert not r2.changed_files
    finally:
        _restore_env()


def test_install_creates_backup_on_first_run(tmp_path: Path) -> None:
    inst, config_path, _state = _make_inst(tmp_path)
    try:
        r = inst.install()
        backup = config_path.with_suffix(config_path.suffix + ".telos.bak")
        assert backup.exists()
        assert backup in r.backups
        backed_up = yaml.safe_load(backup.read_text())
        assert (backed_up["model"]["base_url"]
                == "https://openrouter.ai/api/v1")
    finally:
        _restore_env()


def test_uninstall_restores_original(tmp_path: Path) -> None:
    inst, config_path, state_path = _make_inst(tmp_path)
    try:
        inst.install()
        r = inst.uninstall()
        assert config_path in r.changed_files
        data = yaml.safe_load(config_path.read_text())
        assert data["model"]["base_url"] == "https://openrouter.ai/api/v1"
        assert not state_path.exists()
    finally:
        _restore_env()


def test_uninstall_skips_when_user_manually_changed(tmp_path: Path) -> None:
    inst, config_path, state_path = _make_inst(tmp_path)
    try:
        inst.install()
        # User manually changes base_url after install.
        data = yaml.safe_load(config_path.read_text())
        data["model"]["base_url"] = "https://something-else"
        config_path.write_text(yaml.safe_dump(data, sort_keys=False))

        r = inst.uninstall()
        # Hermes's own config.yaml is left alone (the user clearly wanted
        # the manual value); state file is preserved so a later legitimate
        # uninstall can still attempt the restore.
        assert config_path not in r.changed_files
        data2 = yaml.safe_load(config_path.read_text())
        assert data2["model"]["base_url"] == "https://something-else"
        assert state_path.exists()
    finally:
        _restore_env()


def test_status_reports_connected(tmp_path: Path) -> None:
    inst, _config_path, _state = _make_inst(tmp_path)
    try:
        inst.install()
        r = inst.status()
        assert r.already_installed is True
        assert any("upstreams/openrouter" in n for n in r.notes)
    finally:
        _restore_env()


def test_status_reports_not_installed(tmp_path: Path) -> None:
    inst, _config_path, _state = _make_inst(tmp_path)
    try:
        r = inst.status()
        assert r.already_installed is False
        assert any("direct →" in n for n in r.notes)
    finally:
        _restore_env()


def test_install_no_config_file(tmp_path: Path) -> None:
    inst = HermesInstaller(
        config_path=tmp_path / "nonexistent" / "config.yaml",
        state_path=tmp_path / "state.json",
    )
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    try:
        r = inst.install()
        assert not r.changed_files
        assert any("does not exist" in n for n in r.notes)
    finally:
        _restore_env()


def test_install_anthropic_api_mode_uses_anthropic_upstream(tmp_path: Path) -> None:
    """When api_mode=anthropic_messages, the registered upstream uses the
    anthropic protocol + engine."""
    cfg = _sample_hermes_config()
    cfg["model"]["api_mode"] = "anthropic_messages"
    cfg["model"]["provider"] = "claude-anthropic"
    cfg["model"]["base_url"] = "https://api.anthropic.com"
    config_path = tmp_path / "config.yaml"
    _write_yaml(config_path, cfg)
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    try:
        inst = HermesInstaller(
            config_path=config_path,
            state_path=tmp_path / "state.json",
        )
        inst.install()
        telos_data = json.loads(
            (Path(os.environ["TELOS_HOME"]) / "config.json").read_text()
        )
        upstream = telos_data["upstreams"]["claude-anthropic"]
        assert upstream["protocol"] == "anthropic-messages"
        assert upstream["engine"] == "anthropic"
    finally:
        _restore_env()


def test_install_patches_auth_json_credential_pool(tmp_path: Path) -> None:
    """install() also patches the credential_pool base_url in auth.json so the
    hermes runtime (which prefers pool URL over config.yaml) routes through telos."""
    inst, config_path, state_path = _make_inst(tmp_path)
    auth_path = config_path.parent / "auth.json"
    try:
        inst.install()
        auth = json.loads(auth_path.read_text())
        pool_url = auth["credential_pool"]["openrouter"][0]["base_url"]
        assert pool_url == "http://127.0.0.1:7171/_h/hermes/upstreams/openrouter/v1"
        # State records the original so uninstall can restore it.
        state = json.loads(state_path.read_text())
        rec = next(r for r in state["patched"] if r["key"] == "__primary__")
        assert rec["auth_pool_patches"][0]["previous_base_url"] == "https://openrouter.ai/api/v1"
    finally:
        _restore_env()
def test_uninstall_restores_auth_json_credential_pool(tmp_path: Path) -> None:
    """uninstall() restores the credential pool base_url in auth.json."""
    inst, config_path, state_path = _make_inst(tmp_path)
    auth_path = config_path.parent / "auth.json"
    try:
        inst.install()
        inst.uninstall()
        auth = json.loads(auth_path.read_text())
        pool_url = auth["credential_pool"]["openrouter"][0]["base_url"]
        assert pool_url == "https://openrouter.ai/api/v1"
    finally:
        _restore_env()


def test_install_corrupted_yaml_gives_clear_error(tmp_path: Path) -> None:
    """A corrupted config.yaml must not propagate a raw YAMLError —
    the user needs to know which file to fix and where the backup is."""
    config_path = tmp_path / "config.yaml"
    # Unbalanced bracket — yaml.safe_load raises ScannerError.
    config_path.write_text("model:\n  base_url: [unclosed\n", encoding="utf-8")
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    try:
        inst = HermesInstaller(
            config_path=config_path,
            state_path=tmp_path / "state.json",
        )
        with pytest.raises(RuntimeError) as excinfo:
            inst.install()
        msg = str(excinfo.value)
        assert "not valid YAML" in msg
        assert str(config_path) in msg
        assert ".telos.bak" in msg
    finally:
        _restore_env()


def test_install_empty_config_treated_as_missing(tmp_path: Path) -> None:
    """A zero-byte config.yaml is "config doesn't exist yet", not a crash."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    try:
        inst = HermesInstaller(
            config_path=config_path,
            state_path=tmp_path / "state.json",
        )
        r = inst.install()
        assert not r.changed_files
        assert any("does not exist" in n or "YAML mapping" in n or "hermes" in n
                   for n in r.notes)
    finally:
        _restore_env()
