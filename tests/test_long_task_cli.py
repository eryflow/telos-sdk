from __future__ import annotations

import contextlib
import io

from telos import cli
from telos.tracing import SQLiteTraceStore


def _run(argv: list[str]) -> tuple[int, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            result = cli.main(argv)
        except SystemExit as exc:
            result = int(exc.code)
    return result, output.getvalue()


def test_task_cli_is_explicit_and_freezes_an_execution(tmp_path, monkeypatch) -> None:
    home = tmp_path / "telos"
    monkeypatch.setenv("TELOS_HOME", str(home))

    result, output = _run([
        "task", "create", "--name", "blur optimizer", "--goal", "make blur faster",
        "--workspace", str(tmp_path),
    ])
    assert result == 0
    task_id = output.split("Task ", 1)[1].split()[0]

    state_file = tmp_path / "state.json"
    state_file.write_text('{"status":"ready"}')
    result, output = _run([
        "task", "checkpoint", task_id, "--state-file", str(state_file),
        "--evidence", "trace:setup",
    ])
    assert result == 0
    assert "state " in output and "→ ready" in output
    with SQLiteTraceStore(home / "telos.db") as store:
        page = store.create_wiki_page(title="Blur knowledge")
        claim = store.add_wiki_claim(page["id"], content="Measure the baseline first")
        store.bind_task_knowledge(task_id, [claim["id"]])
    result, output = _run(["task", "show", task_id])
    assert result == 0
    assert "State " in output and "ready" in output
    assert "1 Wiki" in output

    result, output = _run([
        "task", "execute", task_id, "--harness", "codex", "--no-exec",
    ])
    assert result == 0
    assert "TaskExecution " in output and "telos run launch " in output

    with SQLiteTraceStore(home / "telos.db") as store:
        assert len(store.list_tasks()) == 1
        detail = store.get_task(task_id)
        assert detail is not None
        assert len(detail["executions"]) == 1
        assert detail["executions"][0]["state_revision_id"] is not None


def test_ordinary_run_does_not_create_a_long_task(tmp_path, monkeypatch) -> None:
    home = tmp_path / "telos"
    monkeypatch.setenv("TELOS_HOME", str(home))

    result, _ = _run([
        "run", "start", "--task", "legacy", "--goal", "one turn",
        "--harness", "codex", "--workspace", str(tmp_path), "--no-exec",
    ])
    assert result == 0
    with SQLiteTraceStore(home / "telos.db") as store:
        assert store.list_tasks() == []
        assert store.list_task_runs()[0]["task_id"] is None


def test_task_cli_resolves_execution_and_promotes_candidates(tmp_path, monkeypatch) -> None:
    home = tmp_path / "telos"
    monkeypatch.setenv("TELOS_HOME", str(home))
    with SQLiteTraceStore(home / "telos.db") as store:
        task = store.create_task(name="blur", goal="make blur faster")
        executions = [store.create_task_execution(task["id"], harness="codex") for _ in range(3)]

    for index, execution in enumerate(executions):
        result, output = _run([
            "task", "outcome", execution["id"], "--outcome", "pass",
            "--evidence", f"trace:{index}",
        ])
        assert result == 0
        assert "trusted pass" in output

    with SQLiteTraceStore(home / "telos.db") as store:
        skill = store.add_task_skill(
            task["id"], name="benchmark", content="run gates",
            execution_refs=[item["id"] for item in executions],
        )
        agent = store.create_task_agent_revision(task["id"], agent_md="Run the benchmark.")

    assert _run(["task", "promote-skill", skill["id"]])[0] == 0
    assert _run([
        "task", "promote-agent", agent["id"], "--evidence", "evaluation:1",
    ])[0] == 0
    with SQLiteTraceStore(home / "telos.db") as store:
        assert store.list_task_skills(task["id"])[0]["state"] == "production"
        assert store.get_task(task["id"])["current_agent"]["id"] == agent["id"]
