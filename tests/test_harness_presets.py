"""Unit tests for harness preset support (no network).

Covers:
- ``load_harness`` resolves each canonical name to its independent plugin
- factory construction of each ``TelosTransport`` preset (no requests sent)
- dashboard display names

Aliases were removed in 2026-05 (Claude Code and Hermes split into separate
plugins/installers). This file no longer tests alias resolution.
"""

from __future__ import annotations

import os

from telos.harness.claude_code import ClaudeCodePlugin
from telos.harness.hermes import HermesPlugin
from telos.harness.openclaw import OpenClawPlugin
from telos.proxy.pipeline import process_anthropic_request
from telos.registry import harness_display_name, load_harness
from telos.scripts.transport import PRESETS, TelosTransport


# Provide a fake API key for transport construction. The transport never
# actually sends a request in these tests; we only verify factory plumbing.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


def test_load_harness_returns_independent_plugins() -> None:
    assert isinstance(load_harness("claude-code"), ClaudeCodePlugin)
    assert isinstance(load_harness("hermes"), HermesPlugin)
    assert isinstance(load_harness("openclaw"), OpenClawPlugin)
    # Each is a *distinct* class — no alias collapsing.
    assert type(load_harness("claude-code")) is not type(load_harness("hermes"))
    print("✓ test_load_harness_returns_independent_plugins")


def test_harness_display_name() -> None:
    assert harness_display_name("claude-code") == "Claude Code"
    assert harness_display_name("hermes") == "Hermes"
    assert harness_display_name("openclaw") == "OpenClaw"
    assert harness_display_name("codex") == "Codex"
    assert harness_display_name("kimi-code") == "Kimi Code"
    # Unknown / sentinel names pass through unchanged so the dashboard can
    # surface anonymous or untagged traffic without a lookup miss.
    assert harness_display_name("passthrough") == "passthrough"
    assert harness_display_name("?") == "?"
    print("✓ test_harness_display_name")


def test_load_harness_rejects_unknown() -> None:
    try:
        load_harness("does-not-exist")
    except ValueError as e:
        assert "Unknown harness plugin" in str(e)
        print("✓ test_load_harness_rejects_unknown")
        return
    raise AssertionError("expected ValueError for unknown harness")


def test_preset_table_has_independent_entries() -> None:
    """Each preset name maps to a distinct ``harness_name`` (no alias coupling)."""
    assert set(PRESETS) == {"openclaw", "hermes", "claude-code"}
    assert PRESETS["claude-code"].harness_name == "claude-code"
    assert PRESETS["hermes"].harness_name == "hermes"
    assert PRESETS["openclaw"].harness_name == "openclaw"
    print("✓ test_preset_table_has_independent_entries")


def test_transport_for_harness_constructs_each_preset() -> None:
    for name in ("openclaw", "hermes", "claude-code"):
        t = TelosTransport.for_harness(name)
        assert t.preset.harness_name == name
        # Sanity-check the inner transport is reachable.
        assert t.messages is not None
    print("✓ test_transport_for_harness_constructs_each_preset")


def test_pipeline_claude_code_harness_name_kept() -> None:
    """Passing ``harness_name="claude-code"`` is no longer normalized away —
    the canonical name flows straight into ``result.harness``."""
    req = {
        "model": "claude-opus-4-7",
        "max_tokens": 16,
        "system": [{"type": "text", "text": "hi"}],
        "messages": [{"role": "user",
                      "content": [{"type": "text", "text": "x"}]}],
    }
    r = process_anthropic_request(
        req, session_id="t-cc-name", harness_name="claude-code",
    )
    assert r.harness == "claude-code", \
        f"expected 'claude-code', got {r.harness!r}"
    print("✓ test_pipeline_claude_code_harness_name_kept")


def main() -> None:
    test_load_harness_returns_independent_plugins()
    test_harness_display_name()
    test_load_harness_rejects_unknown()
    test_preset_table_has_independent_entries()
    test_transport_for_harness_constructs_each_preset()
    test_pipeline_claude_code_harness_name_kept()
    print("\nall harness preset tests passed.")


if __name__ == "__main__":
    main()
