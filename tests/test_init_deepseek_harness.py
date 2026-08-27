"""DeepSeek Harness native telemetry adapter/installer checks."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from telos.config import load_config
from telos.init.deepseek_harness import (
    _BEGIN,
    _END,
    DeepSeekHarnessInstaller,
)
from telos.init.__main__ import _make_installer
from telos.tracing import SQLiteTraceStore


OTEL_ROW = {
    "id": "session-telemetry-otel",
    "name": "@deepseek-ai/dsh-session-telemetry-otel",
    "config": {"mode": "DISABLED"},
}


def _installer(tmp_path: Path, *, replace: bool) -> DeepSeekHarnessInstaller:
    dsh_home = tmp_path / ".dsh"
    patch = dsh_home / "profiles" / "web" / "cordis.patch.yml"
    patch.parent.mkdir(parents=True)
    patch.write_text("# user layer\n[]\n", encoding="utf-8")
    return DeepSeekHarnessInstaller(
        proxy_url="http://127.0.0.1:7171",
        profile="web",
        dsh_home=dsh_home,
        asset_path=tmp_path / ".telos" / "integrations" / "adapter.mjs",
        token_path=tmp_path / ".telos" / "integrations" / "token",
        replace_telemetry_backend=replace,
    )


def _fake_dump(installer: DeepSeekHarnessInstaller) -> None:
    def dump() -> tuple[str, list[dict]]:
        text = installer.patch_path.read_text(encoding="utf-8")
        if _BEGIN not in text:
            rows = [dict(OTEL_ROW)]
        else:
            rows = [dict(OTEL_ROW, disabled=True), {
                "id": "session-telemetry-telos",
                "name": installer.asset_path.resolve().as_uri(),
            }]
        return yaml.safe_dump(rows), rows

    installer._dump_config = dump  # type: ignore[method-assign]


def test_init_factory_passes_deepseek_profile_and_replace_flag() -> None:
    installer = _make_installer(
        "deepseek-harness",
        "http://127.0.0.1:7171",
        dsh_profile="research",
        replace_telemetry_backend=True,
    )
    assert installer.profile == "research"
    assert installer.replace_telemetry_backend is True


def test_install_refuses_existing_backend_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TELOS_HOME", str(tmp_path / ".telos"))
    installer = _installer(tmp_path, replace=False)
    _fake_dump(installer)

    with pytest.raises(RuntimeError, match="one sessionTelemetry backend"):
        installer.install()

    assert installer.patch_path.read_text(encoding="utf-8") == "# user layer\n[]\n"
    assert not installer.asset_path.exists()


def test_install_is_idempotent_and_uninstall_removes_only_owned_patch(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("TELOS_HOME", str(tmp_path / ".telos"))
    installer = _installer(tmp_path, replace=True)
    _fake_dump(installer)

    first = installer.install()
    installed = installer.patch_path.read_text(encoding="utf-8")
    assert _BEGIN in installed and _END in installed
    assert "session-telemetry-otel" in installed
    assert "disabled: true" in installed
    assert "session-telemetry-telos" in installed
    assert installer.asset_path in first.changed_files
    assert stat.S_IMODE(installer.token_path.stat().st_mode) == 0o600

    policy = load_config().trace_harnesses["deepseek-harness"]
    assert policy["model_span_source"] == "adapter"
    assert installer.token_path.read_text().strip() == policy["tracing_token"]

    second = installer.install()
    assert second.already_installed is True
    assert installer.patch_path.read_text(encoding="utf-8") == installed
    assert installer.status().already_installed is True

    result = installer.uninstall()
    assert installer.patch_path in result.changed_files
    assert installer.patch_path.read_text(encoding="utf-8") == "# user layer\n[]\n"
    assert installer.asset_path.exists()  # shared across profiles; intentionally retained


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_adapter_maps_native_events_to_trace_span_tree(tmp_path: Path) -> None:
    stub = tmp_path / "telemetry-stub.mjs"
    stub.write_text(
        "export class SessionTelemetryBackend { constructor() {} }\n"
        "export class SessionTelemetryCoordinator { constructor() {} }\n",
        encoding="utf-8",
    )
    asset = Path(__file__).parents[1] / "init" / "assets" / "deepseek_harness_telemetry.mjs"
    records = [
        ["turn/start", 1, {"turn": 1}],
        ["user/message", 2, {
            "id": "user-1", "role": "user", "content": [{"type": "text", "text": "hi"}],
            "source": {"kind": "user"},
        }],
        ["step/start", 3, {"turn": 1, "step": 1}],
        ["request/header", 4, {"header": {"config": {"provider": "deepseek", "model": "v3"}}}],
        ["assistant/chunk", 5, {"turn": 1, "step": 1, "chunk": {"type": "text-delta", "text": "h"}}],
        ["tool/call", 6, {"turn": 1, "step": 1, "callId": "call-1", "name": "bash", "arguments": "{}"}],
        ["tool/result", 7, {
            "turn": 1, "step": 1,
            "message": {
                "source": {"kind": "tool", "callId": "call-1"},
                "content": [{"type": "tool-result", "toolCallId": "call-1", "content": [], "isError": True}],
            },
            "error": {"name": "ToolError", "code": "FAILED"},
        }],
        ["assistant/message", 8, {
            "turn": 1, "step": 1,
            "message": {"source": {"kind": "model", "provider": "deepseek", "model": "v3"}, "content": []},
            "usage": {"inputTokens": 10, "outputTokens": 4, "reasoningTokens": 2},
        }],
        ["step/end", 9, {"turn": 1, "step": 1}],
        ["turn/end", 10, {"turn": 1, "reason": {"kind": "completed"}}],
    ]
    script = """
      const { mapRecordsForTest } = await import(process.env.ASSET_URL)
      const rows = JSON.parse(process.env.RECORDS).map(([type, time, body], seq) => ({
        channel: 'ledger', time, severity: 'info',
        attributes: {'session.id': 'session-1', 'event.type': type, 'event.seq': seq}, body,
      }))
      process.stdout.write(JSON.stringify(mapRecordsForTest(rows)))
    """
    env = os.environ.copy()
    env["TELOS_DSH_TELEMETRY_MODULE_URL"] = stub.resolve().as_uri()
    env["ASSET_URL"] = asset.resolve().as_uri()
    env["RECORDS"] = json.dumps(records)
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    operations = json.loads(completed.stdout)
    spans = [op["body"] for op in operations if op["entity"] == "span"]
    traces = [op["body"] for op in operations if op["entity"] == "trace"]

    llm = next(span for span in reversed(spans) if span["type"] == "llm" and span["status"] == "ok")
    tool = next(span for span in reversed(spans) if span["type"] == "tool")
    assert llm["model"] == "v3"
    assert llm["input_tokens"] == 10
    assert llm["reasoning_tokens"] == 2
    assert llm["ttft_us"] == 2000
    assert tool["status"] == "error"
    assert tool["parent_span_id"] == llm["parent_span_id"]
    assert traces[-1]["status"] == "ok"

    # The adapter speaks the store's public snapshot vocabulary, not SQLite's
    # private ``*_json`` column names.
    for operation in operations:
        body = operation["body"]
        body.pop("project_name", None)
        if operation["entity"] in ("thread", "trace"):
            body["project_id"] = "default"
    with SQLiteTraceStore(tmp_path / "telos.db") as store:
        store.upsert_batch(operations)
        detail = store.get_trace(traces[-1]["id"])
        assert detail is not None
        stored_llm = next(span for span in detail["spans"] if span["type"] == "llm")
        assert stored_llm["ttft_us"] == 2000
        assert stored_llm["usage"]["reasoningTokens"] == 2
