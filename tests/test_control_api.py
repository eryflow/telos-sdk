from __future__ import annotations

from aiohttp import ClientSession, web
import os
import pytest

from telos.proxy.server import PROXY_APP_KEY, make_app
from telos.task_run import main as task_run_main
from telos.tracing import SQLiteTraceStore


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
            response = await session.get(base + "/")
            assert response.status == 200
            assert "Context Control Plane" in await response.text()

            response = await session.post(base + "/api/v1/packs", json={})
            assert response.status == 401
            response = await session.post(
                base + "/api/v1/packs",
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

            response = await session.get(base + f"/api/v1/packs/{pack['id']}")
            detail = await response.json()
            assert response.status == 200
            assert detail["portability"]["kimi-code"]["overall"] == "native"

            response = await session.post(
                base + "/api/v1/handoffs/plan",
                json={"pack_id": pack["id"], "destination": "kimi-code"},
            )
            report = await response.json()
            assert response.status == 200
            assert report["overall"] == "native"

            response = await session.get(base + f"/api/v1/task-runs/{run['id']}")
            lineage = await response.json()
            assert lineage["task_run"]["goal"] == "fix persistent tabs"
            assert lineage["packs"][0]["id"] == pack["id"]

            response = await session.post(
                base + "/api/v1/task-runs",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "goal": "inspect the failing trajectory",
                    "runtime": "kimi-code",
                    "workspace": str(tmp_path),
                    "plugins": ["tracing", "context-pack"],
                },
            )
            created = await response.json()
            assert response.status == 201
            assert created["attempt"]["harness"] == "kimi-code"
            assert created["attempt"]["status"] == "planned"
            assert created["attempt"]["launch_plan"]["plugins"] == [
                "tracing", "context-pack",
            ]
            assert created["command_display"] == (
                f"telos run launch {created['attempt']['id']}"
            )

            response = await session.get(base + "/api/v1/tasks")
            assert response.status == 200
            assert (await response.json())["items"] == []  # TaskRun is not a Long Task

            response = await session.post(
                base + "/api/v1/tasks",
                json={"name": "Optimize scorer", "goal": "Improve the score repeatedly"},
            )
            assert response.status == 401
            response = await session.post(
                base + "/api/v1/tasks",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": "Bad flag", "goal": "Must be rejected",
                    "workspace": {"root": str(tmp_path)}, "self_evolve": "false",
                },
            )
            assert response.status == 400
            response = await session.post(
                base + "/api/v1/tasks",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": "Bad workspace", "goal": "Must be rejected",
                    "workspace": {"path": str(tmp_path)},
                },
            )
            assert response.status == 400
            response = await session.post(
                base + "/api/v1/tasks",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": "Optimize scorer",
                    "goal": "Improve the score repeatedly",
                    "contract": {"acceptance_constraints": ["score > baseline"]},
                    "workspace": str(tmp_path),
                },
            )
            long_task = await response.json()
            assert response.status == 201
            assert long_task["name"] == "Optimize scorer"

            response = await session.post(
                base + f"/api/v1/tasks/{long_task['id']}/executions",
                headers={"Authorization": f"Bearer {token}"},
                json={"harness": "codex"},
            )
            launched = await response.json()
            assert response.status == 201
            execution = launched["execution"]
            assert execution["task_id"] == long_task["id"]
            assert launched["attempt"]["task_execution_id"] == execution["id"]
            assert launched["command_display"] == (
                f"telos run launch {launched['attempt']['id']}"
            )
            response = await session.post(
                base + f"/api/v1/tasks/{long_task['id']}/evolve",
                headers={"Authorization": f"Bearer {token}"},
                json={"execution_id": execution["id"], "knowledge": [], "skills": []},
            )
            assert response.status == 400
            assert "trusted execution outcome" in (await response.json())["error"]

            response = await session.post(
                base + f"/api/v1/tasks/{long_task['id']}/state-patches",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "state": {"status": "running", "next_action": "measure baseline"},
                    "task_execution_id": execution["id"],
                    "evidence_refs": ["trace:test"],
                },
            )
            assert response.status == 201

            response = await session.post(
                base + f"/api/v1/tasks/{long_task['id']}/knowledge",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "kind": "fact", "content": "Baseline score is 0.4",
                    "execution_id": execution["id"], "source_refs": ["trace:test"],
                },
            )
            assert response.status == 201
            response = await session.post(
                base + f"/api/v1/tasks/{long_task['id']}/skills",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": "measure-score", "content": "Run the scorer and record it",
                    "execution_refs": [execution["id"]],
                },
            )
            assert response.status == 400  # one execution may add knowledge, not a Skill
            assert "three distinct trusted executions" in (await response.json())["error"]

            response = await session.post(
                base + f"/api/v1/task-executions/{execution['id']}/outcome",
                headers={"Authorization": f"Bearer {token}"},
                json={"outcome": "pass", "evidence_refs": ["trace:test"]},
            )
            assert response.status == 200
            assert (await response.json())["trusted"] == 1
            extra = [store.create_task_execution(long_task["id"], harness="codex") for _ in range(2)]
            for index, item in enumerate(extra):
                store.set_task_execution_status(
                    item["id"], "completed", outcome="pass",
                    evidence_refs=[f"trace:extra:{index}"], trusted=True,
                )
            skill = store.add_task_skill(
                long_task["id"], name="measure-score", content="Run the scorer",
                execution_refs=[execution["id"], *(item["id"] for item in extra)],
            )
            agent = store.create_task_agent_revision(
                long_task["id"], agent_md="Always run the scorer.",
            )
            response = await session.post(
                base + f"/api/v1/task-skills/{skill['id']}/promote",
                headers={"Authorization": f"Bearer {token}"}, json={},
            )
            assert response.status == 200
            assert (await response.json())["state"] == "production"
            response = await session.post(
                base + f"/api/v1/task-agent-revisions/{agent['id']}/promote",
                headers={"Authorization": f"Bearer {token}"},
                json={"evidence_refs": ["evaluation:test"]},
            )
            assert response.status == 200
            assert (await response.json())["state"] == "production"

            response = await session.get(base + f"/api/v1/tasks/{long_task['id']}")
            detail = await response.json()
            assert response.status == 200
            assert detail["task"]["id"] == long_task["id"]
            assert len(detail["executions"]) == 3
            assert (await (await session.get(
                base + f"/api/v1/tasks/{long_task['id']}/knowledge"
            )).json())["items"][0]["content"] == "Baseline score is 0.4"

            response = await session.post(
                base + "/api/v1/wiki/pages",
                headers={"Authorization": f"Bearer {token}"},
                json={"title": "Scoring experiments", "category": "domains"},
            )
            page = await response.json()
            assert response.status == 201
            claims = []
            for content in ("Baseline is 0.4", "Candidate is 0.6"):
                response = await session.post(
                    base + "/api/v1/wiki/claims",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "page_id": page["id"], "kind": "fact",
                        "content": content, "source_refs": ["trace:test"],
                    },
                )
                assert response.status == 201
                claims.append(await response.json())
            response = await session.post(
                base + "/api/v1/wiki/relations",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "source_claim_id": claims[1]["id"],
                    "target_claim_id": claims[0]["id"],
                    "relation": "applies-to",
                },
            )
            assert response.status == 201
            response = await session.post(
                base + f"/api/v1/tasks/{long_task['id']}/knowledge-bindings",
                headers={"Authorization": f"Bearer {token}"},
                json={"claim_ids": [claims[0]["id"]]},
            )
            manifest = await response.json()
            assert response.status == 201
            assert manifest[0]["id"] == claims[0]["id"]
            response = await session.get(base + "/api/v1/wiki/graph")
            graph = await response.json()
            assert response.status == 200
            assert len(graph["claims"]) == 2
            assert graph["relations"][0]["relation"] == "applies-to"
            assert (await session.get(base + "/__telos/api/v1/tasks")).status == 200
            assert (await session.get(base + "/__telos/api/v1/wiki/graph")).status == 200
    finally:
        await runner.cleanup()


def test_dashboard_attempt_launch_reuses_the_existing_identity(tmp_path, monkeypatch) -> None:
    with SQLiteTraceStore(tmp_path / "telos.db") as store:
        run = store.create_task_run(goal="reuse this task", workspace={"root": str(tmp_path)})
        attempt = store.create_attempt(task_run_id=run["id"], harness="codex")

    launched = []
    monkeypatch.setattr("telos.task_run.telos_home", lambda: tmp_path)
    monkeypatch.setattr("telos.task_run.os.chdir", lambda path: launched.append(str(path)))
    monkeypatch.setattr(
        "telos.cli._cmd_launch_harness",
        lambda runtime, arguments, replace: launched.extend([runtime, arguments, replace]) or 0,
    )
    monkeypatch.setenv("TELOS_ATTEMPT_ID", "")
    monkeypatch.setenv("TELOS_TASK_PLUGINS", "")

    assert task_run_main(["launch", attempt["id"]]) == 0
    assert launched == [
        str(tmp_path), "codex",
        ["exec", "--approve-for-me", "--skip-git-repo-check", "reuse this task"],
        False,
    ]
    assert os.environ["TELOS_TASK_PLUGINS"] == "[]"
    with SQLiteTraceStore(tmp_path / "telos.db") as store:
        assert store.get_attempt(attempt["id"])["status"] == "ok"


def test_long_task_launch_injects_the_frozen_execution_context(tmp_path, monkeypatch) -> None:
    with SQLiteTraceStore(tmp_path / "telos.db") as store:
        task = store.create_task(
            name="blur", goal="make blur faster", contract={"quality": "SSIM >= .98"},
            workspace={"root": str(tmp_path)}, agent_md="Never skip the quality gate.",
        )
        execution = store.create_task_execution(task["id"], harness="codex")
        attempt = store.create_attempt(task_execution_id=execution["id"], harness="codex")

    launched = []
    monkeypatch.setattr("telos.task_run.telos_home", lambda: tmp_path)
    monkeypatch.setattr("telos.task_run.os.chdir", lambda path: None)
    monkeypatch.setenv("TELOS_ATTEMPT_ID", "")
    monkeypatch.setenv("TELOS_TASK_PLUGINS", "")
    monkeypatch.setattr(
        "telos.cli._cmd_launch_harness",
        lambda runtime, arguments, replace: launched.append(arguments[-1]) or 0,
    )

    assert task_run_main(["launch", attempt["id"]]) == 0
    assert "make blur faster" in launched[0]
    assert '"quality": "SSIM >= .98"' in launched[0]
    assert "Never skip the quality gate." in launched[0]
    with SQLiteTraceStore(tmp_path / "telos.db") as store:
        resolved = store.get_task_execution(execution["id"])["execution"]
        assert resolved["status"] == "completed"
        assert resolved["trusted"] == 0  # requires a separate evidenced outcome
