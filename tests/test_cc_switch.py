"""Tests for cc-switch coexistence: detection/classification (``init.cc_switch``)
and the Codex custom-relay capture. The Claude Code relay capture + string-marker
self-heal are covered in ``test_init_claude_code.py``.

All paths are redirected to temp dirs (``CC_SWITCH_HOME`` / ``CLAUDE_CONFIG_DIR``
/ ``CODEX_HOME`` / ``TELOS_HOME``) so the real user files are never touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from telos.config import load_config
from telos.init import cc_switch
from telos.init.codex import CodexInstaller


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Redirect every home/config dir at once."""
    monkeypatch.setenv("CC_SWITCH_HOME", str(tmp_path / "cc-switch"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("TELOS_HOME", str(tmp_path / "telos-home"))
    return tmp_path


# --- detection ------------------------------------------------------------

def test_is_installed_false_when_absent(env) -> None:
    assert cc_switch.is_installed() is False


def test_is_installed_true_when_home_present(env) -> None:
    (env / "cc-switch").mkdir()
    assert cc_switch.is_installed() is True


def test_read_device_settings(env) -> None:
    home = env / "cc-switch"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({
        "currentProviderClaude": "abc-123",
        "currentProviderCodex": "codex-official",
        "language": "zh",
    }))
    dev = cc_switch.read_device_settings()
    assert dev.current_provider_claude == "abc-123"
    assert dev.current_provider_codex == "codex-official"


def test_read_device_settings_tolerates_missing(env) -> None:
    dev = cc_switch.read_device_settings()  # no file at all
    assert dev.current_provider_claude is None


# --- classify_harness -----------------------------------------------------

def _write_claude_env(env, base_url: str | None, *, marker=None) -> None:
    claude = env / "claude"
    claude.mkdir(parents=True, exist_ok=True)
    envblock: dict = {}
    if base_url is not None:
        envblock["ANTHROPIC_BASE_URL"] = base_url
    if marker is not None:
        envblock["__telos_installed"] = marker
    (claude / "settings.json").write_text(json.dumps({"env": envblock}))


def test_classify_relay_active(env) -> None:
    _write_claude_env(env, "https://relay.example/api", marker=None)
    st = cc_switch.classify_harness("claude-code")
    assert st.state == cc_switch.RELAY_ACTIVE
    assert st.live_base_url == "https://relay.example/api"


def test_classify_official(env) -> None:
    _write_claude_env(env, "https://api.anthropic.com", marker=None)
    st = cc_switch.classify_harness("claude-code")
    assert st.state == cc_switch.OFFICIAL


def test_classify_telos_chained(env) -> None:
    # both a telos route and the (string-coerced) marker → chained
    _write_claude_env(env, "http://127.0.0.1:7171/_h/claude-code", marker="true")
    st = cc_switch.classify_harness("claude-code")
    assert st.state == cc_switch.TELOS_CHAINED


def test_classify_absent(env) -> None:
    st = cc_switch.classify_harness("claude-code")  # no settings.json
    assert st.state == cc_switch.ABSENT


# --- Codex relay capture --------------------------------------------------

def _codex_with_custom_provider(env, base_url: str) -> CodexInstaller:
    codex = env / "codex"
    codex.mkdir(parents=True, exist_ok=True)
    # API-key auth so we don't take the chatgpt path
    (codex / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-test"}))
    (codex / "config.toml").write_text(
        'model_provider = "packy"\n\n'
        '[model_providers.packy]\n'
        'name = "PackyCode"\n'
        f'base_url = "{base_url}"\n'
        'wire_api = "responses"\n'
    )
    return CodexInstaller(
        proxy_url="http://127.0.0.1:7171",
        config_path=codex / "config.toml",
        auth_json_path=codex / "auth.json",
    )


def test_codex_captures_custom_relay(env) -> None:
    inst = _codex_with_custom_provider(env, "https://relay.packy.io/v1")
    inst.install()
    text = (env / "codex" / "config.toml").read_text()
    assert "/_h/codex/upstreams/codex-upstream/v1" in text
    up = load_config().upstreams["codex-upstream"]
    assert up.url == "https://relay.packy.io"   # /v1 stripped
    assert up.via == "codex"
    assert up.protocol == "openai-chat"
    # uninstall reverts the captured slug
    inst.uninstall()
    assert "codex-upstream" not in load_config().upstreams


def test_codex_official_openai_not_captured(env) -> None:
    inst = _codex_with_custom_provider(env, "https://api.openai.com/v1")
    inst.install()
    text = (env / "codex" / "config.toml").read_text()
    assert "codex-upstream" not in text          # fell back to fixed openai slug
    assert "/_h/codex/upstreams/openai/v1" in text
    assert "codex-upstream" not in load_config().upstreams
