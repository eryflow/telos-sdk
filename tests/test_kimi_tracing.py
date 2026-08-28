"""Kimi Code hook mapping and turn-correlation tests."""

from __future__ import annotations

import io
import json

import pytest

from telos.kimi_tracing import map_kimi_hook, run_kimi_hook, stable_id


def _payload(event: str, **extra):
    return {
        "hook_event_name": event,
        "session_id": "session-1",
        "cwd": "/repo",
        "_telos_turn_id": "turn-1",
        **extra,
    }


def test_maps_turn_tool_failure_and_interrupt() -> None:
    started = map_kimi_hook(
        _payload("UserPromptSubmit", prompt="fix it"), 100
    )
    assert started[1]["body"]["input"] == "fix it"
    assert started[1]["body"]["harness"] == "kimi-code"

    tool = map_kimi_hook(_payload(
        "PostToolUseFailure",
        tool_name="Bash",
        tool_call_id="call-1",
        tool_input={"command": "false"},
        error={"message": "exit 1"},
    ), 200)[-1]["body"]
    assert tool["id"] == stable_id("tool", "session-1", "turn-1", "call-1")
    assert tool["status"] == "error"
    assert tool["error"]["message"] == "exit 1"

    ended = map_kimi_hook(_payload("Interrupt", reason="user"), 300)[1]["body"]
    assert ended["status"] == "cancelled"
    assert ended["end_time_us"] == 300


def test_mapping_inherits_explicit_attempt_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELOS_ATTEMPT_ID", "attempt-8")
    operations = map_kimi_hook(_payload("UserPromptSubmit", prompt="continue"), 100)
    assert operations[0]["body"]["attempt_id"] == "attempt-8"
    assert operations[1]["body"]["attempt_id"] == "attempt-8"


def test_runner_correlates_events_and_is_fail_open(tmp_path) -> None:
    batches = []
    state = tmp_path / "turns.json"
    clock = iter((100, 200, 300))
    sender = batches.append

    assert run_kimi_hook(
        io.StringIO(json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-1",
            "cwd": "/repo",
            "prompt": "hello",
        })),
        sender=sender,
        clock_us=lambda: next(clock),
        turn_state_path=state,
    ) == 0
    assert run_kimi_hook(
        io.StringIO(json.dumps({
            "hook_event_name": "PreToolUse",
            "session_id": "session-1",
            "cwd": "/repo",
            "tool_name": "Read",
            "tool_call_id": "call-1",
            "tool_input": {"path": "README.md"},
        })),
        sender=sender,
        clock_us=lambda: next(clock),
        turn_state_path=state,
    ) == 0
    assert batches[0][1]["body"]["id"] == batches[1][1]["body"]["id"]

    assert run_kimi_hook(
        io.StringIO("not-json"),
        sender=lambda _ops: (_ for _ in ()).throw(AssertionError()),
        clock_us=lambda: next(clock),
        turn_state_path=state,
    ) == 0


def test_subagent_start_and_stop_share_span_id() -> None:
    started = map_kimi_hook(_payload(
        "SubagentStart", agent_name="reviewer", prompt="review this"
    ), 100)[-1]["body"]
    stopped = map_kimi_hook(_payload(
        "SubagentStop", agent_name="reviewer", response="done"
    ), 200)[-1]["body"]

    assert started["id"] == stopped["id"]
    assert stopped["status"] == "ok"
