"""``telos.harnesses`` tests: spec registry / executable resolution / detection."""

from __future__ import annotations

from pathlib import Path

import telos.harnesses as h


def test_specs_present() -> None:
    for name in ("claude-code", "codex", "openclaw", "hermes"):
        spec = h.get_spec(name)
        assert spec.name == name
        assert spec.env_var in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL")
    print("✓ test_specs_present")


def test_get_spec_unknown_raises() -> None:
    try:
        h.get_spec("nope")
    except ValueError:
        print("✓ test_get_spec_unknown_raises")
        return
    raise AssertionError("expected ValueError")


def test_resolve_executable_override() -> None:
    spec = h.get_spec("openclaw")
    assert h.resolve_executable(spec) == "openclaw"
    assert h.resolve_executable(spec, {"openclaw": "openclaw-beta"}) == "openclaw-beta"
    print("✓ test_resolve_executable_override")


def test_gateway_env() -> None:
    assert h.gateway_env(h.get_spec("codex"), "http://x") == {
        "OPENAI_BASE_URL": "http://x"}
    assert h.gateway_env(h.get_spec("hermes"), "http://x") == {
        "ANTHROPIC_BASE_URL": "http://x"}
    print("✓ test_gateway_env")


def test_detect_installed(monkeypatch=None) -> None:
    # monkeypatch shutil.which: only let the "claude" executable name "exist".
    import telos.harnesses as mod

    real_which = mod.shutil.which
    real_fallback = mod._fallback_executable_candidates
    mod.shutil.which = lambda name: "/usr/bin/" + name if name == "claude" else None
    mod._fallback_executable_candidates = lambda spec: ()
    try:
        found = mod.detect_installed()
        names = {s.name for s in found}
        assert names == {"claude-code"}, names
    finally:
        mod.shutil.which = real_which
        mod._fallback_executable_candidates = real_fallback
    print("✓ test_detect_installed")


def test_detect_codex_app_fallback(monkeypatch=None) -> None:
    import telos.harnesses as mod

    real_which = mod.shutil.which
    real_fallback = mod._fallback_executable_candidates
    mod.shutil.which = lambda name: None
    mod._fallback_executable_candidates = (
        lambda spec: (Path("/tmp/codex-present"),) if spec.name == "codex" else ()
    )
    real_exists = Path.exists
    real_is_file = Path.is_file
    Path.exists = lambda self: str(self) == "/tmp/codex-present"
    Path.is_file = lambda self: str(self) == "/tmp/codex-present"
    try:
        found = mod.detect_installed()
        names = {s.name for s in found}
        assert names == {"codex"}, names
    finally:
        mod.shutil.which = real_which
        mod._fallback_executable_candidates = real_fallback
        Path.exists = real_exists
        Path.is_file = real_is_file
    print("✓ test_detect_codex_app_fallback")


def main() -> None:
    test_specs_present()
    test_get_spec_unknown_raises()
    test_resolve_executable_override()
    test_gateway_env()
    test_detect_installed()
    test_detect_codex_app_fallback()
    print("\nall harnesses tests passed.")


if __name__ == "__main__":
    main()
