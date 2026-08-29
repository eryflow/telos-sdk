"""Pure-function unit tests for ``telos.scripts.build_developer_page``.

No need to start aiohttp: manually construct _SessionInspector + _SessionRegistry to feed data,
and check that the rendered HTML contains the key fields. Verifies:

- the empty state (no session) renders without raising
- single-session detail: the three region stacks / tool-call statistics / cache fields
- pin/fold/drop chars delta (the "previous turn → this turn" arrow) is correct
- tool_use → tool_result pairing: relating back to the tool name via tool_use_id
"""

from __future__ import annotations

from telos.proxy.inspector import SessionInspector
from telos.scripts.build_developer_page import render_developer


# reuse a friendlier alias
_SessionInspector = SessionInspector


class _SessionRegistry:
    """A minimal stub for testing -- exposes only __len__."""
    def __init__(self) -> None:
        self._n = 0
    def __len__(self) -> int:
        return self._n


def test_empty_overview_renders() -> None:
    """Must not raise even when there is no session."""
    body = render_developer(
        _SessionInspector(), _SessionRegistry(),
        focus_session=None, refresh_seconds=5,
    )
    assert "TELOS · Trace inspector" in body
    assert 'href="/">Context</a>' in body
    assert 'href="/savings">Savings</a>' in body
    assert 'href="/traces">Evidence</a>' in body
    assert "0 session(s) tracked" in body
    assert "No sessions yet" in body
    assert 'content="5"' in body  # refresh tag


def _layout(*, pin_sys=200, fold_sys=300, drop_sys=50,
              messages_chars=1000) -> dict:
    return {
        "session_id": "s1",
        "engine": "anthropic",
        "model": "claude-opus-4-7",
        "segments": {
            "tools": {
                "pin": {"blocks": 3, "chars": 800},
                "fold": {"blocks": 0, "chars": 0},
                "drop": {"blocks": 0, "chars": 0},
            },
            "system": {
                "pin": {"blocks": 1, "chars": pin_sys},
                "fold": {"blocks": 2, "chars": fold_sys},
                "drop": {"blocks": 1, "chars": drop_sys},
            },
            "messages": {
                "pin": {"blocks": 1, "chars": 50},
                "fold": {"blocks": 3, "chars": messages_chars},
                "drop": {"blocks": 1, "chars": 20},
            },
        },
        "messages": [
            {"index": 0, "role": "user", "blocks": [
                {"id": "u0/pin", "band": "pin", "kind": "text", "chars": 50,
                 "source_tag": "telos/user-pin", "ref_slug": None},
                {"id": "u0/drop", "band": "drop", "kind": "text", "chars": 20,
                 "source_tag": "telos/env", "ref_slug": None},
            ]},
            {"index": 1, "role": "assistant", "blocks": [
                {"id": "a1/use", "band": "fold", "kind": "tool_use", "chars": 80,
                 "source_tag": "telos/tu", "ref_slug": None},
            ]},
        ],
        "ref_pool": [
            {"slug": "login-py", "band": "fold", "chars": 3200},
        ],
    }


def test_session_detail_with_calls_and_tools() -> None:
    insp = _SessionInspector()
    reg = _SessionRegistry()
    entry = insp.touch("sess-A")

    # the 1st call
    entry.record(
        call_index=1,
        layout=_layout(messages_chars=1000),
        plan_slots=["BP-T", "BP-S", "BP-R", "BP-X"],
        tool_uses=[
            {"message_index": 1, "id": "tu_001", "name": "Bash",
             "args_chars": 40},
        ],
        tool_results=[],
        usage_norm={"raw_input": 200, "cache_read": 1500,
                    "cache_write": 500, "output": 80},
        usage_raw={"input_tokens": 200, "cache_read_input_tokens": 1500,
                    "cache_creation_input_tokens": 500,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 300,
                        "ephemeral_1h_input_tokens": 200},
                    "output_tokens": 80},
        latency_s=2.4,
        model="claude-opus-4-7", harness="telos",
    )
    # the 2nd call: the messages segment grows larger (fold growth), tool_result flows back, a new Read tool is added
    entry.record(
        call_index=2,
        layout=_layout(messages_chars=1800),  # +800 chars
        plan_slots=["BP-T", "BP-S", "BP-R", "BP-X"],
        tool_uses=[
            {"message_index": 3, "id": "tu_002", "name": "Read",
             "args_chars": 30},
        ],
        tool_results=[
            {"message_index": 2, "tool_use_id": "tu_001", "result_chars": 1200},
        ],
        usage_norm={"raw_input": 100, "cache_read": 2400,
                    "cache_write": 200, "output": 95},
        usage_raw={"input_tokens": 100, "cache_read_input_tokens": 2400,
                    "cache_creation_input_tokens": 200,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 120,
                        "ephemeral_1h_input_tokens": 80},
                    "output_tokens": 95},
        latency_s=2.7,
        model="claude-opus-4-7", harness="telos",
    )

    body = render_developer(insp, reg, focus_session="sess-A",
                              refresh_seconds=3)

    # key checks
    assert "session · sess-A" in body
    assert "claude-opus-4-7" in body
    assert "telos" in body
    # segment stack bar
    assert "tools" in body and "system" in body and "messages" in body
    # plan slots list
    assert "BP-T" in body and "BP-X" in body
    # tool call statistics
    assert "Bash" in body
    assert "Read" in body
    # tool_result is looked up back to Bash via tu_001 → Bash's result_chars_total = 1200
    assert "1,200" in body
    # the segment character delta (messages +800) must be displayed as a positive Δ
    assert "+800" in body
    # the cache breakdown fields must appear unchanged
    assert "ephemeral_5m_input_tokens" in body
    assert "ephemeral_1h_input_tokens" in body or "200" in body  # at least passed through
    # refresh tag
    assert 'content="3"' in body


def test_overview_lists_session() -> None:
    insp = _SessionInspector()
    reg = _SessionRegistry()
    entry = insp.touch("alice-key")
    entry.record(
        call_index=1, layout=_layout(),
        plan_slots=["BP-T"], tool_uses=[], tool_results=[],
        usage_norm={"raw_input": 1, "cache_read": 0, "cache_write": 0, "output": 1},
        usage_raw={}, latency_s=0.1,
        model="claude-sonnet-4-6", harness="telos",
    )
    body = render_developer(insp, reg, focus_session=None)
    assert "alice-key" in body
    assert "claude-sonnet-4-6" in body
    assert "1 session(s) tracked" in body


def test_unknown_focus_falls_back_to_friendly_404() -> None:
    body = render_developer(_SessionInspector(), _SessionRegistry(),
                              focus_session="does-not-exist")
    assert "session not found" in body
    assert "does-not-exist" in body
    # provides a link back to the overview
    assert "back to overview" in body


def test_entry_to_json_roundtrips() -> None:
    """The JSON view must be serializable and have all fields."""
    import json as _json
    from telos.proxy.inspector import entry_to_json

    insp = _SessionInspector()
    e = insp.touch("s-X")
    e.record(call_index=1, layout=_layout(),
        plan_slots=["BP-T"],
        tool_uses=[{"id":"tu","name":"Bash","args_chars":10}],
        tool_results=[{"tool_use_id":"tu","result_chars":500}],
        usage_norm={"raw_input":1,"cache_read":2,"cache_write":3,"output":4},
        usage_raw={"input_tokens":1}, latency_s=0.5,
        model="m", harness="h")
    js = entry_to_json(e)
    # must be able to round-trip
    s = _json.dumps(js)
    js2 = _json.loads(s)
    assert js2["session_id"] == "s-X"
    assert js2["model"] == "m"
    assert len(js2["calls"]) == 1
    assert any(t["name"] == "Bash" and t["invocations"] == 1
                for t in js2["tools"])
    # the Bash tool_result is related to Bash via tu, so result_chars_total == 500
    bash = next(t for t in js2["tools"] if t["name"] == "Bash")
    assert bash["result_chars_total"] == 500


def _run_all() -> None:
    test_empty_overview_renders()
    test_session_detail_with_calls_and_tools()
    test_overview_lists_session()
    test_unknown_focus_falls_back_to_friendly_404()
    test_entry_to_json_roundtrips()
    print("OK · all developer-page tests passed")


if __name__ == "__main__":
    _run_all()
