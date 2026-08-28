from __future__ import annotations

import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from telos.context_pack import (
    create_context_pack,
    export_context_pack,
    import_context_pack,
    validate_context_pack,
)


def _pack(home: Path, **overrides):
    values = {
        "task_run_id": "run-1",
        "objective": {"goal": "repair the tab state", "acceptance": ["selection persists"]},
        "policy": {"instructions": ["preserve user changes"]},
        "progress": {"done": [], "next": ["reproduce"]},
        "memory": {"facts": [{"text": "polling refreshes detail", "confidence": 1.0}]},
        "provenance": {"harness": "codex", "attempt_id": "attempt-1"},
        "requirements": {"workspace": "read-write", "tools": ["shell", "file-edit"]},
        "home": home,
    }
    values.update(overrides)
    return create_context_pack(**values)


def test_pack_digest_is_deterministic_and_bundle_round_trips(tmp_path) -> None:
    first, first_path = _pack(tmp_path / "one")
    second, _ = _pack(tmp_path / "two")

    assert first["pack_id"] != second["pack_id"]
    assert first["digest"] == second["digest"]
    assert first["layers"] == {
        "objective": "included", "policy": "included", "progress": "included",
        "memory": "included", "conversation": "omitted", "workspace": "omitted",
        "provenance": "included",
    }
    bundle = export_context_pack(first_path, tmp_path / "task.telosbundle")
    second_bundle = export_context_pack(first_path, tmp_path / "task-again.telosbundle")
    assert bundle.read_bytes() == second_bundle.read_bytes()
    imported, imported_path = import_context_pack(bundle, home=tmp_path / "imported")
    assert imported["digest"] == first["digest"]
    assert validate_context_pack(imported_path)["digest"] == first["digest"]
    assert oct(imported_path.stat().st_mode & 0o777) == "0o700"


def test_pack_rejects_checksum_tampering_secret_and_path_traversal(tmp_path) -> None:
    manifest, pack_path = _pack(tmp_path / "home")
    (pack_path / "objective.json").write_text("{}\n")
    with pytest.raises(ValueError, match="checksum mismatch"):
        validate_context_pack(pack_path)

    with pytest.raises(ValueError, match="possible secret"):
        _pack(tmp_path / "secret", objective={"api_key": "sk-abcdefghijklmnopqrstuv"})

    manifest["pack_id"] = "unsafe"
    bundle = tmp_path / "unsafe.telosbundle"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("../escape", "owned")
    with pytest.raises(ValueError, match="unsafe bundle path"):
        import_context_pack(bundle, home=tmp_path / "unsafe-home")
    assert not (tmp_path / "escape").exists()


def test_workspace_snapshot_marks_untracked_files_dirty_and_preserves_patch(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    tracked = repo / "app.py"
    tracked.write_text("print('before')\n")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    tracked.write_text("print('after')\n")
    (repo / "untracked.txt").write_text("not included")

    manifest, pack_path = _pack(tmp_path / "workspace-home", workspace=repo)
    state = json.loads((pack_path / "workspace/state.json").read_text())
    assert manifest["capture_status"] == "dirty"
    assert state["untracked"] == ["untracked.txt"]
    assert "print('after')" in (pack_path / "workspace/changes.patch").read_text()


def test_workspace_secret_can_only_be_omitted_by_explicit_path(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    secret = repo / "fixture.txt"
    secret.write_text("placeholder\n")
    subprocess.run(["git", "-C", str(repo), "add", "fixture.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    secret.write_text("api_key = sk-abcdefghijklmnopqrstuv\n")

    with pytest.raises(ValueError, match="possible secret"):
        _pack(tmp_path / "blocked", workspace=repo)
    manifest, path = _pack(
        tmp_path / "excluded", workspace=repo, workspace_exclude=["fixture.txt"],
    )
    state = json.loads((path / "workspace/state.json").read_text())
    assert manifest["capture_status"] == "dirty"
    assert state["explicitly_excluded"] == ["fixture.txt"]
    assert not (path / "workspace/changes.patch").exists()
