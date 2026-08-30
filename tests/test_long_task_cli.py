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
