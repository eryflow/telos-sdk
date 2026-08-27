"""Codex Hook → Trace/Span adapter and isolated plugin bundle tests."""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import pytest

from telos import config as config_module
from telos.codex_tracing import (
    HOOK_EVENTS,
    map_codex_hook,
    post_codex_operations,
    run_codex_hook,
)
from telos.init.codex import CodexInstaller
from telos.tracing import SQLiteTraceStore


def _payload(event: str) -> dict:
    return {
        "hook_event_name": event,
        "session_id": "session-1",
        "turn_id": "turn-2",
        "transcript_path": "/tmp/transcript.jsonl",
        "agent_transcript_path": "/tmp/agent.jsonl",
        "cwd": "/tmp/work",
        "model": "gpt-5.6-codex",
        "permission_mode": "on-request",
        "source": "startup",
        "reason": "other",
        "prompt": "fix the bug",
        "agent_id": "agent-3",
        "agent_type": "worker",
        "tool_name": "Bash",
        "tool_input": {"command": "pwd"},
        "tool_response": {"output": "/tmp/work", "success": True},
        "tool_use_id": "tool-4",
        "trigger": "auto",
        "last_assistant_message": "done",
    }


@pytest.mark.parametrize(
    ("event", "last_entity", "last_type", "last_status"),
    [
        ("SessionStart", "thread", None, "running"),
        ("UserPromptSubmit", "trace", None, "running"),
        ("PreToolUse", "span", "tool", "running"),
        ("PostToolUse", "span", "tool", "ok"),
        ("PermissionRequest", "span", "approval", "unknown"),
        ("PreCompact", "span", "compaction", "running"),
        ("PostCompact", "span", "compaction", "ok"),
        ("SubagentStart", "span", "agent", "running"),
        ("SubagentStop", "span", "agent", "ok"),
        ("Stop", "trace", None, "ok"),
        ("Interrupt", "trace", None, "cancelled"),
        ("SessionEnd", "thread", None, "ok"),
    ],
)
def test_maps_all_codex_hook_events(
    event: str, last_entity: str, last_type: str | None, last_status: str
) -> None:
    operations = map_codex_hook(_payload(event), 1_700_000_000_000_000)
    assert all(operation["op"] == "upsert" for operation in operations)
    assert operations[-1]["entity"] == last_entity
    body = operations[-1]["body"]
    assert body["status"] == last_status
    if last_type:
        assert body["type"] == last_type


def test_mapping_uses_stable_uuid5_and_prerequisites() -> None:
    first = map_codex_hook(_payload("PreToolUse"), 100)
    second = map_codex_hook(_payload("PreToolUse"), 200)
    assert [item["entity"] for item in first] == ["thread", "trace", "span", "span"]
    assert [item["body"]["id"] for item in first] == [
        item["body"]["id"] for item in second
    ]
    assert all(uuid.UUID(item["body"]["id"]).version == 5 for item in first)
    assert first[0]["body"]["project_name"] == "default"
    assert first[1]["body"]["project_name"] == "default"
    assert first[-1]["body"]["parent_span_id"] == first[-2]["body"]["id"]


def test_post_tool_error_and_permission_are_observation_only() -> None:
    failed = _payload("PostToolUse")
    failed["tool_response"] = {"success": False, "error": "boom"}
    tool = map_codex_hook(failed, 100)[-1]["body"]
    assert tool["status"] == "error"
    assert tool["error"] == "boom"

    approval = map_codex_hook(_payload("PermissionRequest"), 100)[-1]["body"]
    assert approval["status"] == "unknown"
    assert approval["metadata"] == {"decision": "requested"}
    assert "output" not in approval


def test_mapper_batch_contract_and_session_cleanup(tmp_path: Path) -> None:
    with SQLiteTraceStore(tmp_path / "tracing.db") as store:
        store.upsert_batch(map_codex_hook(_payload("UserPromptSubmit"), 100))
        store.upsert_batch(map_codex_hook(_payload("PreToolUse"), 200))
        store.upsert_batch(map_codex_hook(_payload("SessionEnd"), 300))
        trace_id = map_codex_hook(_payload("UserPromptSubmit"), 100)[1]["body"]["id"]
        detail = store.get_trace(trace_id)
        assert detail is not None
        assert detail["trace"]["status"] == "abandoned"
        assert {span["status"] for span in detail["spans"]} == {"abandoned"}


def test_hook_runner_is_silent_and_fail_open(capsys: pytest.CaptureFixture[str]) -> None:
    def unavailable(_operations: list[dict]) -> None:
        raise RuntimeError("gateway down")

    rc = run_codex_hook(
        io.StringIO(json.dumps(_payload("PermissionRequest"))),
        sender=unavailable,
        clock_us=lambda: 100,
    )
    assert rc == 0
    assert capsys.readouterr() == ("", "")
    assert run_codex_hook(io.StringIO("not-json"), sender=unavailable) == 0


def test_sender_uses_config_token_as_bearer_not_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELOS_HOME", str(tmp_path / "telos"))
    config, _ = config_module.enable_harness_trace("codex")
    expected_token = config.trace_harnesses["codex"]["tracing_token"]

    class State:
        host = "127.0.0.1"
        port = 7171

    monkeypatch.setattr("telos.codex_tracing.daemon.read_state", lambda: State())
    captured: dict = {}

    def post(host: str, port: int, payload: dict, *, token: str) -> dict:
        captured.update(host=host, port=port, payload=payload, token=token)
        return {"results": []}

    monkeypatch.setattr("telos.codex_tracing.control.post_trace_batch", post)
    operation = {"entity": "thread", "op": "upsert", "body": {"id": "x"}}
    post_codex_operations([operation])
    assert captured["token"] == expected_token
    assert captured["payload"] == {"schema_version": 1, "operations": [operation]}
    assert expected_token not in json.dumps(captured["payload"])


def test_installer_owns_only_its_plugin_files(tmp_path: Path) -> None:
    config_path = tmp_path / "codex" / "config.toml"
    config_path.parent.mkdir()
    fallback_path = config_path.parent / "hooks.json"
    user_hook = {
        "type": "command",
        "command": "python3 /tmp/user-hook.py",
        "timeout": 10,
    }
    original_hooks = {
        "description": "user hooks",
        "hooks": {
            "PreToolUse": [{"matcher": "^Bash$", "hooks": [user_hook]}],
        },
    }
    fallback_path.write_text(json.dumps(original_hooks), encoding="utf-8")
    plugin_path = tmp_path / "telos-tracing"
    plugin_path.mkdir()
    user_file = plugin_path / "keep-me.txt"
    user_file.write_text("user content", encoding="utf-8")
    installer = CodexInstaller(
        proxy_url="http://127.0.0.1:7171",
        config_path=config_path,
        trace_plugin_path=plugin_path,
        hooks_path=fallback_path,
    )

    first = installer.install()
    manifest_path = plugin_path / ".codex-plugin/plugin.json"
    plugin_hooks_path = plugin_path / "hooks/hooks.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hooks = json.loads(plugin_hooks_path.read_text(encoding="utf-8"))["hooks"]
    assert manifest["name"] == "telos-tracing"
    assert manifest["hooks"] == "./hooks/hooks.json"
    assert tuple(hooks) == HOOK_EVENTS
    assert all(
        group[0]["hooks"][0]["command"] == "telos trace-hook codex"
        for group in hooks.values()
    )
    assert any("fallback enabled" in note for note in first.notes)
    fallback = json.loads(fallback_path.read_text(encoding="utf-8"))
    assert fallback["description"] == "user hooks"
    assert fallback["hooks"]["PreToolUse"][0] == original_hooks["hooks"]["PreToolUse"][0]
    assert all(
        sum(
            handler.get("command") == "telos trace-hook codex"
            for group in fallback["hooks"][event]
            for handler in group["hooks"]
        ) == 1
        for event in HOOK_EVENTS
    )
    assert fallback_path.with_suffix(".json.telos.bak").is_file()

    before = plugin_hooks_path.read_text(encoding="utf-8")
    second = installer.install()
    assert plugin_hooks_path.read_text(encoding="utf-8") == before
    assert second.already_installed
    assert manifest_path not in second.changed_files
    assert plugin_hooks_path not in second.changed_files

    plugin_hooks_path.write_text("{}", encoding="utf-8")
    repaired = installer.install()
    assert plugin_hooks_path in repaired.changed_files
    assert json.loads(plugin_hooks_path.read_text(encoding="utf-8"))["hooks"]

    # A user hook added after TELOS installation must also survive uninstall.
    fallback = json.loads(fallback_path.read_text(encoding="utf-8"))
    fallback["hooks"]["PostToolUse"].append(
        {"matcher": "^Read$", "hooks": [user_hook]}
    )
    fallback_path.write_text(json.dumps(fallback), encoding="utf-8")

    removed = installer.uninstall()
    assert manifest_path in removed.changed_files
    assert plugin_hooks_path in removed.changed_files
    assert not manifest_path.exists()
    assert not plugin_hooks_path.exists()
    assert user_file.read_text(encoding="utf-8") == "user content"
    assert plugin_path.is_dir()
    cleaned = json.loads(fallback_path.read_text(encoding="utf-8"))
    assert cleaned["description"] == "user hooks"
    assert cleaned["hooks"]["PreToolUse"] == original_hooks["hooks"]["PreToolUse"]
    assert cleaned["hooks"]["PostToolUse"] == [
        {"matcher": "^Read$", "hooks": [user_hook]}
    ]
    assert not any(
        handler.get("command") == "telos trace-hook codex"
        for groups in cleaned["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    )


def test_uninstall_removes_telos_only_fallback_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    hooks_path = tmp_path / "hooks.json"
    installer = CodexInstaller(config_path=config_path, hooks_path=hooks_path)
    installer.install()
    assert hooks_path.is_file()
    installer.uninstall()
    assert not hooks_path.exists()


def test_installer_registers_native_plugin_and_skips_fallback(tmp_path: Path) -> None:
    config_path = tmp_path / "codex" / "config.toml"
    plugin_path = tmp_path / "telos-marketplace" / "telos-tracing"
    hooks_path = tmp_path / "codex" / "hooks.json"
    installer = CodexInstaller(
        config_path=config_path,
        trace_plugin_path=plugin_path,
        hooks_path=hooks_path,
        register_trace_plugin=True,
    )
    state = {"marketplace": False, "plugin": False}
    calls: list[tuple[str, ...]] = []

    def run(arguments: list[str]) -> dict[str, object]:
        calls.append(tuple(arguments))
        if arguments[:3] == ["plugin", "marketplace", "list"]:
            return {"marketplaces": ([{
                "name": "telos-local",
                "root": str(plugin_path.parent.resolve()),
            }] if state["marketplace"] else [])}
        if arguments[:3] == ["plugin", "marketplace", "add"]:
            state["marketplace"] = True
            return {"name": "telos-local"}
        if arguments[:2] == ["plugin", "list"]:
            return {"installed": ([{
                "pluginId": "telos-tracing@telos-local",
                "enabled": True,
            }] if state["plugin"] else [])}
        if arguments[:2] == ["plugin", "add"]:
            state["plugin"] = True
            return {"pluginId": "telos-tracing@telos-local"}
        if arguments[:2] == ["plugin", "remove"]:
            state["plugin"] = False
            return {"removed": True}
        if arguments[:3] == ["plugin", "marketplace", "remove"]:
            state["marketplace"] = False
            return {"removed": True}
        raise AssertionError(arguments)

    installer._run_codex_json = run  # type: ignore[method-assign]
    result = installer.install()

    assert state == {"marketplace": True, "plugin": True}
    assert any("registered and enabled" in note for note in result.notes)
    assert not hooks_path.exists()
    marketplace = json.loads(
        installer.trace_marketplace_manifest.read_text(encoding="utf-8")
    )
    assert marketplace["plugins"][0]["name"] == "telos-tracing"

    installer.uninstall()
    assert state == {"marketplace": False, "plugin": False}
    assert not installer.trace_marketplace_manifest.exists()
    assert ("plugin", "remove", "telos-tracing@telos-local", "--json") in calls


def test_installer_does_not_overwrite_invalid_user_hooks(tmp_path: Path) -> None:
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text("{ user-owned invalid json", encoding="utf-8")
    installer = CodexInstaller(config_path=tmp_path / "config.toml", hooks_path=hooks_path)
    result = installer.install()
    assert hooks_path.read_text(encoding="utf-8") == "{ user-owned invalid json"
    assert any("fallback not enabled" in note for note in result.notes)
    installer.uninstall()
    assert hooks_path.read_text(encoding="utf-8") == "{ user-owned invalid json"
