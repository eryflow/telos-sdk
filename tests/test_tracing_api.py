from __future__ import annotations

from aiohttp import ClientSession, web
import pytest

from telos.config import UpstreamConfig
from telos.proxy.server import make_app


async def _start(tmp_path) -> tuple[web.AppRunner, str]:
    app = make_app(
        upstream="http://127.0.0.1:1",
        record=False,
        tracing_db=tmp_path / "telos.db",
        trace_harnesses={
            "codex": {
                "enabled": True,
                "tracing_token": "codex-secret",
                "model_span_source": "gateway",
            }
        },
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


def _batch() -> dict:
    return {
        "schema_version": 1,
        "operations": [
            {
                "entity": "thread",
                "op": "upsert",
                "body": {
                    "id": "thread-1",
                    "project_name": "default",
                    "harness": "codex",
                    "external_id": "session-1",
                    "name": "Session 1",
                    "status": "running",
                    "start_time_us": 100,
                },
            },
            {
                "entity": "trace",
                "op": "upsert",
                "body": {
                    "id": "trace-1",
                    "project_name": "default",
                    "thread_id": "thread-1",
                    "harness": "codex",
                    "source": "codex-hook",
                    "external_id": "session-1:turn-1",
                    "name": "Fix the test",
                    "status": "running",
                    "start_time_us": 110,
                    "input": "please fix it",
                    "source_updated_at_us": 110,
                },
            },
            {
                "entity": "span",
                "op": "upsert",
                "body": {
                    "id": "span-1",
                    "trace_id": "trace-1",
                    "source": "gateway",
                    "external_id": "call-1",
                    "name": "gpt response",
                    "type": "llm",
                    "status": "ok",
                    "start_time_us": 120,
                    "end_time_us": 150,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cost_usd_micros": 42,
                    "source_updated_at_us": 150,
                },
            },
        ],
    }


@pytest.mark.asyncio
async def test_tracing_batch_read_api_and_page(tmp_path) -> None:
    runner, base = await _start(tmp_path)
    headers = {"Authorization": "Bearer codex-secret"}
    try:
        async with ClientSession() as client:
            response = await client.post(
                f"{base}/__telos/tracing/v1/batch", json=_batch(), headers=headers
            )
            assert response.status == 200
            assert len((await response.json())["accepted"]) == 3

            retry = await client.post(
                f"{base}/__telos/tracing/v1/batch", json=_batch(), headers=headers
            )
            assert retry.status == 200
            assert all(not item["created"] for item in (await retry.json())["accepted"])

            listed = await client.get(f"{base}/__telos/api/v1/traces?limit=10")
            item = (await listed.json())["items"][0]
            assert item["id"] == "trace-1"
            assert item["total_tokens"] == 15
            assert item["cost_usd_micros"] == 42
            assert "input" not in item

            detail = await client.get(f"{base}/__telos/api/v1/traces/trace-1")
            payload = await detail.json()
            assert payload["trace"]["input"] == "please fix it"
            assert payload["spans"][0]["type"] == "llm"

            thread = await client.get(f"{base}/__telos/api/v1/threads/thread-1")
            assert (await thread.json())["traces"][0]["id"] == "trace-1"

            feedback = await client.post(
                f"{base}/__telos/api/v1/feedback-scores",
                json={"trace_id": "trace-1", "name": "quality", "value": 1},
            )
            assert feedback.status == 201

            page = await client.get(f"{base}/__telos/traces")
            assert page.status == 200
            assert "TELOS Traces" in await page.text()

            removed = await client.post(
                f"{base}/__telos/reporter/events", json={"token": "must-not-forward"}
            )
            assert removed.status == 404
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_tracing_ingest_rejects_wrong_token_and_harness(tmp_path) -> None:
    runner, base = await _start(tmp_path)
    try:
        async with ClientSession() as client:
            denied = await client.post(
                f"{base}/__telos/tracing/v1/batch",
                json=_batch(),
                headers={"Authorization": "Bearer wrong"},
            )
            assert denied.status == 401

            body = _batch()
            body["operations"][0]["body"]["harness"] = "deepseek-harness"
            mismatch = await client.post(
                f"{base}/__telos/tracing/v1/batch",
                json=body,
                headers={"Authorization": "Bearer codex-secret"},
            )
            assert mismatch.status == 400
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_codex_responses_enriches_active_trace(tmp_path) -> None:
    async def responses(request: web.Request) -> web.Response:
        assert request.headers["session_id"] == "codex-session"
        return web.json_response({
            "id": "response-1",
            "output": [{"type": "message", "content": "done"}],
            "usage": {
                "input_tokens": 20,
                "input_tokens_details": {"cached_tokens": 8},
                "output_tokens": 7,
                "output_tokens_details": {"reasoning_tokens": 3},
            },
        })

    upstream_app = web.Application()
    upstream_app.router.add_post("/responses", responses)
    upstream_runner = web.AppRunner(upstream_app)
    await upstream_runner.setup()
    upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
    await upstream_site.start()
    upstream_port = upstream_site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    app = make_app(
        upstream="http://127.0.0.1:1",
        upstreams={
            "codex": UpstreamConfig(
                url=f"http://127.0.0.1:{upstream_port}",
                engine="openai",
                protocol="openai-chat",
                via="codex",
            )
        },
        record=False,
        tracing_db=tmp_path / "telos.db",
        trace_harnesses={
            "codex": {
                "enabled": True,
                "tracing_token": "codex-secret",
                "model_span_source": "gateway",
            }
        },
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    base = f"http://127.0.0.1:{port}"
    seed = _batch()
    seed["operations"] = seed["operations"][:2]
    seed["operations"][0]["body"]["external_id"] = "codex-session"
    seed["operations"][1]["body"]["external_id"] = "codex-session:turn-1"
    try:
        async with ClientSession() as client:
            seeded = await client.post(
                f"{base}/__telos/tracing/v1/batch",
                json=seed,
                headers={"Authorization": "Bearer codex-secret"},
            )
            assert seeded.status == 200, await seeded.text()

            response = await client.post(
                f"{base}/upstreams/codex/responses",
                json={"model": "gpt-5", "input": "fix it"},
                headers={"session_id": "codex-session"},
            )
            assert response.status == 200, await response.text()

            detail = await client.get(f"{base}/__telos/api/v1/traces/trace-1")
            spans = (await detail.json())["spans"]
            assert len(spans) == 1
            assert spans[0]["type"] == "llm"
            assert spans[0]["status"] == "ok"
            assert spans[0]["input_tokens"] == 20
            assert spans[0]["cache_read_tokens"] == 8
            assert spans[0]["reasoning_tokens"] == 3
            assert spans[0]["output"]["id"] == "response-1"
            assert spans[0]["ttft_us"] is not None
    finally:
        await runner.cleanup()
        await upstream_runner.cleanup()
