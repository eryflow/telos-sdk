from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from telos.context_pack import create_context_pack
from telos.handoff import compatibility_report, create_launch_plan, prepare_handoff
from telos.tracing import SQLiteTraceStore


def _workspace(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "app.py").write_text("selected = 'input'\n")
    subprocess.run(["git", "-C", str(path), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True)
    (path / "app.py").write_text("selected = user_selection\n")
    return path


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nprintf '%s\\n' \"$TELOS_ATTEMPT_ID\" \"$@\"\n")
    path.chmod(0o755)
    return path


def _registered_pack(store: SQLiteTraceStore, home: Path, workspace: Path):
    task_type = store.ensure_task_type("code-defect-repair")
    run = store.create_task_run(
        goal="make tab selection persist", task_type_id=task_type["id"],
        workspace={"root": str(workspace)},
    )
    source = store.create_attempt(task_run_id=run["id"], harness="codex")
    manifest, path = create_context_pack(
        task_run_id=run["id"], source_attempt_id=source["id"],
        task_type="code-defect-repair", home=home, workspace=workspace,
        objective={"goal": run["goal"], "constraints": ["preserve user changes"]},
        policy={"instructions": ["test the root cause"]},
        progress={"done": ["reproduced"], "next": ["patch polling refresh"]},
        memory={"decisions": ["selection state belongs in the UI"]},
        conversation=[{"role": "user", "content": "the buttons jump back to input"}],
        provenance={"harness": "codex", "attempt_id": source["id"]},
        requirements={"workspace": "read-write", "tools": ["shell", "file-edit"]},
    )
    store.register_context_pack(manifest, path)
    return run, source, manifest, path


def test_capability_report_and_launch_plans_are_explicit(tmp_path) -> None:
    workspace = _workspace(tmp_path / "repo")
    with SQLiteTraceStore(tmp_path / "trace.db") as store:
        _, _, _, pack_path = _registered_pack(store, tmp_path / "telos", workspace)
        report = compatibility_report(pack_path, "kimi-code")
        assert report["overall"] == "degraded"
        assert report["layers"]["objective"]["level"] == "native"
        assert report["layers"]["conversation"]["level"] == "degraded"
        deepseek = compatibility_report(pack_path, "deepseek-harness")
        assert deepseek["overall"] == "degraded"
        assert deepseek["capabilities"]["lifecycle_hooks"] == ["session", "turn", "tool", "model"]

        fake_kimi = _executable(tmp_path / "kimi")
        plan = create_launch_plan(
            pack_path, "kimi-code", attempt_id="attempt-kimi", home=tmp_path / "launch",
            executables={"kimi-code": str(fake_kimi)},
        )
        assert plan["command"][:2] == [str(fake_kimi), "--agent-file"]
        handoff = Path(plan["context_file"]).read_text()
        assert "make tab selection persist" in handoff
        assert "reproduced" in handoff
        assert "selection state belongs in the UI" in handoff
        assert "patch polling refresh" in handoff
        assert oct(Path(plan["context_file"]).stat().st_mode & 0o777) == "0o600"


def test_prepare_handoff_creates_new_attempt_and_env_links_trace(tmp_path) -> None:
    workspace = _workspace(tmp_path / "repo")
    fake_kimi = _executable(tmp_path / "kimi")
    with SQLiteTraceStore(tmp_path / "trace.db") as store:
        run, source, manifest, _ = _registered_pack(store, tmp_path / "telos", workspace)
        plan, destination = prepare_handoff(
            store, pack_id=manifest["pack_id"], destination="kimi-code",
            home=tmp_path / "telos", executables={"kimi-code": str(fake_kimi)},
        )
        assert destination["source_attempt_id"] == source["id"]
        assert destination["task_run_id"] == run["id"]
        assert destination["context_pack_id"] == manifest["pack_id"]
        assert plan["environment"]["TELOS_ATTEMPT_ID"] == destination["id"]

        result = subprocess.run(
            plan["command"], cwd=plan["cwd"],
            env={**os.environ, **plan["environment"]}, capture_output=True, text=True, check=True,
        )
        assert destination["id"] in result.stdout
        lineage = store.get_task_run(run["id"])
        assert [attempt["harness"] for attempt in lineage["attempts"]] == ["codex", "kimi-code"]
        assert lineage["handoffs"][0]["destination_attempt_id"] == destination["id"]
        assert json.loads((Path(plan["context_file"]).parent / "launch.json").read_text())["attempt_id"] == destination["id"]


def test_reverse_kimi_to_codex_keeps_one_task_run(tmp_path) -> None:
    workspace = _workspace(tmp_path / "repo")
    fake_kimi = _executable(tmp_path / "kimi")
    fake_codex = _executable(tmp_path / "codex")
    with SQLiteTraceStore(tmp_path / "trace.db") as store:
        run, _, first_pack, _ = _registered_pack(store, tmp_path / "telos", workspace)
        _, kimi = prepare_handoff(
            store, pack_id=first_pack["pack_id"], destination="kimi-code",
            home=tmp_path / "telos", executables={"kimi-code": str(fake_kimi)},
        )
        second_manifest, second_path = create_context_pack(
            home=tmp_path / "telos", task_run_id=run["id"], source_attempt_id=kimi["id"],
            parent_pack_id=first_pack["pack_id"], workspace=workspace,
            objective={"goal": run["goal"]}, policy={"source": "kimi"},
            progress={"done": ["patched"], "next": ["verify in Codex"]},
            memory={"decisions": ["keep state outside polling"]},
            provenance={"harness": "kimi-code", "attempt_id": kimi["id"]},
            requirements={"workspace": "read-write", "tools": ["shell", "file-edit"]},
        )
        store.register_context_pack(second_manifest, second_path)
        _, codex = prepare_handoff(
            store, pack_id=second_manifest["pack_id"], destination="codex",
            home=tmp_path / "telos", executables={"codex": str(fake_codex)},
        )
        lineage = store.get_task_run(run["id"])
        assert [attempt["harness"] for attempt in lineage["attempts"]] == [
            "codex", "kimi-code", "codex",
        ]
        assert codex["source_attempt_id"] == kimi["id"]
        assert lineage["packs"][-1]["parent_pack_id"] == first_pack["pack_id"]
