"""Kimi Code installer tests against an isolated config.toml."""

from __future__ import annotations

import os

from telos.config import load_config
from telos.init.kimi_code import KimiCodeInstaller, _active_provider


_CONFIG = '''default_model = "kimi/k3"

[providers.kimi]
type = "kimi"
api_key = "test-key"
base_url = "https://api.kimi.com/coding/v1"

[models."kimi/k3"]
provider = "kimi"
model = "k3"
max_context_size = 1048576

[[hooks]]
event = "Notification"
command = "notify-existing"
timeout = 10
'''

_MANAGED_CONFIG = _CONFIG.replace('default_model = "kimi/k3"', 'default_model = "kimi-code/k3"') \
    .replace('[providers.kimi]', '[providers."managed:kimi-code"]') \
    .replace('api_key = "test-key"', 'api_key = ""\noauth = { storage = "file", key = "kimi-code" }') \
    .replace('[models."kimi/k3"]', '[models."kimi-code/k3"]') \
    .replace('provider = "kimi"', 'provider = "managed:kimi-code"')


def test_kimi_installer_round_trip_and_idempotency(tmp_path) -> None:
    previous = os.environ.get("TELOS_HOME")
    os.environ["TELOS_HOME"] = str(tmp_path / ".telos")
    try:
        config = tmp_path / "config.toml"
        state = tmp_path / "kimi-state.json"
        config.write_text(_CONFIG)
        installer = KimiCodeInstaller(config_path=config, state_path=state)

        first = installer.install()
        installed = config.read_text()
        provider, route = _active_provider(installed)
        assert provider == "kimi"
        assert route == installer.route
        assert installed.count("telos trace-hook kimi-code") == 15
        assert "notify-existing" in installed
        assert state.exists()
        assert config.with_suffix(".toml.telos.bak").exists()

        upstream = load_config().upstreams["kimi-code-upstream"]
        assert upstream.url == "https://api.kimi.com/coding"
        assert upstream.protocol == "openai-chat"
        assert upstream.via == "kimi-code"
        assert first.changed_files

        second = installer.install()
        assert second.already_installed is True
        assert config.read_text() == installed

        removed = installer.uninstall()
        restored = config.read_text()
        assert _active_provider(restored)[1] == "https://api.kimi.com/coding/v1"
        assert "telos trace-hook kimi-code" not in restored
        assert "notify-existing" in restored
        assert "kimi-code-upstream" not in load_config().upstreams
        assert removed.changed_files
    finally:
        if previous is None:
            os.environ.pop("TELOS_HOME", None)
        else:
            os.environ["TELOS_HOME"] = previous


def test_kimi_status_reports_partial_config(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_CONFIG)
    status = KimiCodeInstaller(
        config_path=config, state_path=tmp_path / "state.json"
    ).status()
    assert status.already_installed is False
    assert "incomplete" in status.notes[0]


def test_kimi_managed_oauth_keeps_official_url(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_MANAGED_CONFIG)
    installer = KimiCodeInstaller(
        config_path=config, state_path=tmp_path / "state.json"
    )

    installer.install()

    assert _active_provider(config.read_text())[1] == "https://api.kimi.com/coding/v1"
    assert config.read_text().count("telos trace-hook kimi-code") == 15
    assert installer.status().already_installed is True

    config.write_text(config.read_text().replace(
        "https://api.kimi.com/coding/v1", installer.route
    ))
    installer.install()
    assert _active_provider(config.read_text())[1] == "https://api.kimi.com/coding/v1"
