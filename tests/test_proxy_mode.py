"""End-to-end proxy test: mode switch + RTK filtering + compare_group persistence.

Start a mock upstream, run the real proxy, and assert on the wire content / usage_log records.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from telos.corpus import load_session
from telos.output_filter import TelosMode
from telos.proxy.server import make_app


class _MockUpstream:
    """Records the received wire requests, returns a fixed JSON."""

    def __init__(self) -> None:
        self.last_body: dict[str, Any] | None = None

    async def handler(self, request: web.Request) -> web.StreamResponse:
        self.last_body = await request.json()
        return web.json_response({
            "id": "msg_test", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-opus-4-7", "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 500,
                      "cache_creation_input_tokens": 0, "output_tokens": 5},
        })


async def _start_upstream(mock: _MockUpstream) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_post("/v1/messages", mock.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


async def _start_proxy(
    upstream_url: str, *, usage_log: Path | None = None,
    mode: TelosMode | None = None, corpus_dir: Path | None = None,
    record: bool = True,
) -> tuple[web.AppRunner, str]:
    app = make_app(upstream=upstream_url, usage_log=usage_log, mode=mode,
                   corpus_dir=corpus_dir, record=record)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def _req_with_big_tool_result() -> dict[str, Any]:
    """Constructs a request with a large block of repeated bash output (an ideal RTK filtering target)."""
    big = "build start\n" + ("compiling module foo bar baz qux\n" * 300) + "build done\n"
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 64,
        "system": [{"type": "text", "text": "You are an agent."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "build it"}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu1", "name": "Bash",
                 "input": {"command": "cargo build"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": big},
            ]},
        ],
    }


def _tool_result_len(wire: dict[str, Any]) -> int:
    for msg in wire.get("messages", []):
        if msg.get("role") != "user":
            continue
        for item in msg.get("content", []):
            if isinstance(item, dict) and item.get("type") == "tool_result":
                c = item.get("content")
                if isinstance(c, str):
                    return len(c)
    return -1


async def _test_mode_rtk_shrinks_tool_result() -> None:
    """mode=rtk: the tool_result the upstream receives is filtered and shortened, with no cache_control."""
    mock = _MockUpstream()
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)
    try:
        req = _req_with_big_tool_result()
        original_len = _tool_result_len(req)
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=req,
                headers={"x-api-key": "k", "x-telos-mode": "rtk",
                         "x-telos-session": "rtk-test"},
            ) as resp:
                assert resp.status == 200, await resp.text()

        wire = mock.last_body
        assert wire is not None
        assert _tool_result_len(wire) < original_len, "tool_result was not shortened by RTK"
        # rtk-only mode does not apply cache_control
        blocks = list(wire.get("tools") or []) + list(wire.get("system") or [])
        assert not any("cache_control" in b for b in blocks)
        print("✓ test_mode_rtk_shrinks_tool_result")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_mode_none_is_byte_identical() -> None:
    """mode=none: pure passthrough, the body the upstream receives is exactly the original raw."""
    mock = _MockUpstream()
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)
    try:
        req = _req_with_big_tool_result()
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=req,
                headers={"x-api-key": "k", "x-telos-mode": "none",
                         "x-telos-session": "none-test"},
            ) as resp:
                assert resp.status == 200

        assert mock.last_body == req, "mode=none should pass through unchanged"
        print("✓ test_mode_none_is_byte_identical")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_mode_telos_marks_cache_control() -> None:
    """mode=telos: runs the pipeline and applies cache_control, but tool_result is not filtered."""
    mock = _MockUpstream()
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)
    try:
        req = _req_with_big_tool_result()
        original_len = _tool_result_len(req)
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=req,
                headers={"x-api-key": "k", "x-telos-mode": "telos",
                         "x-telos-session": "telos-test"},
            ) as resp:
                assert resp.status == 200

        wire = mock.last_body
        blocks = list(wire.get("tools") or []) + list(wire.get("system") or [])
        assert any("cache_control" in b for b in blocks), "mode=telos should have cache_control"
        # TELOS does not touch the tool_result text
        assert _tool_result_len(wire) == original_len
        print("✓ test_mode_telos_marks_cache_control")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_compare_group_and_reduction_logged(tmp_log: Path) -> None:
    """mode=both + compare_group header: usage_log records mode / group / filtered amount."""
    mock = _MockUpstream()
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url, usage_log=tmp_log)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=_req_with_big_tool_result(),
                headers={"x-api-key": "k", "x-telos-mode": "both",
                         "x-telos-session": "cmp-test",
                         "x-telos-compare-group": "task-42"},
            ) as resp:
                assert resp.status == 200

        record = json.loads(tmp_log.read_text().strip().splitlines()[-1])
        assert record["mode"] == "both"
        assert record["compare_group"] == "task-42"
        red = record["tool_output_reduction"]
        assert red["blocks_filtered"] == 1
        assert red["saved_chars"] > 0
        print("✓ test_compare_group_and_reduction_logged")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_mode_is_sticky_per_session() -> None:
    """The first request sets mode=rtk via header; later requests in the same session without a header still use rtk."""
    mock = _MockUpstream()
    up_runner, up_url = await _start_upstream(mock)
    # the proxy process defaults to mode=telos -- if sticky fails, the second request would apply cache_control
    px_runner, px_url = await _start_proxy(up_url, mode=TelosMode.from_label("telos"))
    try:
        async with aiohttp.ClientSession() as client:
            # turn 1: explicit header rtk
            async with client.post(
                f"{px_url}/v1/messages", json=_req_with_big_tool_result(),
                headers={"x-api-key": "k", "x-telos-mode": "rtk",
                         "x-telos-session": "sticky-test"},
            ) as resp:
                assert resp.status == 200
            # turn 2: same session, no header
            async with client.post(
                f"{px_url}/v1/messages", json=_req_with_big_tool_result(),
                headers={"x-api-key": "k", "x-telos-session": "sticky-test"},
            ) as resp:
                assert resp.status == 200

        wire = mock.last_body
        blocks = list(wire.get("tools") or []) + list(wire.get("system") or [])
        assert not any("cache_control" in b for b in blocks), \
            "sticky failed: the second request fell back to the default telos mode"
        print("✓ test_mode_is_sticky_per_session")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_proxy_records_corpus(corpus_dir: Path) -> None:
    """The proxy records the original request into the corpus by default; replay can load it back afterward."""
    mock = _MockUpstream()
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url, corpus_dir=corpus_dir)
    try:
        async with aiohttp.ClientSession() as client:
            for _ in range(2):
                async with client.post(
                    f"{px_url}/v1/messages", json=_req_with_big_tool_result(),
                    headers={"x-api-key": "k", "x-telos-mode": "both",
                             "x-telos-session": "corpus-test"},
                ) as resp:
                    assert resp.status == 200

        turns = load_session(corpus_dir, "corpus-test")
        assert len(turns) == 2, f"the corpus should record 2 turns, got {len(turns)}"
        # what is recorded is the original request (not shortened by RTK)
        tr_len = _tool_result_len(turns[1]["request"])
        assert tr_len == _tool_result_len(_req_with_big_tool_result())
        print("✓ test_proxy_records_corpus")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_no_record_disables_corpus(corpus_dir: Path) -> None:
    mock = _MockUpstream()
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url, corpus_dir=corpus_dir,
                                           record=False)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=_req_with_big_tool_result(),
                headers={"x-api-key": "k", "x-telos-session": "norecord-test"},
            ) as resp:
                assert resp.status == 200
        assert not (corpus_dir / "norecord-test.jsonl").exists(), \
            "should not write the corpus when --no-record is set"
        print("✓ test_no_record_disables_corpus")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


def test_mode_rtk_shrinks_tool_result() -> None:
    asyncio.run(_test_mode_rtk_shrinks_tool_result())


def test_mode_none_is_byte_identical() -> None:
    asyncio.run(_test_mode_none_is_byte_identical())


def test_mode_telos_marks_cache_control() -> None:
    asyncio.run(_test_mode_telos_marks_cache_control())


def test_compare_group_and_reduction_logged(tmp_path) -> None:
    asyncio.run(_test_compare_group_and_reduction_logged(tmp_path / "usage.jsonl"))


def test_mode_is_sticky_per_session() -> None:
    asyncio.run(_test_mode_is_sticky_per_session())


def test_proxy_records_corpus(tmp_path) -> None:
    asyncio.run(_test_proxy_records_corpus(tmp_path / "corpus"))


def test_no_record_disables_corpus(tmp_path) -> None:
    asyncio.run(_test_no_record_disables_corpus(tmp_path / "corpus"))
