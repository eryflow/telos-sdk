"""``telos.init`` env-based installers tests (openclaw / hermes).

These installers don't persist anything to disk — routing is done by the
``telos <harness>`` launcher injecting env into the subprocess. The tests
verify install/uninstall are no-ops and that ``status()`` reports the real
preconditions of the launcher path (executable presence, gateway state, live
env).
"""

from __future__ import annotations

import os
from unittest.mock import patch

from telos.init import INSTALLERS
from telos.init.anthropic_env import EnvInstaller


def test_env_installers_registered() -> None:
    for name in ("openclaw", "hermes"):
        assert name in INSTALLERS
    print("✓ test_env_installers_registered")


def test_install_changes_no_files() -> None:
    inst = EnvInstaller(name="codex", env_var="OPENAI_BASE_URL",
                        proxy_url="http://127.0.0.1:7171")
    r = inst.install()
    assert r.agent == "codex"
    assert not r.changed_files
    assert not r.backups
    # Notes should mention the launcher path and the env var.
    assert any("telos codex" in n for n in r.notes)
    assert any("OPENAI_BASE_URL" in n for n in r.notes)
    print("✓ test_install_changes_no_files")


def test_install_mentions_config_self_ownership() -> None:
    inst = EnvInstaller(name="openclaw", env_var="ANTHROPIC_BASE_URL",
                        proxy_url="http://h:1")
    r = inst.install()
    # The notes should make it clear telos won't touch ~/.openclaw/*.
    assert any("~/.openclaw" in n for n in r.notes)
    print("✓ test_install_mentions_config_self_ownership")


def test_uninstall_is_noop() -> None:
    inst = EnvInstaller(name="hermes", env_var="ANTHROPIC_BASE_URL",
                        proxy_url="http://h:1")
    r = inst.uninstall()
    assert not r.changed_files
    assert any("no persistent state" in n for n in r.notes)
    print("✓ test_uninstall_is_noop")


def test_status_reports_executable_missing() -> None:
    inst = EnvInstaller(name="openclaw", env_var="ANTHROPIC_BASE_URL",
                        proxy_url="http://127.0.0.1:7171")
    with patch("telos.init.anthropic_env.EnvInstaller._which_executable",
               return_value=None), \
         patch("telos.init.anthropic_env.EnvInstaller._gateway_url",
               return_value=None):
        saved = os.environ.pop("ANTHROPIC_BASE_URL", None)
        try:
            r = inst.status()
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_BASE_URL"] = saved
    assert r.already_installed is False
    assert any("NOT found on PATH" in n for n in r.notes)
    assert any("not running" in n for n in r.notes)
    print("✓ test_status_reports_executable_missing")


def test_status_reports_executable_present_no_gateway() -> None:
    inst = EnvInstaller(name="openclaw", env_var="ANTHROPIC_BASE_URL",
                        proxy_url="http://127.0.0.1:7171")
    with patch("telos.init.anthropic_env.EnvInstaller._which_executable",
               return_value="/opt/homebrew/bin/openclaw"), \
         patch("telos.init.anthropic_env.EnvInstaller._gateway_url",
               return_value=None):
        r = inst.status()
    assert r.already_installed is True  # launcher path is wired up
    assert any("/opt/homebrew/bin/openclaw" in n for n in r.notes)
    assert any("not running" in n for n in r.notes)
    print("✓ test_status_reports_executable_present_no_gateway")


def test_status_reports_live_env_match() -> None:
    inst = EnvInstaller(name="openclaw", env_var="ANTHROPIC_BASE_URL",
                        proxy_url="http://127.0.0.1:7171")
    gateway_url = "http://127.0.0.1:7171"
    with patch("telos.init.anthropic_env.EnvInstaller._which_executable",
               return_value="/usr/bin/openclaw"), \
         patch("telos.init.anthropic_env.EnvInstaller._gateway_url",
               return_value=gateway_url), \
         patch.dict(os.environ, {"ANTHROPIC_BASE_URL": gateway_url}, clear=False):
        r = inst.status()
    assert any("points at telos" in n for n in r.notes)
    print("✓ test_status_reports_live_env_match")


def test_status_reports_live_env_mismatch() -> None:
    inst = EnvInstaller(name="openclaw", env_var="ANTHROPIC_BASE_URL",
                        proxy_url="http://127.0.0.1:7171")
    with patch("telos.init.anthropic_env.EnvInstaller._which_executable",
               return_value="/usr/bin/openclaw"), \
         patch("telos.init.anthropic_env.EnvInstaller._gateway_url",
               return_value="http://127.0.0.1:7171"), \
         patch.dict(os.environ,
                    {"ANTHROPIC_BASE_URL": "http://elsewhere:9999"},
                    clear=False):
        r = inst.status()
    assert any("NOT the telos gateway" in n for n in r.notes)
    print("✓ test_status_reports_live_env_mismatch")


def test_factory_from_registry() -> None:
    """openclaw / hermes use config-patching installers in the registry."""
    from telos.init.hermes import HermesInstaller
    from telos.init.openclaw import OpenClawInstaller
    assert isinstance(INSTALLERS["openclaw"](proxy_url="http://h:1"),
                       OpenClawInstaller)
    assert isinstance(INSTALLERS["hermes"](proxy_url="http://h:1"),
                       HermesInstaller)
    print("✓ test_factory_from_registry")


def main() -> None:
    test_env_installers_registered()
    test_install_changes_no_files()
    test_install_mentions_config_self_ownership()
    test_uninstall_is_noop()
    test_status_reports_executable_missing()
    test_status_reports_executable_present_no_gateway()
    test_status_reports_live_env_match()
    test_status_reports_live_env_mismatch()
    test_factory_from_registry()
    print("\nall env installer tests passed.")


if __name__ == "__main__":
    main()
