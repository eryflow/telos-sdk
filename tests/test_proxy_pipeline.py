"""Pure-function unit tests for ``telos.proxy.pipeline`` (no network)."""

from __future__ import annotations

from telos.bridge import BridgeSessionState
from telos.proxy.pipeline import process_anthropic_request
from telos.scripts.telos_anthropic_transport import _detect_harness


_OPENCLAW_REQ = {
    "model": "claude-opus-4-7",
    "max_tokens": 1024,
    "tools": [
        {"name": "Read", "input_schema": {"type": "object",
                                            "properties": {"path": {"type": "string"}}}},
    ],
    "system": [{"type": "text", "text": "You are an engineer agent."}],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Read login.py"}]},
    ],
    "stream": False,
}

_CLAUDE_CODE_REQ = {
    "model": "claude-opus-4-7",
    "max_tokens": 1024,
    "system": [{"type": "text",
                "text": "You are Claude Code. <system-reminder>cwd=/repo</system-reminder>"}],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Fix bug"}]},
    ],
}


def test_pipeline_detects_openclaw() -> None:
    r = process_anthropic_request(_OPENCLAW_REQ, session_id="t-oc")
    assert r.harness == "openclaw"
    assert r.wire["model"] == "claude-opus-4-7"
    assert r.wire["max_tokens"] == 1024
    assert r.wire.get("stream") is False
    print("✓ test_pipeline_detects_openclaw")


def test_pipeline_detects_claude_code() -> None:
    r = process_anthropic_request(_CLAUDE_CODE_REQ, session_id="t-h")
    assert r.harness == "claude-code"
    print("✓ test_pipeline_detects_claude_code")


def test_pipeline_injects_cache_control() -> None:
    """The tools segment or system segment must contain cache_control (an Anthropic BP marker)."""
    r = process_anthropic_request(_OPENCLAW_REQ, session_id="t-cc")
    bins = list(r.wire.get("tools", [])) + list(r.wire.get("system", []))
    assert any("cache_control" in b for b in bins), \
        f"wire has no cache_control marker at all: {r.wire}"
    assert r.plan_slots, "EmitPlan should return at least one slot"
    print(f"✓ test_pipeline_injects_cache_control (slots={r.plan_slots})")


def test_pipeline_explicit_harness_override() -> None:
    r = process_anthropic_request(_CLAUDE_CODE_REQ, session_id="t-ov",
                                   harness_name="openclaw")
    assert r.harness == "openclaw", \
        "an explicit harness_name should override auto-detection"
    print("✓ test_pipeline_explicit_harness_override")


def test_pipeline_passthrough_fields() -> None:
    req = dict(_OPENCLAW_REQ)
    req["temperature"] = 0.7
    req["top_p"] = 0.95
    req["stop_sequences"] = ["</done>"]
    r = process_anthropic_request(req, session_id="t-pt")
    assert r.wire["temperature"] == 0.7
    assert r.wire["top_p"] == 0.95
    assert r.wire["stop_sequences"] == ["</done>"]
    print("✓ test_pipeline_passthrough_fields")


def test_detect_claude_code_marker_in_user_message() -> None:
    """Claude Code injects ``<system-reminder>`` into the user message rather than system.

    The old implementation scanned only the system segment and would misclassify such requests as openclaw.
    """
    req = {
        "model": "claude-opus-4-7",
        "max_tokens": 1024,
        "system": [{"type": "text", "text": "You are an assistant."}],
        "messages": [
            {"role": "user", "content": [
                {"type": "text",
                 "text": "Fix the bug.\n<system-reminder>cwd=/repo</system-reminder>"},
            ]},
        ],
    }
    assert _detect_harness(req) == "claude-code"
    print("✓ test_detect_claude_code_marker_in_user_message")


def test_detect_claude_code_command_name_in_user_message() -> None:
    """``<command-name>`` is also a Claude Code envelope (the slash command panel)."""
    req = {
        "model": "claude-opus-4-7",
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "<command-name>/init</command-name>"},
            ]},
        ],
    }
    assert _detect_harness(req) == "claude-code"
    print("✓ test_detect_claude_code_command_name_in_user_message")


def test_detect_claude_code_via_tool_fingerprint() -> None:
    """On the first turn with no reminder and no thinking, Claude Code's tool set is also a strong fingerprint."""
    req = {
        "model": "claude-opus-4-7",
        "tools": [
            {"name": "Bash", "input_schema": {"type": "object"}},
            {"name": "Read", "input_schema": {"type": "object"}},
            {"name": "Edit", "input_schema": {"type": "object"}},
            {"name": "Write", "input_schema": {"type": "object"}},
        ],
        "system": [{"type": "text", "text": "You are Claude Code."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Hi"}]},
        ],
    }
    assert _detect_harness(req) == "claude-code"
    print("✓ test_detect_claude_code_via_tool_fingerprint")


def test_detect_openclaw_when_no_signals() -> None:
    """No envelope tags, no thinking, and tools that don't look like Claude Code → openclaw."""
    req = {
        "model": "claude-opus-4-7",
        "tools": [{"name": "search_api", "input_schema": {"type": "object"}}],
        "system": [{"type": "text", "text": "Be helpful."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
        ],
    }
    assert _detect_harness(req) == "openclaw"
    print("✓ test_detect_openclaw_when_no_signals")


def test_detect_substring_no_false_positive() -> None:
    """A bare ``<system-reminder>`` string without a closing tag should not trigger the claude-code verdict."""
    req = {
        "model": "claude-opus-4-7",
        "messages": [
            {"role": "user", "content": [
                {"type": "text",
                 "text": "Discussing the <system-reminder> tag in our docs."},
            ]},
        ],
    }
    assert _detect_harness(req) == "openclaw"
    print("✓ test_detect_substring_no_false_positive")


def test_sticky_harness_across_calls() -> None:
    """Under the same session_state, the first-turn detection result should be locked to avoid later flipping."""
    state = BridgeSessionState()
    # turn 1: a typical Claude Code request
    cc_req = {
        "model": "claude-opus-4-7",
        "max_tokens": 1024,
        "system": [{"type": "text", "text": "Claude Code system."}],
        "messages": [
            {"role": "user", "content": [
                {"type": "text",
                 "text": "Help.\n<system-reminder>x</system-reminder>"},
            ]},
        ],
    }
    r1 = process_anthropic_request(cc_req, session_id="t-sticky",
                                     session_state=state)
    assert r1.harness == "claude-code"
    assert state.sticky_harness == "claude-code"

    # turn 2: construct a request that bare detection would identify as openclaw (no claude-code fingerprint at all)
    follow_up = {
        "model": "claude-opus-4-7",
        "max_tokens": 1024,
        "system": [{"type": "text", "text": "Continue."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Next step"}]},
        ],
    }
    r2 = process_anthropic_request(follow_up, session_id="t-sticky",
                                     session_state=state)
    # the session is already locked → it should still be claude-code
    assert r2.harness == "claude-code", \
        f"sticky_harness should keep subsequent calls on claude-code, got {r2.harness}"
    print("✓ test_sticky_harness_across_calls")


def test_explicit_harness_overrides_sticky() -> None:
    """An explicit harness_name must be able to override sticky_harness."""
    state = BridgeSessionState(sticky_harness="claude-code")
    req = dict(_OPENCLAW_REQ)
    r = process_anthropic_request(req, session_id="t-ov2",
                                    session_state=state,
                                    harness_name="openclaw")
    assert r.harness == "openclaw"
    # an explicit override does **not** rewrite sticky_harness (preserving the session's "natural" detection)
    assert state.sticky_harness == "claude-code"
    print("✓ test_explicit_harness_overrides_sticky")


def main() -> None:
    test_pipeline_detects_openclaw()
    test_pipeline_detects_claude_code()
    test_pipeline_injects_cache_control()
    test_pipeline_explicit_harness_override()
    test_pipeline_passthrough_fields()
    test_detect_claude_code_marker_in_user_message()
    test_detect_claude_code_command_name_in_user_message()
    test_detect_claude_code_via_tool_fingerprint()
    test_detect_openclaw_when_no_signals()
    test_detect_substring_no_false_positive()
    test_sticky_harness_across_calls()
    test_explicit_harness_overrides_sticky()
    print("\nall pipeline tests passed.")


if __name__ == "__main__":
    main()
