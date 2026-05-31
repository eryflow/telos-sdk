"""Regression test: Claude Code / Hermes-style multi-content-block user messages.

§5 ordering invariant: in the message segment, ``tool_result`` blocks must physically
come first, followed by ``pin* → fold* → drop*``.

Historical bug: the harness classifies ``tool_result`` as FOLD and the user's question text
as PIN; ``enforce_band_order`` sorting purely by band would place tool_result after the text,
whereas Anthropic requires the tool_result of a user message to come physically first → API 400.
"""

from __future__ import annotations

from telos import Band, Bridge, load_engine, load_harness
from telos.ir import assert_ir_invariants
from telos.proxy.pipeline import process_anthropic_request


_CLAUDE_CODE_USER_MSG = {
    "role": "user",
    "content": [
        # the tool_result from the previous turn (FOLD) -- comes first in source order
        {"type": "tool_result", "tool_use_id": "toolu_x",
         "content": [{"type": "text", "text": "<file content...>"}]},
        # this turn's new question (PIN) + envelope (DROP)
        {"type": "text",
         "text": "Please continue editing\n<system-reminder>cwd=/repo</system-reminder>"},
    ],
}

_CLAUDE_CODE_REQ = {
    "model": "claude-opus-4-7",
    "max_tokens": 256,
    "system": [{"type": "text", "text": "You are Claude Code."}],
    "messages": [_CLAUDE_CODE_USER_MSG],
}


def _assert_tool_result_first(msg, where: str) -> None:
    """Assert that within a TelosMessage all tool_result blocks come before non-tool_result blocks."""
    kinds = [b.kind for b in msg.blocks]
    tr_idx = [i for i, k in enumerate(kinds) if k == "tool_result"]
    if tr_idx:
        assert tr_idx == list(range(len(tr_idx))), \
            f"{where}: tool_result not contiguous-at-front: {kinds}"


def test_claude_code_tool_result_stays_first() -> None:
    harness = load_harness("claude-code")
    ir = harness.parse(_CLAUDE_CODE_REQ, session_id="r-h",
                        engine="anthropic", model="claude-opus-4-7")
    assert_ir_invariants(ir)  # §5 invariant must hold

    user_msg = ir.messages[0]
    # tool_result must be the first block -- a hard constraint of the Anthropic protocol
    assert user_msg.blocks[0].kind == "tool_result", \
        f"first block must be tool_result, got {[b.kind for b in user_msg.blocks]}"
    _assert_tool_result_first(user_msg, "claude-code user_msg")
    # tool_result is still the FOLD band (band membership is unchanged, only the physical order)
    assert user_msg.blocks[0].band is Band.FOLD
    print("✓ test_claude_code_tool_result_stays_first")


def test_openclaw_tool_result_stays_first() -> None:
    harness = load_harness("openclaw")
    ir = harness.parse(_CLAUDE_CODE_REQ, session_id="r-o",
                        engine="anthropic", model="claude-opus-4-7")
    assert_ir_invariants(ir)
    assert ir.messages[0].blocks[0].kind == "tool_result"
    print("✓ test_openclaw_tool_result_stays_first")


def test_wire_user_message_leads_with_tool_result() -> None:
    """End-to-end: run the full TELOS pipeline; the user message in the wire must have tool_result first.

    This is a direct regression for the Anthropic 400 "tool use concurrency" error -- before the fix
    the wire would place tool_result after the text.
    """
    res = process_anthropic_request(
        _CLAUDE_CODE_REQ, session_id="r-w", session_state=None, harness_name=None,
    )
    for i, m in enumerate(res.wire["messages"]):
        content = m.get("content")
        if not isinstance(content, list):
            continue
        types = [b.get("type") for b in content if isinstance(b, dict)]
        tr_idx = [j for j, t in enumerate(types) if t == "tool_result"]
        if tr_idx:
            assert tr_idx == list(range(len(tr_idx))), \
                f"wire msg{i}: tool_result not first: {types}"
    print("✓ test_wire_user_message_leads_with_tool_result")


def test_bridge_accepts_reordered_message() -> None:
    """Bridge construction should not raise TelosInvariantError."""
    harness = load_harness("claude-code")
    engine = load_engine("anthropic")
    ir = harness.parse(_CLAUDE_CODE_REQ, session_id="r-b",
                        engine="anthropic", model="claude-opus-4-7")
    bridge = Bridge(ir, engine)
    plan = bridge.mark()
    assert plan is not None
    print("✓ test_bridge_accepts_reordered_message")


def test_deeply_interleaved_content() -> None:
    """Pathological case: text, tool_result, text, text, tool_result, text.

    Each text splits into PIN+DROP, each tool_result is FOLD. After reordering it must be
    tool_result* first, followed by non-tool_result blocks in pin* → fold* → drop* order.
    """
    req = {
        "model": "claude-opus-4-7",
        "max_tokens": 256,
        "system": [{"type": "text", "text": "agent"}],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "q1\nCurrent time: 2026"},
                {"type": "tool_result", "tool_use_id": "a", "content": "r1"},
                {"type": "text", "text": "q2\nCurrent time: 2026"},
                {"type": "text", "text": "q3"},
                {"type": "tool_result", "tool_use_id": "b", "content": "r2"},
                {"type": "text", "text": "q4\n<system-reminder>x</system-reminder>"},
            ],
        }],
    }
    harness = load_harness("claude-code")
    ir = harness.parse(req, session_id="r-d", engine="anthropic")
    assert_ir_invariants(ir)

    msg = ir.messages[0]
    _assert_tool_result_first(msg, "interleaved user_msg")
    # non-tool_result blocks are still strictly pin* → fold* → drop*
    rest = [b.band.value for b in msg.blocks if b.kind != "tool_result"]
    assert rest == sorted(rest, key=lambda b: {"pin": 0, "fold": 1, "drop": 2}[b]), \
        f"non-tool_result bands not sorted: {rest}"
    kinds = [b.kind for b in msg.blocks]
    print(f"✓ test_deeply_interleaved_content (kinds={kinds})")


def main() -> None:
    test_claude_code_tool_result_stays_first()
    test_openclaw_tool_result_stays_first()
    test_wire_user_message_leads_with_tool_result()
    test_bridge_accepts_reordered_message()
    test_deeply_interleaved_content()
    print("\nall band-order regression tests passed.")


if __name__ == "__main__":
    main()
