"""Append-only Harness Reporter Trace tests."""

from __future__ import annotations

from telos.trace_store import TraceStore, load_events


def test_append_is_ordered_and_idempotent(tmp_path) -> None:
    store = TraceStore(tmp_path)
    seq1, created1 = store.append(
        harness="codex", session_id="session/1", event_id="e1",
        kind="attempt.started", data={},
    )
    seq1_again, created_again = store.append(
        harness="codex", session_id="session/1", event_id="e1",
        kind="attempt.started", data={},
    )
    seq2, created2 = store.append(
        harness="codex", session_id="session/1", event_id="e2",
        kind="tool.finished", data={"exit_code": 0},
    )
    assert (seq1, created1) == (1, True)
    assert (seq1_again, created_again) == (1, False)
    assert (seq2, created2) == (2, True)
    events = load_events(store.path_for("codex", "session/1"))
    assert [event["seq"] for event in events] == [1, 2]


def test_restart_continues_sequence_and_heals_torn_tail(tmp_path) -> None:
    first = TraceStore(tmp_path)
    first.append(
        harness="codex", session_id="s", event_id="e1",
        kind="attempt.started", data={},
    )
    path = first.path_for("codex", "s")
    with path.open("ab") as f:
        f.write(b'{"broken":')

    restarted = TraceStore(tmp_path)
    seq, created = restarted.append(
        harness="codex", session_id="s", event_id="e2",
        kind="attempt.finished", data={"exit_code": 0},
    )
    assert (seq, created) == (2, True)
    events = load_events(path)
    assert [event["event_id"] for event in events] == ["e1", "e2"]
