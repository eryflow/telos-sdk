from __future__ import annotations

from aiohttp import ClientSession, web
import pytest

from telos.proxy.server import PROXY_APP_KEY, make_app


@pytest.mark.asyncio
async def test_control_api_creates_pack_and_reports_handoff_without_guessing(tmp_path) -> None:
    app = make_app(tracing_db=tmp_path / "telos.db")
    store = app[PROXY_APP_KEY]._tracing_store
    task = store.ensure_task_type("code-defect-repair")
    run = store.create_task_run(goal="fix persistent tabs", task_type_id=task["id"])
    attempt = store.create_attempt(task_run_id=run["id"], harness="codex")
    token = (tmp_path / "control.token").read_text().strip()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    try:
        async with ClientSession() as session:
            response = await session.get(base + "/__telos")
            assert response.status == 200
            assert "Context Control Plane" in await response.text()

            response = await session.post(base + "/__telos/api/v1/packs", json={})
            assert response.status == 401
            response = await session.post(
                base + "/__telos/api/v1/packs",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "attempt_id": attempt["id"],
                    "progress": {"done": ["reproduced"], "next": ["patch"]},
                    "memory": {"decisions": ["keep UI selection independent"]},
                },
            )
            assert response.status == 201
            pack = await response.json()
            assert pack["source_attempt_id"] == attempt["id"]

            response = await session.get(base + f"/__telos/api/v1/packs/{pack['id']}")
            detail = await response.json()
            assert response.status == 200
            assert detail["portability"]["kimi-code"]["overall"] == "native"

            response = await session.post(
                base + "/__telos/api/v1/handoffs/plan",
                json={"pack_id": pack["id"], "destination": "kimi-code"},
            )
            report = await response.json()
            assert response.status == 200
            assert report["overall"] == "native"

            response = await session.get(base + f"/__telos/api/v1/task-runs/{run['id']}")
            lineage = await response.json()
            assert lineage["task_run"]["goal"] == "fix persistent tabs"
            assert lineage["packs"][0]["id"] == pack["id"]
    finally:
        await runner.cleanup()
