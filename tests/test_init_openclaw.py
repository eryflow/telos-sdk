"""OpenClawInstaller tests.

The installer patches ``~/.openclaw/openclaw.json`` to route the primary
provider through the telos gateway, and mirrors the original baseUrl into
``~/.telos/config.json`` so the gateway can forward verbatim.

These tests inject a fake openclaw config + a separate telos home (via
``TELOS_HOME``) so the real user files are never touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from telos.init.openclaw import OpenClawInstaller


def _sample_openclaw_config() -> dict[str, Any]:
    return {
        "agents": {
            "defaults": {
                "model": {"primary": "deepseek/deepseek-v4-flash"},
                "models": {
                    "deepseek/deepseek-v4-flash": {"alias": "DeepSeek"},
                },
            },
        },
        "models": {
            "mode": "merge",
            "providers": {
                "deepseek": {
                    "baseUrl": "https://api.deepseek.com",
                    "api": "openai-completions",
                    "models": [{"id": "deepseek-v4-flash"}],
                },
            },
        },
    }


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _make_inst(tmp_path: Path) -> tuple[OpenClawInstaller, Path, Path]:
    config_path = tmp_path / "openclaw" / "openclaw.json"
    _write_json(config_path, _sample_openclaw_config())
    state_path = tmp_path / "telos-state" / "openclaw.json"
    # Point TELOS_HOME at a clean temp dir so save_config doesn't clobber the
    # user's real ~/.telos/config.json.
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    inst = OpenClawInstaller(
        proxy_url="http://127.0.0.1:7171",
        config_path=config_path,
        state_path=state_path,
    )
    return inst, config_path, state_path


def _restore_env() -> None:
    os.environ.pop("TELOS_HOME", None)


def test_install_patches_provider_baseurl(tmp_path: Path) -> None:
    inst, config_path, state_path = _make_inst(tmp_path)
    try:
        r = inst.install()
        assert config_path in r.changed_files
        data = json.loads(config_path.read_text())
        new_url = data["models"]["providers"]["deepseek"]["baseUrl"]
        assert new_url == "http://127.0.0.1:7171/upstreams/deepseek"
        # State file records the original (v2 list format).
        state = json.loads(state_path.read_text())
        assert state["version"] == 2
        recs = state["patched"]
        assert len(recs) == 1
        assert recs[0]["previous_base_url"] == "https://api.deepseek.com"
        assert recs[0]["provider_id"] == "deepseek"
    finally:
        _restore_env()


def test_install_mirrors_url_into_telos_upstreams(tmp_path: Path) -> None:
    """The slug used by the gateway resolves to the same URL openclaw was
    originally using. We assert via ``load_config`` (which transparently
    falls back to in-memory defaults when nothing was written to disk).
    """
    from telos.config import load_config as _load_telos_config

    # Use a baseUrl that does NOT match telos's default deepseek upstream,
    # so we also exercise the "write changes to disk" path.
    config_path = tmp_path / "openclaw" / "openclaw.json"
    data = _sample_openclaw_config()
    data["models"]["providers"]["deepseek"]["baseUrl"] = "https://custom.deepseek.example/v1"
    _write_json(config_path, data)
    state_path = tmp_path / "telos-state" / "openclaw.json"
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    inst = OpenClawInstaller(
        proxy_url="http://127.0.0.1:7171",
        config_path=config_path,
        state_path=state_path,
    )
    try:
        inst.install()
        # The telos config file should have been written (custom URL differs
        # from telos defaults).
        telos_cfg_path = Path(os.environ["TELOS_HOME"]) / "config.json"
        assert telos_cfg_path.exists()
        upstream = _load_telos_config().upstreams["deepseek"]
        assert upstream.url == "https://custom.deepseek.example"
        assert upstream.protocol == "openai-chat"
        assert upstream.engine == "deepseek"
        # Phase 2.6: the installer tags the slug with its own name so the
        # gateway can label dashboard entries as "openclaw" rather than the
        # wire-level "telos" harness for OpenAI-shape traffic.
        assert upstream.via == "openclaw"
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
        # Backup is the pre-install snapshot.
        backed_up = json.loads(backup.read_text())
        assert (backed_up["models"]["providers"]["deepseek"]["baseUrl"]
                == "https://api.deepseek.com")
    finally:
        _restore_env()


def test_uninstall_restores_original(tmp_path: Path) -> None:
    inst, config_path, state_path = _make_inst(tmp_path)
    try:
        inst.install()
        r = inst.uninstall()
        assert config_path in r.changed_files
        data = json.loads(config_path.read_text())
        assert (data["models"]["providers"]["deepseek"]["baseUrl"]
                == "https://api.deepseek.com")
        assert not state_path.exists()
    finally:
        _restore_env()


def test_uninstall_skips_when_user_manually_changed(tmp_path: Path) -> None:
    """If after install the user manually changed the baseUrl to something
    else, uninstall must not stomp on it."""
    inst, config_path, state_path = _make_inst(tmp_path)
    try:
        inst.install()
        # Simulate user manually changing baseUrl.
        data = json.loads(config_path.read_text())
        data["models"]["providers"]["deepseek"]["baseUrl"] = "https://something-else"
        config_path.write_text(json.dumps(data, indent=2))

        r = inst.uninstall()
        # Openclaw's own openclaw.json is left alone (the user clearly
        # picked a different route); state file is preserved so a later
        # legitimate uninstall can still restore.
        assert config_path not in r.changed_files
        data2 = json.loads(config_path.read_text())
        assert (data2["models"]["providers"]["deepseek"]["baseUrl"]
                == "https://something-else")
        # State file kept so user can decide.
        assert state_path.exists()
    finally:
        _restore_env()


def test_status_reports_connected(tmp_path: Path) -> None:
    inst, _config_path, _state = _make_inst(tmp_path)
    try:
        inst.install()
        r = inst.status()
        assert r.already_installed is True
        assert any("upstreams/deepseek" in n for n in r.notes)
    finally:
        _restore_env()


def test_status_reports_not_installed(tmp_path: Path) -> None:
    inst, _config_path, _state = _make_inst(tmp_path)
    try:
        r = inst.status()
        assert r.already_installed is False
        # New status format shows per-provider lines; unrouted providers
        # use the "direct →" prefix.
        assert any("direct →" in n for n in r.notes)
    finally:
        _restore_env()


def test_install_no_config_file(tmp_path: Path) -> None:
    inst = OpenClawInstaller(
        config_path=tmp_path / "nonexistent" / "openclaw.json",
        state_path=tmp_path / "state.json",
    )
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    try:
        r = inst.install()
        assert not r.changed_files
        assert any("does not exist" in n for n in r.notes)
    finally:
        _restore_env()


def test_install_no_primary_provider(tmp_path: Path) -> None:
    """A config missing agents.defaults.model.primary should be reported, not crash."""
    config_path = tmp_path / "openclaw.json"
    # Provider entry exists but no primary set anywhere.
    _write_json(config_path, {
        "models": {
            "providers": {
                "openrouter": {
                    "baseUrl": "https://openrouter.ai/api/v1",
                    "api": "openai-completions",
                },
            },
        },
    })
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    try:
        inst = OpenClawInstaller(
            config_path=config_path,
            state_path=tmp_path / "state.json",
        )
        r = inst.install()
        assert not r.changed_files
        # Either the primary-missing or no-providers-selected note shows up;
        # both correctly communicate "nothing to do".
        assert any(
            "primary" in n or "no providers selected" in n
            for n in r.notes
        )
    finally:
        _restore_env()


def test_install_corrupted_json_gives_clear_error(tmp_path: Path) -> None:
    """A corrupted openclaw.json must not propagate a raw JSONDecodeError —
    the user needs to know which file to fix and where the backup is."""
    config_path = tmp_path / "openclaw.json"
    config_path.write_text("{ this is not valid json", encoding="utf-8")
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    try:
        inst = OpenClawInstaller(
            config_path=config_path,
            state_path=tmp_path / "state.json",
        )
        with pytest.raises(RuntimeError) as excinfo:
            inst.install()
        msg = str(excinfo.value)
        assert "not valid JSON" in msg
        assert str(config_path) in msg
        assert ".telos.bak" in msg
    finally:
        _restore_env()


def test_install_empty_config_treated_as_missing(tmp_path: Path) -> None:
    """A zero-byte openclaw.json is "config doesn't exist yet", not a crash."""
    config_path = tmp_path / "openclaw.json"
    config_path.write_text("", encoding="utf-8")
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    try:
        inst = OpenClawInstaller(
            config_path=config_path,
            state_path=tmp_path / "state.json",
        )
        r = inst.install()
        assert not r.changed_files
        assert any("does not exist" in n or "openclaw setup" in n for n in r.notes)
    finally:
        _restore_env()


def test_install_non_object_top_level_gives_clear_error(tmp_path: Path) -> None:
    """A valid-JSON but non-object top level (someone wrote a list) errors clearly."""
    config_path = tmp_path / "openclaw.json"
    config_path.write_text("[1, 2, 3]", encoding="utf-8")
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    try:
        inst = OpenClawInstaller(
            config_path=config_path,
            state_path=tmp_path / "state.json",
        )
        with pytest.raises(RuntimeError) as excinfo:
            inst.install()
        assert "JSON object" in str(excinfo.value)
    finally:
        _restore_env()
