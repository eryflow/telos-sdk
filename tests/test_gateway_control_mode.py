"""``/__telos/control/mode`` tests: hot-update default mode + reject invalid label."""

from __future__ import annotations

import asyncio

import aiohttp
from aiohttp import web

from telos.output_filter import TelosMode
from telos.proxy.server import make_app


async def _start(mode: TelosMode | None = None) -> tuple[web.AppRunner, str]:
    app = make_app(upstream="http://127.0.0.1:1", mode=mode)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


async def _test_get_then_post() -> None:
    runner, url = await _start(mode=TelosMode.from_label("telos"))
    try:
        async with aiohttp.ClientSession() as c:
            async with c.get(f"{url}/__telos/control/mode") as r:
                assert r.status == 200
                assert (await r.json())["mode"] == "telos"

            # hot-switch to both
            async with c.post(f"{url}/__telos/control/mode",
                              json={"mode": "both"}) as r:
                assert r.status == 200
                body = await r.json()
                assert body["mode"] == "both"
                assert body["telos"] is True and body["rtk"] is True

            # GET confirms the change took effect
            async with c.get(f"{url}/__telos/control/mode") as r:
                assert (await r.json())["mode"] == "both"
    finally:
        await runner.cleanup()
    print("✓ test_get_then_post")


async def _test_bad_label_rejected() -> None:
    runner, url = await _start()
    try:
        async with aiohttp.ClientSession() as c:
            async with c.post(f"{url}/__telos/control/mode",
                              json={"mode": "turbo"}) as r:
                assert r.status == 400, await r.text()
    finally:
        await runner.cleanup()
    print("✓ test_bad_label_rejected")


def test_get_then_post() -> None:
    asyncio.run(_test_get_then_post())


def test_bad_label_rejected() -> None:
    asyncio.run(_test_bad_label_rejected())
