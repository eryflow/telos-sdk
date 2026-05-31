"""``telos.init.claude_code`` installer tests (operating on an isolated temporary settings.json)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from telos.init.claude_code import ClaudeCodeInstaller


def _new_settings_path() -> Path:
    return Path(tempfile.mkdtemp(prefix="telos-claude-")) / "settings.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def test_install_on_missing_file() -> None:
    p = _new_settings_path()
    inst = ClaudeCodeInstaller(settings_path=p, proxy_url="http://127.0.0.1:7171")
    r = inst.install()
    assert r.already_installed is False
    assert p in r.changed_files
    data = _read(p)
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:7171/_h/claude-code"
    assert data["env"]["__telos_installed"] is True
    print("✓ test_install_on_missing_file")


def test_install_preserves_existing_settings() -> None:
    p = _new_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "permissions": {"defaultMode": "ask"},
        "env": {"FOO": "bar"},
    }))
    inst = ClaudeCodeInstaller(settings_path=p, proxy_url="http://127.0.0.1:7171")
    r = inst.install()
    assert r.backups, "should generate a .telos.bak backup"
    data = _read(p)
    assert data["permissions"]["defaultMode"] == "ask"  # untouched
    assert data["env"]["FOO"] == "bar"
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:7171/_h/claude-code"
    print("✓ test_install_preserves_existing_settings")


def test_install_preserves_user_anthropic_base_url() -> None:
    p = _new_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://my.proxy/"}}))
    inst = ClaudeCodeInstaller(settings_path=p, proxy_url="http://127.0.0.1:7171")
    inst.install()
    data = _read(p)
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:7171/_h/claude-code"
    assert data["env"]["__telos_previous_base_url"] == "https://my.proxy/"
    print("✓ test_install_preserves_user_anthropic_base_url")


def test_install_is_idempotent() -> None:
    p = _new_settings_path()
    inst = ClaudeCodeInstaller(settings_path=p)
    inst.install()
    r2 = inst.install()
    assert r2.already_installed is True
    assert not r2.changed_files
    print("✓ test_install_is_idempotent")


def test_uninstall_restores_state() -> None:
    p = _new_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://my.proxy/"}}))
    inst = ClaudeCodeInstaller(settings_path=p, proxy_url="http://127.0.0.1:7171")
    inst.install()
    inst.uninstall()
    data = _read(p)
    assert data["env"]["ANTHROPIC_BASE_URL"] == "https://my.proxy/"
    assert "__telos_installed" not in data["env"]
    assert "__telos_previous_base_url" not in data["env"]
    print("✓ test_uninstall_restores_state")


def test_uninstall_removes_env_block_if_empty() -> None:
    p = _new_settings_path()
    inst = ClaudeCodeInstaller(settings_path=p)
    inst.install()
    inst.uninstall()
    data = _read(p)
    assert "env" not in data
    print("✓ test_uninstall_removes_env_block_if_empty")


def test_status_reports_installed() -> None:
    p = _new_settings_path()
    inst = ClaudeCodeInstaller(settings_path=p)
    pre = inst.status()
    assert pre.already_installed is False
    inst.install()
    post = inst.status()
    assert post.already_installed is True
    print("✓ test_status_reports_installed")


def test_install_rejects_non_object_env() -> None:
    p = _new_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"env": "not-an-object"}))
    inst = ClaudeCodeInstaller(settings_path=p)
    try:
        inst.install()
    except RuntimeError as e:
        assert "env" in str(e)
        print("✓ test_install_rejects_non_object_env")
        return
    raise AssertionError("expected RuntimeError")


def test_install_preserves_other_tool_with_same_url() -> None:
    """Regression: when another tool sets ANTHROPIC_BASE_URL to the same URL as ours,

    install must still record it into __telos_previous_base_url so that uninstall
    can restore it, without deleting the other tool's configuration along with it.
    """
    p = _new_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    same = "http://127.0.0.1:7171/_h/claude-code"
    p.write_text(json.dumps({"env": {
        "ANTHROPIC_BASE_URL": same, "__other_tool": "true"}}))
    inst = ClaudeCodeInstaller(settings_path=p, proxy_url="http://127.0.0.1:7171")
    inst.install()
    data = _read(p)
    assert data["env"]["__telos_previous_base_url"] == same
    inst.uninstall()
    data = _read(p)
    assert data["env"]["ANTHROPIC_BASE_URL"] == same  # the other tool's config is restored intact
    assert data["env"]["__other_tool"] == "true"
    print("✓ test_install_preserves_other_tool_with_same_url")


def test_reinstall_with_new_url_keeps_original_previous() -> None:
    """Regression: when telos is reinstalled with a new proxy_url, it must not overwrite

    __telos_previous_base_url with telos's own old value (that would lose the true original value).
    """
    p = _new_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://orig/"}}))
    ClaudeCodeInstaller(settings_path=p, proxy_url="http://127.0.0.1:7171").install()
    # install again with a different url
    ClaudeCodeInstaller(settings_path=p, proxy_url="http://127.0.0.1:9999").install()
    data = _read(p)
    assert data["env"]["__telos_previous_base_url"] == "https://orig/"
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999/_h/claude-code"
    print("✓ test_reinstall_with_new_url_keeps_original_previous")


def main() -> None:
    test_install_on_missing_file()
    test_install_preserves_existing_settings()
    test_install_preserves_user_anthropic_base_url()
    test_install_preserves_other_tool_with_same_url()
    test_reinstall_with_new_url_keeps_original_previous()
    test_install_is_idempotent()
    test_uninstall_restores_state()
    test_uninstall_removes_env_block_if_empty()
    test_status_reports_installed()
    test_install_rejects_non_object_env()
    print("\nall init/claude-code tests passed.")


if __name__ == "__main__":
    main()
