"""Harness Reporter endpoint authentication, validation, and dedupe."""

from __future__ import annotations

import asyncio

import aiohttp
from aiohttp import web

from telos.proxy.server import make_app
from telos.trace_store import TraceStore, load_events


async def _start(trace_dir) -> tuple[web.AppRunner, str]:
    app = make_app(
        upstream="http://127.0.0.1:1",
        trace_dir=trace_dir,
        trace_harnesses={
            "codex": {
                "enabled": True,
                "capture": "full",
                "reporter_token": "secret-token",
            },
        },
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def _payload(token: str = "secret-token", kind: str = "tool.finished") -> dict:
    return {
        "harness": "codex",
        "reporter_token": token,
        "session_id": "session-1",
        "events": [{
            "event_id": "event-1",
            "kind": kind,
            "data": {"exit_code": 0},
        }],
    }


async def _test_accept_and_dedupe(tmp_path) -> None:
    runner, url = await _start(tmp_path)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(f"{url}/__telos/reporter/events",
                                   json=_payload()) as response:
                assert response.status == 200
                first = await response.json()
            async with client.post(f"{url}/__telos/reporter/events",
                                   json=_payload()) as response:
                assert response.status == 200
                duplicate = await response.json()
        assert first["accepted"] == [{"event_id": "event-1", "seq": 1, "created": True}]
        assert duplicate["accepted"] == [{"event_id": "event-1", "seq": 1, "created": False}]
        path = TraceStore(tmp_path).path_for("codex", "session-1")
        assert len(load_events(path)) == 1
    finally:
        await runner.cleanup()


async def _test_rejects_bad_token_and_kind(tmp_path) -> None:
    runner, url = await _start(tmp_path)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(f"{url}/__telos/reporter/events",
                                   json=_payload(token="wrong")) as response:
                assert response.status == 401
            async with client.post(f"{url}/__telos/reporter/events",
                                   json=_payload(kind="model.response")) as response:
                assert response.status == 400
    finally:
        await runner.cleanup()


def test_accept_and_dedupe(tmp_path) -> None:
    asyncio.run(_test_accept_and_dedupe(tmp_path))


def test_rejects_bad_token_and_kind(tmp_path) -> None:
    asyncio.run(_test_rejects_bad_token_and_kind(tmp_path))
