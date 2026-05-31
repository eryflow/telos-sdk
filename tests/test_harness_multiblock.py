"""harness §5 ordering regression: a multi-content-block user message must produce a valid IR.

The real bug targeted: Claude Code / Hermes user messages are often multi-part structures
(text + tool_result + image), and each text carries its own envelope. The old code expanded
each content item separately into ``(PIN, FOLD*, DROP*)``, then concatenated with ``extend``,
yielding ``PIN, DROP, PIN, DROP, ...`` -- which violates ``pin* → fold* → drop*``.

After the fix: the harness applies ``enforce_band_order`` as a fallback at the message level,
ensuring §5 holds.
"""

from __future__ import annotations

from telos import Band, Bridge, load_engine, load_harness


def _user_with_n_text_blocks(n: int) -> dict:
    """Constructs a user message containing n text content blocks,
    each carrying its own ``<environment_info>`` envelope (a DROP candidate)."""
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 64,
        "system": [{"type": "text", "text": "You are an agent."}],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": f"Question {i}. "
                         f"<environment_info>turn={i}</environment_info>"}
                for i in range(n)
            ],
        }],
    }


def _assert_band_order(blocks) -> None:
    """§5 ordering: tool_result* → pin* → fold* → drop* (tool_result is treated as rank -1)."""
    rank = {Band.PIN: 0, Band.FOLD: 1, Band.DROP: 2}
    last = -2
    for b in blocks:
        r = -1 if b.kind == "tool_result" else rank[b.band]
        assert r >= last, f"violation: {b.id} band={b.band} after rank={last}"
        last = r


def test_openclaw_multiple_text_blocks() -> None:
    h = load_harness("openclaw")
    for n in (2, 4, 13):  # 13 simulates the first message of a real long Claude Code conversation
        ir = h.parse(_user_with_n_text_blocks(n), session_id=f"oc-{n}",
                     engine="anthropic", model="claude-opus-4-7")
        msg = ir.messages[0]
        _assert_band_order(msg.blocks)
        # the Bridge constructor re-validates all IR invariants
        Bridge(ir, load_engine("anthropic"))
    print("✓ test_openclaw_multiple_text_blocks")


def test_claude_code_multiple_text_blocks() -> None:
    h = load_harness("claude-code")
    for n in (2, 4, 13):
        ir = h.parse(_user_with_n_text_blocks(n), session_id=f"cc-{n}",
                     engine="anthropic", model="claude-opus-4-7")
        _assert_band_order(ir.messages[0].blocks)
        Bridge(ir, load_engine("anthropic"))
    print("✓ test_claude_code_multiple_text_blocks")


def test_pins_preserved_in_source_order() -> None:
    """Within the same band, the source order of content must be preserved (stable sort).
    The three PINs Question 0 / 1 / 2 remain in order 0, 1, 2 after sorting."""
    h = load_harness("openclaw")
    ir = h.parse(_user_with_n_text_blocks(3), session_id="stable",
                 engine="anthropic")
    pins = [b for b in ir.messages[0].blocks if b.band is Band.PIN]
    assert [str(b.payload).startswith(f"Question {i}.") for i, b in enumerate(pins)]
    print("✓ test_pins_preserved_in_source_order")


def test_user_text_then_tool_result_then_text() -> None:
    """Mixed text + tool_result + text: tool_result comes first, the rest is pin*→fold*→drop*."""
    req = {
        "model": "claude-opus-4-7",
        "max_tokens": 64,
        "system": [{"type": "text", "text": "agent"}],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": "Q1. <environment_info>x</environment_info>"},
                {"type": "tool_result", "tool_use_id": "tu_1",
                 "content": [{"type": "text", "text": "stdout..."}]},
                {"type": "text",
                 "text": "Q2. Current time: now"},
            ],
        }],
    }
    h = load_harness("openclaw")
    ir = h.parse(req, session_id="mix", engine="anthropic")
    blocks = ir.messages[0].blocks
    _assert_band_order(blocks)
    bands = [b.band for b in blocks]
    assert bands.count(Band.PIN) == 2
    assert bands.count(Band.FOLD) == 1  # tool_result
    assert bands.count(Band.DROP) == 2  # two envelopes
    Bridge(ir, load_engine("anthropic"))
    print("✓ test_user_text_then_tool_result_then_text")


def main() -> None:
    test_openclaw_multiple_text_blocks()
    test_claude_code_multiple_text_blocks()
    test_pins_preserved_in_source_order()
    test_user_text_then_tool_result_then_text()
    print("\nall harness-multiblock regression tests passed.")


if __name__ == "__main__":
    main()
