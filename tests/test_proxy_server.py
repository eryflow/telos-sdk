"""End-to-end proxy test: start a mock upstream, run the real proxy, assert on the wire content."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from telos.proxy.server import make_app


# ---------------------------------------------------------------------------
# mock upstream (acts as api.anthropic.com)
# ---------------------------------------------------------------------------

class _MockUpstream:
    """Records the received wire requests; returns a fixed JSON or an SSE stream."""

    def __init__(self, *, sse: bool = False) -> None:
        self.last_body: dict[str, Any] | None = None
        self.last_headers: dict[str, str] | None = None
        self.sse = sse

    async def handler(self, request: web.Request) -> web.StreamResponse:
        self.last_body = await request.json()
        self.last_headers = dict(request.headers)
        if self.sse:
            response = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await response.prepare(request)
            chunks = [
                b'event: message_start\n'
                b'data: {"type":"message_start","message":{"id":"msg_x","usage":'
                b'{"input_tokens":120,"cache_read_input_tokens":1000,'
                b'"cache_creation_input_tokens":0,"output_tokens":1}}}\n\n',
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,"delta":'
                b'{"type":"text_delta","text":"Hello"}}\n\n',
                b'event: message_delta\n'
                b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
                b'"usage":{"output_tokens":42}}\n\n',
                b'event: message_stop\n'
                b'data: {"type":"message_stop"}\n\n',
            ]
            for c in chunks:
                await response.write(c)
            await response.write_eof()
            return response

        return web.json_response({
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-opus-4-7",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 120,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 0,
                "output_tokens": 5,
            },
        })


# ---------------------------------------------------------------------------
# Fixture helpers (do not pull in pytest-aiohttp, set up by hand)
# ---------------------------------------------------------------------------

async def _start_upstream(mock: _MockUpstream) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_post("/v1/messages", mock.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def _start_proxy(upstream_url: str, *, usage_log: Path | None = None) -> tuple[web.AppRunner, str]:
    app = make_app(upstream=upstream_url, usage_log=usage_log)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def _test_non_streaming() -> None:
    mock = _MockUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)

    try:
        async with aiohttp.ClientSession() as client:
            req_body = {
                "model": "claude-opus-4-7",
                "max_tokens": 256,
                "tools": [
                    {"name": "Read", "input_schema": {"type": "object",
                        "properties": {"path": {"type": "string"}}}},
                ],
                "system": [{"type": "text", "text": "You are an agent."}],
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "Hi"}]},
                ],
                "stream": False,
            }
            async with client.post(
                f"{px_url}/v1/messages",
                json=req_body,
                headers={
                    "x-api-key": "test-key",
                    "anthropic-version": "2023-06-01",
                    "x-telos-session": "smoke-non-stream",
                },
            ) as resp:
                assert resp.status == 200, await resp.text()
                body = await resp.json()
                assert body["id"] == "msg_test"
                assert body["usage"]["cache_read_input_tokens"] == 1000

        # ---- verify the upstream received the TELOS-processed wire ----
        wire = mock.last_body
        assert wire is not None
        # the tools segment or system segment has cache_control in at least one place (BP-T or BP-S)
        all_blocks = list(wire.get("tools") or []) + list(wire.get("system") or [])
        assert any("cache_control" in b for b in all_blocks), \
            f"the wire the upstream received has no cache_control: {wire}"
        # the auth header should be passed through
        assert mock.last_headers.get("x-api-key") == "test-key"
        assert mock.last_headers.get("anthropic-version") == "2023-06-01"
        print("✓ test_non_streaming")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_streaming_sse() -> None:
    mock = _MockUpstream(sse=True)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)

    try:
        async with aiohttp.ClientSession() as client:
            req_body = {
                "model": "claude-opus-4-7",
                "max_tokens": 256,
                "system": [{"type": "text", "text": "agent"}],
                "messages": [{"role": "user",
                              "content": [{"type": "text", "text": "Hi"}]}],
                "stream": True,
            }
            async with client.post(
                f"{px_url}/v1/messages",
                json=req_body,
                headers={"x-api-key": "test-key",
                         "anthropic-version": "2023-06-01"},
            ) as resp:
                assert resp.status == 200
                # SSE: accumulate chunks
                received = b""
                async for chunk in resp.content.iter_any():
                    received += chunk
                assert b"message_start" in received
                assert b"message_delta" in received
                assert b'"output_tokens":42' in received
        print("✓ test_streaming_sse")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_usage_log_written(tmp_log: Path) -> None:
    mock = _MockUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url, usage_log=tmp_log)

    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages",
                json={
                    "model": "claude-opus-4-7",
                    "max_tokens": 64,
                    "system": [{"type": "text", "text": "agent"}],
                    "messages": [{"role": "user",
                                  "content": [{"type": "text", "text": "x"}]}],
                },
                headers={"x-api-key": "k", "anthropic-version": "2023-06-01",
                         "x-telos-session": "log-test"},
            ) as resp:
                assert resp.status == 200

        line = tmp_log.read_text().strip().splitlines()[-1]
        record = json.loads(line)
        assert record["session_id"] == "log-test"
        assert record["harness"] in ("openclaw", "claude-code")
        assert record["normalized"]["cache_read"] == 1000
        assert record["normalized"]["output"] == 5
        print("✓ test_usage_log_written")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_pipeline_error_returns_anthropic_error() -> None:
    """A non-JSON body must return 400 following the Anthropic error schema."""
    mock = _MockUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)

    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages",
                data=b"not json",
                headers={"content-type": "application/json"},
            ) as resp:
                assert resp.status == 400
                body = await resp.json()
                assert body["type"] == "error"
                assert body["error"]["type"] == "invalid_request_error"
        print("✓ test_pipeline_error_returns_anthropic_error")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_pipeline_failure_falls_back_to_passthrough() -> None:
    """When TELOS fails (non-strict by default), the original raw is passed through to the upstream and the client sees a normal response.

    Construction method: send a request missing the model field but with valid content -- the harness can parse it,
    but we monkey-patch process_anthropic_request to make it raise, simulating a TELOS bug.
    """
    from telos.proxy import server as srv_mod

    mock = _MockUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)

    # monkey-patch: make the pipeline always raise
    original = srv_mod.process_anthropic_request

    def boom(*args, **kwargs):
        raise RuntimeError("simulated TELOS bug")

    srv_mod.process_anthropic_request = boom
    try:
        async with aiohttp.ClientSession() as client:
            req_body = {
                "model": "claude-opus-4-7",
                "max_tokens": 16,
                "system": [{"type": "text", "text": "agent"}],
                "messages": [{"role": "user",
                              "content": [{"type": "text", "text": "x"}]}],
            }
            async with client.post(
                f"{px_url}/v1/messages",
                json=req_body,
                headers={"x-api-key": "k", "anthropic-version": "2023-06-01"},
            ) as resp:
                # passthrough mode: still 200, body comes from the upstream
                assert resp.status == 200, await resp.text()
                body = await resp.json()
                assert body["id"] == "msg_test"

        # what the upstream received should be the ORIGINAL raw (no cache_control rewrite)
        wire = mock.last_body
        all_blocks = list(wire.get("tools") or []) + list(wire.get("system") or [])
        # cache_control should not appear (because TELOS did not run)
        assert not any("cache_control" in b for b in all_blocks), \
            "passthrough should not have a cache_control rewrite"
        print("✓ test_pipeline_failure_falls_back_to_passthrough")
    finally:
        srv_mod.process_anthropic_request = original
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_strict_mode_returns_500_on_pipeline_failure() -> None:
    """When strict=True, a TELOS failure must return 500 (no degradation)."""
    from telos.proxy import server as srv_mod
    from telos.proxy.server import make_app

    mock = _MockUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)

    # manually start a strict app
    app = make_app(upstream=up_url, strict=True)
    px_runner = web.AppRunner(app)
    await px_runner.setup()
    px_site = web.TCPSite(px_runner, "127.0.0.1", 0)
    await px_site.start()
    px_port = px_site._server.sockets[0].getsockname()[1]
    px_url = f"http://127.0.0.1:{px_port}"

    original = srv_mod.process_anthropic_request

    def boom(*args, **kwargs):
        raise RuntimeError("simulated")

    srv_mod.process_anthropic_request = boom
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages",
                json={"model": "x", "max_tokens": 1, "messages": []},
            ) as resp:
                assert resp.status == 500
                body = await resp.json()
                assert body["error"]["type"] == "api_error"
        print("✓ test_strict_mode_returns_500_on_pipeline_failure")
    finally:
        srv_mod.process_anthropic_request = original
        await px_runner.cleanup()
        await up_runner.cleanup()


class _FlakyUpstream:
    """The first ``fail_first`` connections close immediately after receiving the request (returning no response),

    the rest return JSON normally. On the client side this manifests as ``ServerDisconnectedError`` -- simulating
    the transient jitter of api.anthropic.com during connection setup / early stages.
    """

    def __init__(self, *, fail_first: int) -> None:
        self.fail_first = fail_first
        self.requests = 0
        self.served = 0

    async def handler(self, request: web.Request) -> web.StreamResponse:
        self.requests += 1
        if self.requests <= self.fail_first:
            # close the connection immediately after receiving the request, sending no response.
            request.transport.close()
            raise asyncio.CancelledError()
        self.served += 1
        return web.json_response({
            "id": "msg_flaky",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-opus-4-7",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, "output_tokens": 1},
        })


def _req_body() -> dict[str, Any]:
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}],
        "stream": False,
    }


async def _test_retries_transient_connect_failure() -> None:
    """The first 2 connection attempts fail transiently → the proxy backs off and retries on its own, the client eventually gets 200."""
    mock = _FlakyUpstream(fail_first=2)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=_req_body(),
                headers={"x-api-key": "k", "x-telos-session": "retry-ok"},
            ) as resp:
                assert resp.status == 200, await resp.text()
                body = await resp.json()
                assert body["id"] == "msg_flaky"
        # the upstream received 3 requests in total (2 failures + 1 success), serving successfully only once.
        assert mock.requests == 3, mock.requests
        assert mock.served == 1, mock.served
        print("✓ test_retries_transient_connect_failure")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_502_after_exhausting_retries() -> None:
    """Connection setup keeps failing → after retries are exhausted the proxy returns 502 (anthropic error structure)."""
    mock = _FlakyUpstream(fail_first=999)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=_req_body(),
                headers={"x-api-key": "k", "x-telos-session": "retry-exhaust"},
            ) as resp:
                assert resp.status == 502, await resp.text()
                body = await resp.json()
                assert body["error"]["type"] == "api_error"
        # 1 initial + 3 retries = 4 attempts.
        assert mock.requests == 4, mock.requests
        print("✓ test_502_after_exhausting_retries")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


def test_wire_tool_result_first_helper() -> None:
    """_wire_tool_result_first: tool_result first → True, last → False."""
    from telos.proxy.server import _wire_tool_result_first
    ok = {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "r"},
        {"type": "text", "text": "go"}]}]}
    bad = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "go"},
        {"type": "tool_result", "tool_use_id": "t", "content": "r"}]}]}
    assert _wire_tool_result_first(ok) is True
    assert _wire_tool_result_first(bad) is False
    # pure text / pure tool_result / string content are all considered valid
    assert _wire_tool_result_first({"messages": [
        {"role": "user", "content": "hi"}]}) is True
    print("✓ test_wire_tool_result_first_helper")


async def _test_passthrough_attribution_preserves_claude_code(tmp_log: Path) -> None:
    """A pipeline failure must not erase claude-code attribution in the usage log.

    Regression: handle_messages used to write ``harness="passthrough"`` on
    degradation, hiding Claude Code traffic in the dashboard's "breakdown by
    harness". The overlay now re-resolves via ``_resolve_harness`` which
    first honors the ``/_h/<harness>/`` URL tag and otherwise falls back to
    User-Agent / content / sticky memory.
    """
    from telos.proxy import server as srv_mod

    mock = _MockUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url, usage_log=tmp_log)

    original = srv_mod.process_anthropic_request

    def boom(*args, **kwargs):
        raise RuntimeError("simulated TELOS bug")

    srv_mod.process_anthropic_request = boom
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages",
                json={
                    "model": "claude-opus-4-7",
                    "max_tokens": 16,
                    "system": [{"type": "text", "text": "agent"}],
                    "messages": [{"role": "user",
                                  "content": [{"type": "text", "text": "x"}]}],
                },
                headers={"x-api-key": "k",
                         "anthropic-version": "2023-06-01",
                         "x-telos-session": "claude-code-attr",
                         "user-agent": "claude-cli/1.0.123 (External, cli)"},
            ) as resp:
                assert resp.status == 200, await resp.text()

        line = tmp_log.read_text().strip().splitlines()[-1]
        record = json.loads(line)
        assert record["session_id"] == "claude-code-attr"
        assert record["harness"] == "claude-code", \
            f"claude-code attribution lost on passthrough: {record['harness']!r}"
        print("✓ test_passthrough_attribution_preserves_claude_code")
    finally:
        srv_mod.process_anthropic_request = original
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_url_tag_attribution(tmp_log: Path) -> None:
    """``/_h/<harness>/v1/messages`` stamps result.harness with the URL tag,
    overriding header / content detection."""
    mock = _MockUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url, usage_log=tmp_log)
    try:
        async with aiohttp.ClientSession() as client:
            # Deliberately send a User-Agent that would normally pin this client
            # as claude-code. The URL tag says openclaw — tag must win.
            async with client.post(
                f"{px_url}/_h/openclaw/v1/messages",
                json={
                    "model": "claude-opus-4-7",
                    "max_tokens": 16,
                    "system": [{"type": "text", "text": "agent"}],
                    "messages": [{"role": "user",
                                  "content": [{"type": "text", "text": "x"}]}],
                },
                headers={"x-api-key": "k",
                         "anthropic-version": "2023-06-01",
                         "x-telos-session": "tag-test",
                         "user-agent": "claude-cli/1.0 (External, cli)"},
            ) as resp:
                assert resp.status == 200, await resp.text()
        line = tmp_log.read_text().strip().splitlines()[-1]
        record = json.loads(line)
        assert record["session_id"] == "tag-test"
        assert record["harness"] == "openclaw", \
            f"URL tag must win over header detection; got {record['harness']!r}"
        print("✓ test_url_tag_attribution")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_url_tag_unknown_returns_404() -> None:
    """An unknown tag must 404, not silently fall through to detection."""
    mock = _MockUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/_h/bogus/v1/messages",
                json={"model": "m", "max_tokens": 1, "messages": []},
                headers={"x-api-key": "k"},
            ) as resp:
                assert resp.status == 404, await resp.text()
        print("✓ test_url_tag_unknown_returns_404")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_invalid_wire_falls_back_to_passthrough() -> None:
    """TELOS produces an invalid wire with tool_result last → the proxy falls back to passthrough."""
    from telos.proxy import server as srv_mod

    mock = _MockUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)

    real = srv_mod.process_anthropic_request

    def bad_wire(raw, **kw):
        # simulate a TELOS bug: move each user message's tool_result to the end
        res = real(raw, **kw)
        for m in res.wire.get("messages", []):
            c = m.get("content")
            if m.get("role") == "user" and isinstance(c, list):
                tr = [b for b in c if isinstance(b, dict)
                      and b.get("type") == "tool_result"]
                rest = [b for b in c if not (isinstance(b, dict)
                        and b.get("type") == "tool_result")]
                if tr and rest:
                    m["content"] = rest + tr
        return res

    srv_mod.process_anthropic_request = bad_wire
    try:
        req = {
            "model": "claude-opus-4-7", "max_tokens": 16, "stream": False,
            "system": [{"type": "text", "text": "agent"}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "do it"}]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu1", "name": "Bash",
                     "input": {"command": "ls"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu1",
                     "content": "output"},
                    {"type": "text", "text": "continue"}]},
            ],
        }
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=req,
                headers={"x-api-key": "k", "x-telos-session": "bad-wire"},
            ) as resp:
                assert resp.status == 200, await resp.text()
        # what the upstream received is the passthrough-fallback request -- tool_result still comes first
        last = mock.last_body["messages"][-1]["content"]
        assert last[0]["type"] == "tool_result", last
        print("✓ test_invalid_wire_falls_back_to_passthrough")
    finally:
        srv_mod.process_anthropic_request = real
        await px_runner.cleanup()
        await up_runner.cleanup()


def test_non_streaming() -> None:
    asyncio.run(_test_non_streaming())


def test_streaming_sse() -> None:
    asyncio.run(_test_streaming_sse())


def test_usage_log_written(tmp_path) -> None:
    asyncio.run(_test_usage_log_written(tmp_path / "usage.jsonl"))


def test_pipeline_error_returns_anthropic_error() -> None:
    asyncio.run(_test_pipeline_error_returns_anthropic_error())


def test_pipeline_failure_falls_back_to_passthrough() -> None:
    asyncio.run(_test_pipeline_failure_falls_back_to_passthrough())


def test_strict_mode_returns_500_on_pipeline_failure() -> None:
    asyncio.run(_test_strict_mode_returns_500_on_pipeline_failure())


def test_retries_transient_connect_failure() -> None:
    asyncio.run(_test_retries_transient_connect_failure())


def test_502_after_exhausting_retries() -> None:
    asyncio.run(_test_502_after_exhausting_retries())


def test_invalid_wire_falls_back_to_passthrough() -> None:
    asyncio.run(_test_invalid_wire_falls_back_to_passthrough())


def test_passthrough_attribution_preserves_claude_code(tmp_path) -> None:
    asyncio.run(_test_passthrough_attribution_preserves_claude_code(tmp_path / "usage.jsonl"))


def test_url_tag_attribution(tmp_path) -> None:
    asyncio.run(_test_url_tag_attribution(tmp_path / "usage.jsonl"))


def test_url_tag_unknown_returns_404() -> None:
    asyncio.run(_test_url_tag_unknown_returns_404())
