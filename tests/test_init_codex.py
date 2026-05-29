"""Codex installer tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

from telos.init import INSTALLERS
from telos.init.codex import CodexInstaller, _detect_auth_mode


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


def test_detect_auth_mode(tmp_path: Path) -> None:
    """The detector reads ``auth.json`` and reports the codex login state.

    Codex.app users typically have ``auth_mode=chatgpt`` and a null
    ``OPENAI_API_KEY``; older builds set only the field that matters.
    """
    auth = tmp_path / "auth.json"
    assert _detect_auth_mode(auth) == "unknown"
    auth.write_text(json.dumps({"auth_mode": "chatgpt",
                                "OPENAI_API_KEY": None,
                                "tokens": {"access_token": "jwt..."}}),
                    encoding="utf-8")
    assert _detect_auth_mode(auth) == "chatgpt"
    auth.write_text(json.dumps({"auth_mode": "apikey",
                                "OPENAI_API_KEY": "sk-..."}),
                    encoding="utf-8")
    assert _detect_auth_mode(auth) == "apikey"
    # Older builds with no auth_mode but tokens set → chatgpt.
    auth.write_text(json.dumps({"tokens": {"access_token": "jwt..."}}),
                    encoding="utf-8")
    assert _detect_auth_mode(auth) == "chatgpt"
    # Older builds with only OPENAI_API_KEY → apikey.
    auth.write_text(json.dumps({"OPENAI_API_KEY": "sk-..."}),
                    encoding="utf-8")
    assert _detect_auth_mode(auth) == "apikey"
    # Garbage in the file → unknown (and never raises).
    auth.write_text("not json", encoding="utf-8")
    assert _detect_auth_mode(auth) == "unknown"
    print("✓ test_detect_auth_mode")


def test_install_chatgpt_mode_routes_to_backend_codex(tmp_path: Path) -> None:
    """Regression for "codex still doesn't work": when Codex.app uses
    ChatGPT login, forwarding to api.openai.com 403s ("Missing scopes:
    api.model.read"). The installer must instead route to
    ``chatgpt.com/backend-api/codex`` and register the matching upstream
    slug in ``~/.telos/config.json``.
    """
    # Isolate ~/.telos for this test so we don't clobber the developer's
    # real config.
    telos_home = tmp_path / "telos"
    config = tmp_path / "config.toml"
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {"access_token": "jwt..."},
    }), encoding="utf-8")

    prev_telos_home = os.environ.get("TELOS_HOME")
    os.environ["TELOS_HOME"] = str(telos_home)
    try:
        inst = CodexInstaller(
            proxy_url="http://127.0.0.1:7171",
            config_path=config,
            auth_json_path=auth,
        )
        r = inst.install()
        text = config.read_text(encoding="utf-8")

        # codex points at the chatgpt slug, NOT at /upstreams/openai/v1.
        assert 'base_url = "http://127.0.0.1:7171/upstreams/codex-chatgpt"' in text, text
        assert "/upstreams/openai/v1" not in text

        # The telos config now has a codex-chatgpt slug pointing at backend-api/codex.
        from telos.config import load_config
        cfg = load_config()
        assert "codex-chatgpt" in cfg.upstreams
        slug = cfg.upstreams["codex-chatgpt"]
        assert slug.url == "https://chatgpt.com/backend-api/codex"
        assert slug.protocol == "openai-chat"
        assert slug.via == "codex"

        # The notes surface the routing decision (so `telos init` output explains
        # to the user why their config differs from the default install).
        assert any("ChatGPT login" in n for n in r.notes), r.notes
    finally:
        if prev_telos_home is None:
            os.environ.pop("TELOS_HOME", None)
        else:
            os.environ["TELOS_HOME"] = prev_telos_home
    print("✓ test_install_chatgpt_mode_routes_to_backend_codex")


def test_uninstall_chatgpt_mode_removes_telos_upstream(tmp_path: Path) -> None:
    """Regression: ChatGPT-mode install adds a ``codex-chatgpt`` slug to
    ``~/.telos/config.json``. Uninstall must remove it; before the fix the
    slug stayed forever and ``telos init codex --uninstall`` left a dangling
    ``via=codex`` upstream that the gateway kept forwarding to chatgpt.com.
    """
    telos_home = tmp_path / "telos"
    config = tmp_path / "config.toml"
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {"access_token": "jwt..."},
    }), encoding="utf-8")

    prev_telos_home = os.environ.get("TELOS_HOME")
    os.environ["TELOS_HOME"] = str(telos_home)
    try:
        inst = CodexInstaller(
            proxy_url="http://127.0.0.1:7171",
            config_path=config,
            auth_json_path=auth,
        )
        inst.install()

        from telos.config import load_config
        cfg_before = load_config()
        assert "codex-chatgpt" in cfg_before.upstreams

        r = inst.uninstall()
        # config.toml is restored.
        text = config.read_text(encoding="utf-8")
        assert "[model_providers.telos]" not in text
        assert "telos managed codex" not in text

        # ~/.telos/config.json has the codex-chatgpt slug stripped, AND the
        # uninstall notes mention it (so the user can see what was changed).
        cfg_after = load_config()
        assert "codex-chatgpt" not in cfg_after.upstreams, cfg_after.upstreams
        assert any("codex-chatgpt" in n for n in r.notes), r.notes
    finally:
        if prev_telos_home is None:
            os.environ.pop("TELOS_HOME", None)
        else:
            os.environ["TELOS_HOME"] = prev_telos_home
    print("✓ test_uninstall_chatgpt_mode_removes_telos_upstream")


def test_install_tolerates_missing_trailing_newline(tmp_path: Path) -> None:
    """Repro for the real-world case where codex's own config rewriter drops
    the final ``\\n``. Before the fix this raised ``"found a TELOS managed
    Codex block without its end marker"`` and silently no-op'd the install."""
    config = tmp_path / "config.toml"
    body = (
        "# >>> telos managed codex root\n"
        "# telos_previous_model_provider = <absent>\n"
        'model_provider = "telos"\n'
        "# <<< telos managed codex root\n"
        "\n"
        "# >>> telos managed codex provider\n"
        "[model_providers.telos]\n"
        'name = "TELOS Gateway"\n'
        'base_url = "http://127.0.0.1:7171/upstreams/openai/v1"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
        "# <<< telos managed codex provider"  # no trailing newline!
    )
    config.write_text(body)
    inst = CodexInstaller(proxy_url="http://127.0.0.1:7171", config_path=config)
    r = inst.install()
    assert r.changed_files or r.already_installed, r.notes
    text = config.read_text()
    assert "model_provider = \"telos\"" in text
    assert text.endswith("\n"), repr(text[-5:])
    print("✓ test_install_tolerates_missing_trailing_newline")


def test_install_preserves_foreign_content_inside_provider_block(tmp_path: Path) -> None:
    """When codex moves unrelated TOML (notify, [projects.…], mcp servers)
    inside our managed region — observed in the wild after codex's UI
    rewrites the file — the next ``telos init`` must not delete it."""
    config = tmp_path / "config.toml"
    body = (
        "# >>> telos managed codex root\n"
        "# telos_previous_model_provider = <absent>\n"
        'model_provider = "telos"\n'
        "# <<< telos managed codex root\n"
        "\n"
        "# >>> telos managed codex provider\n"
        'notify = ["/path/to/notifier", "turn-ended"]\n'
        "\n"
        "[model_providers.telos]\n"
        'name = "TELOS Gateway"\n'
        'base_url = "http://127.0.0.1:7171/upstreams/openai/v1"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
        "\n"
        '[projects."/Users/me/repo"]\n'
        'trust_level = "trusted"\n'
        "# <<< telos managed codex provider\n"
    )
    config.write_text(body)
    inst = CodexInstaller(proxy_url="http://127.0.0.1:7171", config_path=config)
    r = inst.install()
    text = config.read_text()
    # foreign content survives
    assert "notify =" in text, text
    assert '[projects."/Users/me/repo"]' in text, text
    assert 'trust_level = "trusted"' in text, text
    # telos provider table is still there
    assert "[model_providers.telos]" in text, text
    assert "/upstreams/openai/v1" in text, text
    # the foreign content is OUTSIDE the managed region after rewrite
    provider_begin = text.index("# >>> telos managed codex provider")
    provider_end = text.index("# <<< telos managed codex provider")
    block = text[provider_begin:provider_end]
    assert "notify =" not in block, block
    assert '[projects."/Users/me/repo"]' not in block, block
    assert any("foreign content" in n for n in r.notes), r.notes
    print("✓ test_install_preserves_foreign_content_inside_provider_block")


def main() -> None:
    test_codex_installer_registered()
    test_detect_auth_mode_with_tmp()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        test_install_adds_provider_and_preserves_previous(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_install_is_idempotent(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_uninstall_restores_previous(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_status_reports_connected(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_install_chatgpt_mode_routes_to_backend_codex(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_uninstall_chatgpt_mode_removes_telos_upstream(Path(d))
    print("\nall codex installer tests passed.")


def test_detect_auth_mode_with_tmp() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        test_detect_auth_mode(Path(d))


if __name__ == "__main__":
    main()
