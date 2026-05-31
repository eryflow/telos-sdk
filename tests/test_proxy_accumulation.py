"""End-to-end: after multi-turn requests pass through the proxy, ``cumulative_cache_creation`` truly accumulates.

This is the key test verifying the "full implementation of the mid-term goal". Before the fix,
the proxy created a new ``BridgeSessionState`` every time → any cumulative field reset to 0;
after the fix, requests with the same session_id share one state, and
cumulative_cache_creation / real_requests increase monotonically across calls.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import aiohttp
from aiohttp import web

from telos.proxy.server import make_app


class _CountingUpstream:
    """A mock upstream that reports an accumulating cache_creation_input_tokens on every response."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        # simulate: first cache_write=5000, second 1500, then 0 (already cached)
        self._sequence = [5000, 1500, 0, 0]

    async def handler(self, request: web.Request) -> web.Response:
        self.requests.append(await request.json())
        idx = len(self.requests) - 1
        cache_write = self._sequence[min(idx, len(self._sequence) - 1)]
        return web.json_response({
            "id": f"msg_{idx}", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "OK"}],
            "model": "claude-opus-4-7", "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 50,
                "cache_read_input_tokens": 6500 - cache_write,
                "cache_creation_input_tokens": cache_write,
                "output_tokens": 5,
            },
        })


def _multi_turn_req(turn: int) -> dict:
    """Lengthen messages turn by turn -- multiple turns of the same conversation, with messages[0] unchanged."""
    base_messages = [
        {"role": "user", "content": [{"type": "text",
            "text": "Turn 0 question."}]},
    ]
    extra = []
    for t in range(1, turn + 1):
        extra.extend([
            {"role": "assistant", "content": [{"type": "text",
                "text": f"reply {t}"}]},
            {"role": "user", "content": [{"type": "text",
                "text": f"Turn {t} question."}]},
        ])
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 64,
        "system": [
            {"type": "text", "text": "You are an engineer agent."},
            {"type": "text", "text": "AUTH SPEC:\n" + ("Rule detail line.\n" * 400)},
        ],
        "tools": [
            {"name": "Bash", "input_schema": {"type": "object",
                "properties": {"cmd": {"type": "string"}}}},
        ],
        "messages": base_messages + extra,
    }


async def _test_cumulative_growth_through_proxy(tmp_log: Path) -> None:
    mock = _CountingUpstream()
    up_app = web.Application()
    up_app.router.add_post("/v1/messages", mock.handler)
    up_runner = web.AppRunner(up_app); await up_runner.setup()
    up_site = web.TCPSite(up_runner, "127.0.0.1", 0); await up_site.start()
    up_port = up_site._server.sockets[0].getsockname()[1]

    app = make_app(upstream=f"http://127.0.0.1:{up_port}", usage_log=tmp_log)
    px_runner = web.AppRunner(app); await px_runner.setup()
    px_site = web.TCPSite(px_runner, "127.0.0.1", 0); await px_site.start()
    px_port = px_site._server.sockets[0].getsockname()[1]

    try:
        async with aiohttp.ClientSession() as client:
            headers = {"x-api-key": "sk-cum", "anthropic-version": "2023-06-01"}
            for turn in range(4):
                async with client.post(
                    f"http://127.0.0.1:{px_port}/v1/messages",
                    json=_multi_turn_req(turn),
                    headers=headers,
                ) as r:
                    assert r.status == 200, await r.text()

        # key assertion: cumulative.cache_creation in the log increases monotonically
        lines = [json.loads(l) for l in tmp_log.read_text().strip().splitlines()]
        assert len(lines) == 4
        cums = [rec["cumulative"]["cache_creation"] for rec in lines]
        # expected: 5000 → 6500 → 6500 → 6500
        assert cums == [5000, 6500, 6500, 6500], f"cumulative drift: {cums}"

        # all 4 turns should be the same session_id (derived from content)
        sids = [rec["session_id"] for rec in lines]
        assert len(set(sids)) == 1, f"session_id drifted: {sids}"

        # real_requests_since_refresh also increases monotonically
        reqs = [rec["cumulative"]["real_requests_since_refresh"] for rec in lines]
        assert reqs == [1, 2, 3, 4], f"R8 count drifted: {reqs}"

        # refpool slugs has at least one (the large system document is moved into the ref-pool)
        for rec in lines:
            assert rec["cumulative"]["refpool_slugs"], \
                f"ref-pool should be non-empty (call={rec['call_index']})"
            # all turns share the same slug set
        assert all(rec["cumulative"]["refpool_slugs"] == lines[0]["cumulative"]["refpool_slugs"]
                   for rec in lines), "ref-pool slugs drifted"

        print(f"✓ test_cumulative_growth_through_proxy")
        print(f"  cache_creation: {cums}")
        print(f"  real_requests:  {reqs}")
        print(f"  refpool_slugs:  {lines[0]['cumulative']['refpool_slugs']}")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_different_sessions_dont_share_state(tmp_log: Path) -> None:
    """Two concurrent clients with different api-keys must have independent state."""
    mock = _CountingUpstream()
    up_app = web.Application()
    up_app.router.add_post("/v1/messages", mock.handler)
    up_runner = web.AppRunner(up_app); await up_runner.setup()
    up_site = web.TCPSite(up_runner, "127.0.0.1", 0); await up_site.start()
    up_port = up_site._server.sockets[0].getsockname()[1]

    app = make_app(upstream=f"http://127.0.0.1:{up_port}", usage_log=tmp_log)
    px_runner = web.AppRunner(app); await px_runner.setup()
    px_site = web.TCPSite(px_runner, "127.0.0.1", 0); await px_site.start()
    px_port = px_site._server.sockets[0].getsockname()[1]

    try:
        async with aiohttp.ClientSession() as client:
            for api_key in ("sk-alice", "sk-bob"):
                async with client.post(
                    f"http://127.0.0.1:{px_port}/v1/messages",
                    json=_multi_turn_req(0),
                    headers={"x-api-key": api_key,
                             "anthropic-version": "2023-06-01"},
                ) as r:
                    assert r.status == 200

        lines = [json.loads(l) for l in tmp_log.read_text().strip().splitlines()]
        # the two session_ids must differ
        assert lines[0]["session_id"] != lines[1]["session_id"]
        # each is the 1st request of its session
        assert lines[0]["cumulative"]["real_requests_since_refresh"] == 1
        assert lines[1]["cumulative"]["real_requests_since_refresh"] == 1
        print("✓ test_different_sessions_dont_share_state")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


def test_cumulative_growth_through_proxy(tmp_path) -> None:
    asyncio.run(_test_cumulative_growth_through_proxy(tmp_path / "usage.jsonl"))


def test_different_sessions_dont_share_state(tmp_path) -> None:
    asyncio.run(_test_different_sessions_dont_share_state(tmp_path / "usage.jsonl"))
