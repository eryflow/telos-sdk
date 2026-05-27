"""Codex installer tests."""

from __future__ import annotations

from pathlib import Path

from telos.init import INSTALLERS
from telos.init.codex import CodexInstaller


def test_codex_installer_registered() -> None:
    inst = INSTALLERS["codex"](proxy_url="http://h:1")
    assert isinstance(inst, CodexInstaller)
    print("✓ test_codex_installer_registered")


def test_install_adds_provider_and_preserves_previous(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        'model = "gpt-5.5"\n'
        'model_provider = "openai"\n'
        "\n"
        "[projects.\"/tmp/repo\"]\n"
        'trust_level = "trusted"\n',
        encoding="utf-8",
    )
    inst = CodexInstaller(proxy_url="http://127.0.0.1:7171",
                          config_path=config)
    r = inst.install()
    text = config.read_text(encoding="utf-8")
    assert config in r.changed_files
    assert 'model_provider = "telos"' in text
    assert '# telos_previous_model_provider = model_provider = "openai"' in text
    assert "[model_providers.telos]" in text
    assert 'base_url = "http://127.0.0.1:7171/upstreams/openai/v1"' in text
    assert 'wire_api = "responses"' in text
    assert 'requires_openai_auth = true' in text
    assert text.index('model_provider = "telos"') < text.index("[projects.")
    print("✓ test_install_adds_provider_and_preserves_previous")


def test_install_is_idempotent(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    inst = CodexInstaller(proxy_url="http://h:1", config_path=config)
    inst.install()
    once = config.read_text(encoding="utf-8")
    r = inst.install()
    twice = config.read_text(encoding="utf-8")
    assert once == twice
    assert r.already_installed
    print("✓ test_install_is_idempotent")


def test_uninstall_restores_previous(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('model_provider = "openai"\nmodel = "gpt-5.5"\n',
                      encoding="utf-8")
    inst = CodexInstaller(proxy_url="http://h:1", config_path=config)
    inst.install()
    r = inst.uninstall()
    text = config.read_text(encoding="utf-8")
    assert config in r.changed_files
    assert 'model_provider = "openai"' in text
    assert "[model_providers.telos]" not in text
    assert "telos managed codex" not in text
    print("✓ test_uninstall_restores_previous")


def test_status_reports_connected(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    inst = CodexInstaller(proxy_url="http://h:1", config_path=config)
    inst.install()
    r = inst.status()
    assert r.already_installed
    assert any("wire_api=responses" in n for n in r.notes)
    print("✓ test_status_reports_connected")


def main() -> None:
    test_codex_installer_registered()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        test_install_adds_provider_and_preserves_previous(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_install_is_idempotent(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_uninstall_restores_previous(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_status_reports_connected(Path(d))
    print("\nall codex installer tests passed.")


if __name__ == "__main__":
    main()
