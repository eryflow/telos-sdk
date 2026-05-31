"""Multi-route HermesInstaller tests (Phase 2.5).

Hermes' patchable surface is two-tier:
- top-level ``model.base_url``    (synthetic key ``__primary__``)
- ``providers.<id>.model.base_url``

These tests pin the selection via ``providers_to_patch`` so they're
deterministic without a TTY.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from telos.init.hermes import HermesInstaller


def _write_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _multi_route_cfg() -> dict:
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
                    "base_url": "https://api.aiclaude.xyz/",
                    "api_key": "sk-test",
                    "api_mode": "chat_completions",
                },
            },
            "anthropic_direct": {
                "model": {
                    "base_url": "https://api.anthropic.com",
                    "api_mode": "anthropic_messages",
                },
            },
        },
    }


def _setup(tmp_path: Path, providers_to_patch=None):
    config_path = tmp_path / "config.yaml"
    _write_yaml(config_path, _multi_route_cfg())
    state_path = tmp_path / "state.json"
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    inst = HermesInstaller(
        proxy_url="http://127.0.0.1:7171",
        config_path=config_path,
        state_path=state_path,
        providers_to_patch=providers_to_patch,
    )
    return inst, config_path, state_path


def _teardown():
    os.environ.pop("TELOS_HOME", None)


def test_install_subset_top_and_one_provider(tmp_path: Path) -> None:
    inst, config_path, state_path = _setup(
        tmp_path,
        providers_to_patch=["__primary__", "aiclaudexyz"],
    )
    try:
        inst.install()
        data = yaml.safe_load(config_path.read_text())
        # Top-level patched.
        assert (data["model"]["base_url"]
                == "http://127.0.0.1:7171/_h/hermes/upstreams/openrouter")
        # aiclaudexyz patched.
        assert (data["providers"]["aiclaudexyz"]["model"]["base_url"]
                == "http://127.0.0.1:7171/_h/hermes/upstreams/aiclaudexyz")
        # anthropic_direct untouched.
        assert (data["providers"]["anthropic_direct"]["model"]["base_url"]
                == "https://api.anthropic.com")
        # State has 2 records keyed correctly.
        state = json.loads(state_path.read_text())
        keys = sorted(r["key"] for r in state["patched"])
        assert keys == ["__primary__", "aiclaudexyz"]
    finally:
        _teardown()


def test_install_anthropic_provider_uses_anthropic_protocol(tmp_path: Path) -> None:
    """The anthropic_direct provider entry uses api_mode: anthropic_messages,
    so its telos upstream must record protocol=anthropic-messages."""
    inst, _config_path, _state = _setup(
        tmp_path, providers_to_patch=["anthropic_direct"],
    )
    try:
        inst.install()
        telos_cfg_path = Path(os.environ["TELOS_HOME"]) / "config.json"
        data = json.loads(telos_cfg_path.read_text())
        upstream = data["upstreams"]["anthropic_direct"]
        assert upstream["protocol"] == "anthropic-messages"
        assert upstream["engine"] == "anthropic"
    finally:
        _teardown()


def test_incremental_install_merges_state(tmp_path: Path) -> None:
    inst1, config_path, state_path = _setup(
        tmp_path, providers_to_patch=["__primary__"],
    )
    try:
        inst1.install()
        inst2 = HermesInstaller(
            proxy_url="http://127.0.0.1:7171",
            config_path=config_path,
            state_path=state_path,
            providers_to_patch=["aiclaudexyz"],
        )
        inst2.install()
        data = yaml.safe_load(config_path.read_text())
        assert (data["model"]["base_url"]
                == "http://127.0.0.1:7171/_h/hermes/upstreams/openrouter")
        assert (data["providers"]["aiclaudexyz"]["model"]["base_url"]
                == "http://127.0.0.1:7171/_h/hermes/upstreams/aiclaudexyz")
        state = json.loads(state_path.read_text())
        keys = sorted(r["key"] for r in state["patched"])
        assert keys == ["__primary__", "aiclaudexyz"]
    finally:
        _teardown()


def test_uninstall_restores_all_patched(tmp_path: Path) -> None:
    inst, config_path, state_path = _setup(
        tmp_path,
        providers_to_patch=["__primary__", "aiclaudexyz"],
    )
    try:
        inst.install()
        inst.uninstall()
        data = yaml.safe_load(config_path.read_text())
        assert (data["model"]["base_url"]
                == "https://openrouter.ai/api/v1")
        assert (data["providers"]["aiclaudexyz"]["model"]["base_url"]
                == "https://api.aiclaude.xyz/")
        assert not state_path.exists()
    finally:
        _teardown()


def test_state_v1_backward_compat(tmp_path: Path) -> None:
    """v1 single-record state (legacy hermes installer) reads as one __primary__
    entry; uninstall restores the top-level model and removes state.
    """
    config_path = tmp_path / "config.yaml"
    cfg = _multi_route_cfg()
    cfg["model"]["base_url"] = "http://127.0.0.1:7171/_h/hermes/upstreams/openrouter"
    _write_yaml(config_path, cfg)

    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "version": 1,
        "provider_id": "openrouter",
        "previous_base_url": "https://openrouter.ai/api/v1",
        "gateway_route_url": "http://127.0.0.1:7171/_h/hermes/upstreams/openrouter",
    }))

    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    try:
        inst = HermesInstaller(
            config_path=config_path,
            state_path=state_path,
        )
        inst.uninstall()
        data = yaml.safe_load(config_path.read_text())
        assert data["model"]["base_url"] == "https://openrouter.ai/api/v1"
        assert not state_path.exists()
    finally:
        _teardown()
