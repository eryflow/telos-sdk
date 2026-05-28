"""``telos.replay`` unit tests: replay with an injected fake sender, no network."""

from __future__ import annotations

import json

from telos.output_filter import TelosMode
from telos.replay import replay_session
from telos.scripts.build_savings_dashboard import aggregate


def _turns() -> list[dict]:
    """Two turns: the second carries a large block of repeated bash output (an ideal RTK target)."""
    big = "start\n" + ("compiling module foo\n" * 300) + "done\n"
    return [
        {"call_index": 1, "request": {
            "model": "claude-opus-4-7", "max_tokens": 100,
            "system": [{"type": "text", "text": "You are an agent."}],
            "messages": [{"role": "user",
                          "content": [{"type": "text", "text": "build it"}]}],
        }},
        {"call_index": 2, "request": {
            "model": "claude-opus-4-7", "max_tokens": 100,
            "system": [{"type": "text", "text": "You are an agent."}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "build it"}]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash",
                     "input": {"command": "cargo build"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": big}]},
            ],
        }},
    ]


def _make_sender() -> tuple:
    """Returns (sender, seen_wires); usage is roughly proportional to wire size."""
    seen: list[dict] = []

    def sender(wire):
        seen.append(dict(wire))
        n = len(json.dumps(wire))
        return {"input_tokens": n // 4, "output_tokens": 1,
                "cache_read_input_tokens": n // 10,
                "cache_creation_input_tokens": n // 20}

    return sender, seen


def test_replay_records_carry_mode_and_compare_group() -> None:
    sender, _ = _make_sender()
    r = replay_session(_turns(), TelosMode.from_label("both"),
                       session_id="sess-X", compare_group="grp-1", sender=sender)
    assert r.turns_ok == 2
    assert len(r.records) == 2
    for rec in r.records:
        assert rec["mode"] == "both"
        assert rec["compare_group"] == "grp-1"
        assert rec["replay"] is True
        assert rec["session_id"] == "sess-X/both"
        assert "normalized" in rec and "raw_usage" in rec
    print("✓ test_replay_records_carry_mode_and_compare_group")


def test_replay_forces_max_tokens_and_strips_streaming() -> None:
    sender, seen = _make_sender()
    replay_session(_turns(), TelosMode.from_label("none"),
                   session_id="s", compare_group="g", sender=sender)
    for wire in seen:
        assert wire["max_tokens"] == 1, "replay should force max_tokens to 1"
        assert "stream" not in wire
        assert "tool_choice" not in wire
    print("✓ test_replay_forces_max_tokens_and_strips_streaming")


def test_replay_injects_cache_namespace() -> None:
    """By default, inject a unique system prefix per mode for cache isolation."""
    sender, seen = _make_sender()
    replay_session(_turns(), TelosMode.from_label("none"),
                   session_id="sess-Y", compare_group="g", sender=sender)
    blob = json.dumps(seen)
    assert "telos-replay ns=sess-Y/none" in blob
    # with isolation turned off, nothing is injected
    sender2, seen2 = _make_sender()
    replay_session(_turns(), TelosMode.from_label("none"),
                   session_id="sess-Y", compare_group="g", sender=sender2,
                   cache_isolation=False)
    assert "telos-replay" not in json.dumps(seen2)
    print("✓ test_replay_injects_cache_namespace")


def test_replay_rtk_mode_shrinks_and_records_reduction() -> None:
    sender, seen = _make_sender()
    r = replay_session(_turns(), TelosMode.from_label("rtk"),
                       session_id="s", compare_group="g", sender=sender)
    # the second turn carries large output → reduction is non-empty
    turn2 = r.records[1]
    red = turn2["tool_output_reduction"]
    assert red["blocks_filtered"] == 1
    assert red["saved_chars"] > 0
    # the tool_result in the emitted wire is indeed shortened
    big_orig = len("start\n" + ("compiling module foo\n" * 300) + "done\n")
    wire2 = seen[1]
    tr = None
    for msg in wire2.get("messages", []):
        for item in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                tr = item["content"]
    assert tr is not None and len(tr) < big_orig
    print("✓ test_replay_rtk_mode_shrinks_and_records_reduction")


def test_replay_output_keeps_comparison_metadata() -> None:
    """Replay records keep comparison metadata without coupling it to the live dashboard."""
    sender, _ = _make_sender()
    all_recs = []
    for label in ("none", "telos", "rtk", "both"):
        r = replay_session(_turns(), TelosMode.from_label(label),
                           session_id="sess-Z", compare_group="sess-Z",
                           sender=sender)
        all_recs.extend(r.records)
        assert all(rec["compare_group"] == "sess-Z" for rec in r.records)
        assert all(rec["replay"] is True for rec in r.records)
    summary = aggregate(all_recs)
    assert set(summary.by_mode.keys()) == {"none", "telos", "rtk", "both"}
    print("✓ test_replay_output_keeps_comparison_metadata")


def test_replay_on_turn_callback_fires_per_turn() -> None:
    """``on_turn`` is invoked once per turn with a monotonic (idx, total)."""
    sender, _ = _make_sender()
    seen: list[tuple[int, int, int]] = []

    def on_turn(result, idx, total):
        seen.append((idx, total, len(result.records)))

    r = replay_session(_turns(), TelosMode.from_label("both"),
                       session_id="s", compare_group="g", sender=sender,
                       on_turn=on_turn)
    assert [s[0] for s in seen] == [1, 2]      # idx counts up
    assert all(s[1] == 2 for s in seen)        # total is the turn count
    assert seen[-1][2] == len(r.records) == 2  # records accumulate
    print("✓ test_replay_on_turn_callback_fires_per_turn")


def test_replay_strips_context_management() -> None:
    """`context_management` is dropped before sending (newer field; not needed for prefill)."""
    turns = _turns()
    turns[0]["request"]["context_management"] = {"edits": [{"type": "clear_tool_uses"}]}
    sender, seen = _make_sender()
    replay_session(turns, TelosMode.from_label("none"),
                   session_id="s", compare_group="g", sender=sender)
    assert all("context_management" not in wire for wire in seen)
    print("✓ test_replay_strips_context_management")


def test_replay_retryable_classification() -> None:
    """Transient upstream failures (529 / 5xx / 429 / network) are retryable; 4xx is not."""
    from telos.replay import _is_retryable

    class _Status(Exception):
        def __init__(self, code): self.status_code = code

    class APITimeoutError(Exception):
        pass

    assert _is_retryable(_Status(529))   # overloaded
    assert _is_retryable(_Status(500))   # internal error
    assert _is_retryable(_Status(429))   # rate limited
    assert _is_retryable(APITimeoutError())
    assert not _is_retryable(_Status(400))   # bad request — do not retry
    assert not _is_retryable(_Status(404))
    assert not _is_retryable(ValueError("boom"))
    print("✓ test_replay_retryable_classification")


def main() -> None:
    test_replay_records_carry_mode_and_compare_group()
    test_replay_forces_max_tokens_and_strips_streaming()
    test_replay_injects_cache_namespace()
    test_replay_rtk_mode_shrinks_and_records_reduction()
    test_replay_output_keeps_comparison_metadata()
    test_replay_on_turn_callback_fires_per_turn()
    test_replay_strips_context_management()
    test_replay_retryable_classification()
    print("\nall replay tests passed.")


if __name__ == "__main__":
    main()
