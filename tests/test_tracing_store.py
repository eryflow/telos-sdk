from __future__ import annotations

import sqlite3
import time

import pytest

from telos.tracing import (
    Span,
    SQLiteTraceStore,
    StoreTraceProcessor,
    Trace,
    get_current_span,
    get_current_trace,
)


def _thread(store: SQLiteTraceStore, *, row_id: str = "thread-1", external_id: str = "session-1") -> None:
    store.upsert_thread({
        "id": row_id,
        "harness": "codex",
        "external_id": external_id,
        "start_time_us": 100,
    })


def _trace(store: SQLiteTraceStore, *, row_id: str = "trace-1", thread_id: str = "thread-1") -> dict:
    return {
        "id": row_id,
        "thread_id": thread_id,
        "harness": "codex",
        "source": "codex-hook",
        "external_id": f"external-{row_id}",
        "name": "turn",
        "status": "running",
        "start_time_us": 200,
        "metadata": {
            "authorization": "secret", "openai_api_key": "also-secret", "safe": True,
        },
        "source_updated_at_us": 200,
    }


def test_lifecycle_context_and_store_processor(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "trace.db") as store:
        _thread(store)
        processor = StoreTraceProcessor(store)
        trace = Trace(
            processor=processor,
            id="trace-1",
            external_id="external-trace-1",
            thread_id="thread-1",
            harness="codex",
            source="codex-hook",
            name="turn",
            start_time_us=200,
        ).start(mark_as_current=True)
        parent = Span(
            processor=processor, id="span-1", external_id="span-1", name="agent",
            source="codex-hook", type="agent", start_time_us=210,
        ).start(mark_as_current=True)
        child = Span(
            processor=processor, id="span-2", external_id="span-2", name="tool",
            source="codex-hook", type="tool", start_time_us=220,
        )

        assert child.trace_id == trace.id
        assert child.parent_span_id == parent.id
        child.start(mark_as_current=True).finish(reset_current=True)
        assert get_current_span() is parent
        parent.finish(reset_current=True)
        trace.finish(reset_current=True)
        assert get_current_span() is None
        assert get_current_trace() is None
        detail = store.get_trace("trace-1")
        assert detail is not None
        assert [span["id"] for span in detail["spans"]] == ["span-1", "span-2"]
        assert all(span["status"] == "ok" for span in detail["spans"])


def test_upsert_is_idempotent_order_safe_and_redacts_metadata(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "trace.db") as store:
        _thread(store)
        body = _trace(store)
        assert store.upsert_trace(body) == {"id": "trace-1", "created": True, "updated": False}
        assert store.upsert_trace(body) == {"id": "trace-1", "created": False, "updated": False}

        finished = {**body, "status": "ok", "end_time_us": 300, "output": {"answer": 42},
                    "name": "finished", "source_updated_at_us": 300}
        store.upsert_trace(finished)
        store.upsert_trace({**body, "status": "running", "start_time_us": 50,
                            "name": "stale", "source_updated_at_us": 100})
        store.upsert_trace({**body, "status": "running", "name": "new start",
                            "source_updated_at_us": 400})

        trace = store.get_trace("trace-1")["trace"]
        assert trace["status"] == "ok"
        assert trace["start_time_us"] == 50
        assert trace["end_time_us"] == 300
        assert trace["output"] == {"answer": 42}
        assert trace["name"] == "new start"
        assert trace["metadata"] == {
            "authorization": "[REDACTED]",
            "openai_api_key": "[REDACTED]",
            "safe": True,
        }


def test_parent_validation_batch_rollback_and_queries(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "trace.db") as store:
        _thread(store)
        store.upsert_trace(_trace(store))
        store.upsert_thread({
            "id": "thread-2", "harness": "codex", "external_id": "session-2",
            "start_time_us": 100,
        })
        store.upsert_trace(_trace(store, row_id="trace-2", thread_id="thread-2"))
        store.upsert_span({
            "id": "parent", "trace_id": "trace-1", "source": "hook",
            "external_id": "parent", "name": "parent", "type": "agent",
            "start_time_us": 210, "source_updated_at_us": 210,
        })
        with pytest.raises(ValueError, match="same trace"):
            store.upsert_span({
                "id": "bad-parent", "trace_id": "trace-2", "parent_span_id": "parent",
                "source": "hook", "external_id": "bad-parent", "name": "tool",
                "type": "tool", "start_time_us": 220, "source_updated_at_us": 220,
            })

        with pytest.raises(ValueError, match="thread does not exist"):
            store.upsert_batch([
                {"entity": "trace", "body": _trace(store, row_id="rolled-back")},
                {"entity": "trace", "body": _trace(store, row_id="invalid", thread_id="missing")},
            ])
        assert store.get_trace("rolled-back") is None

        page = store.list_traces(limit=1)
        assert len(page["items"]) == 1
        assert page["next_cursor"]
        second = store.list_traces(limit=1, cursor=page["next_cursor"])
        assert len(second["items"]) == 1
        assert store.get_thread("thread-1")["traces"][0]["id"] == "trace-1"


def test_list_searches_trace_input_and_output(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "trace.db") as store:
        _thread(store)
        store.upsert_trace({
            **_trace(store),
            "input": {"content": [{"text": "TELOS_DSH_E2E_20260827"}]},
            "output": {"answer": "EXPECTED_TOOL_ERROR_RECOVERED"},
        })

        assert [item["id"] for item in store.list_traces(
            search="TELOS_DSH_E2E_20260827",
        )["items"]] == ["trace-1"]
        assert [item["id"] for item in store.list_traces(
            search="EXPECTED_TOOL_ERROR_RECOVERED",
        )["items"]] == ["trace-1"]


def test_find_active_synthetic_feedback_and_pragmas(tmp_path) -> None:
    path = tmp_path / "trace.db"
    with SQLiteTraceStore(path) as store:
        assert store.ensure_project()["id"] == "default"
        synthetic = store.ensure_synthetic_trace(
            harness="codex", thread_external_id="session-x", trace_external_id="request-1",
            input={"prompt": "hello"}, start_time_us=500,
        )
        again = store.ensure_synthetic_trace(
            harness="codex", thread_external_id="session-x", trace_external_id="request-1",
            input={"prompt": "hello"}, start_time_us=500,
        )
        assert again["id"] == synthetic["id"]
        assert store.find_active_trace("codex", "session-x")["id"] == synthetic["id"]
        feedback = store.add_feedback_score({
            "id": "score-1", "trace_id": synthetic["id"], "name": "quality", "value": 0.8,
        })
        assert feedback["created"] is True
        assert store.get_trace(synthetic["id"])["feedback_scores"][0]["value"] == 0.8

        assert store._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 4
    finally:
        connection.close()


def test_terminal_thread_abandons_running_children(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "trace.db") as store:
        _thread(store)
        store.upsert_trace(_trace(store))
        store.upsert_span({
            "id": "span-1", "trace_id": "trace-1", "source": "hook",
            "external_id": "span-1", "name": "tool", "type": "tool",
            "start_time_us": 220, "source_updated_at_us": 220,
        })

        store.upsert_thread({
            "id": "thread-1", "harness": "codex", "external_id": "session-1",
            "status": "ok", "start_time_us": 100, "end_time_us": 400,
        })

        detail = store.get_trace("trace-1")
        assert detail["trace"]["status"] == "abandoned"
        assert detail["trace"]["end_time_us"] == 400
        assert detail["spans"][0]["status"] == "abandoned"
        assert detail["spans"][0]["end_time_us"] == 400

        store.upsert_span({
            "id": "span-1", "trace_id": "trace-1", "source": "hook",
            "external_id": "span-1", "name": "tool", "type": "tool", "status": "ok",
            "start_time_us": 220, "end_time_us": 350, "output": {"late": True},
            "source_updated_at_us": 350,
        })
        detail = store.get_trace("trace-1")
        assert detail["spans"][0]["status"] == "ok"
        assert detail["spans"][0]["end_time_us"] == 350
        assert detail["spans"][0]["output"] == {"late": True}


def test_task_run_attempt_and_trace_have_explicit_lineage(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "trace.db") as store:
        task_type = store.ensure_task_type("code-defect-repair")
        run = store.create_task_run(
            goal="fix the tab state", task_type_id=task_type["id"],
            workspace={"root": "/repo"},
        )
        attempt = store.create_attempt(task_run_id=run["id"], harness="codex")
        store.upsert_thread({
            "id": "thread-1", "harness": "codex", "external_id": "session-1",
            "start_time_us": 100, "attempt_id": attempt["id"],
        })
        store.upsert_trace(_trace(store))

        detail = store.get_trace("trace-1")
        assert detail["attempt"]["id"] == attempt["id"]
        assert detail["task_run"]["id"] == run["id"]
        assert detail["unassigned_evidence"] is False
        assert store.get_task_run(run["id"])["attempts"][0]["harness"] == "codex"

        store.upsert_thread({
            "id": "unassigned-thread", "harness": "kimi-code",
            "external_id": "unassigned", "start_time_us": 100,
        })
        body = _trace(store, row_id="unassigned", thread_id="unassigned-thread")
        body["harness"] = "kimi-code"
        store.upsert_trace(body)
        assert store.get_trace("unassigned")["unassigned_evidence"] is True


def test_trace_list_10k_p95_under_200ms(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "trace.db") as store:
        _thread(store)
        operations = []
        for index in range(10_000):
            trace_id = f"trace-{index:05d}"
            started = 1_000_000 + index
            operations.extend((
                {"entity": "trace", "body": {
                    "id": trace_id,
                    "thread_id": "thread-1",
                    "harness": "codex",
                    "source": "benchmark",
                    "external_id": trace_id,
                    "name": f"turn {index}",
                    "status": "ok",
                    "start_time_us": started,
                    "end_time_us": started + 10,
                    "source_updated_at_us": started + 10,
                }},
                {"entity": "span", "body": {
                    "id": f"span-{index:05d}",
                    "trace_id": trace_id,
                    "source": "gateway",
                    "external_id": f"call-{index}",
                    "name": "gpt-5",
                    "type": "llm",
                    "status": "ok",
                    "start_time_us": started,
                    "end_time_us": started + 10,
                    "model": "gpt-5",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cost_usd_micros": 2,
                    "source_updated_at_us": started + 10,
                }},
            ))
            if len(operations) == 256:
                store.upsert_batch(operations)
                operations.clear()
        if operations:
            store.upsert_batch(operations)

        store.list_traces(harness="codex", status="ok", model="gpt-5", limit=50)
        samples = []
        for _ in range(20):
            started = time.perf_counter()
            page = store.list_traces(
                harness="codex", status="ok", model="gpt-5", limit=50,
            )
            samples.append(time.perf_counter() - started)
        assert len(page["items"]) == 50 and page["next_cursor"]
        assert sorted(samples)[18] < 0.2
