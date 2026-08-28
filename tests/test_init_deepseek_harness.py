"""DeepSeek Harness native telemetry adapter/installer checks."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

from aiohttp import ClientSession, web
import pytest
import yaml

from telos.config import load_config
from telos.init.deepseek_harness import (
    _BEGIN,
    _END,
    DeepSeekHarnessInstaller,
)
from telos.init.__main__ import _make_installer
from telos.proxy.server import make_app
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
        dsh_executable="/opt/dsh/bin/dsh",
        replace_telemetry_backend=True,
    )
    assert installer.profile == "research"
    assert installer.dsh_executable == "/opt/dsh/bin/dsh"
    assert installer.replace_telemetry_backend is True


def test_dump_rejects_unrelated_dsh_for_bundle_profile(
    tmp_path: Path, monkeypatch,
) -> None:
    installer = _installer(tmp_path, replace=True)
    installer.patch_path.with_name("package.json").write_text(
        json.dumps({
            "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 1, stdout="", stderr="unrecognized option '--profile'",
    ))

    with pytest.raises(RuntimeError, match="not the DeepSeek Harness CLI"):
        installer._dump_config()


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
        ["assistant/message", 6, {
            "turn": 1, "step": 1,
            "message": {"source": {"kind": "model", "provider": "deepseek", "model": "v3"}, "content": []},
            "usage": {"inputTokens": 10, "outputTokens": 4, "reasoningTokens": 2},
        }],
        ["request/header", 7, {"header": {"config": {"provider": "deepseek", "model": "v3"}}}],
        ["assistant/chunk", 8, {"turn": 1, "step": 1, "chunk": {"type": "text-delta", "text": "i"}}],
        ["assistant/message", 9, {
            "turn": 1, "step": 1,
            "message": {"source": {"kind": "model", "provider": "deepseek", "model": "v3"}, "content": []},
            "usage": {"inputTokens": 3, "outputTokens": 2},
        }],
        ["tool/call", 10, {"turn": 1, "step": 1, "callId": "call-1", "name": "bash", "arguments": "{}"}],
        ["tool/result", 11, {
            "turn": 1, "step": 1,
            "message": {
                "source": {"kind": "tool", "callId": "call-1"},
                "content": [{"type": "tool-result", "toolCallId": "call-1", "content": [], "isError": True}],
            },
            "error": {"name": "ToolError", "code": "FAILED"},
        }],
        ["step/end", 12, {"turn": 1, "step": 1}],
        ["turn/end", 13, {"turn": 1, "reason": {"kind": "completed"}}],
        ["turn/start", 14, {"turn": 2}],
        ["step/start", 15, {"turn": 2, "step": 1}],
        ["request/header", 16, {"header": {"config": {"provider": "deepseek", "model": "v3"}}}],
        ["agent/error", 17, {"message": "model failed"}],
        ["turn/start", 18, {"turn": 3}],
        ["step/start", 19, {"turn": 3, "step": 1}],
        ["request/header", 20, {"header": {"config": {"provider": "deepseek", "model": "v3"}}}],
        ["turn/end", 21, {"turn": 3, "reason": {"kind": "interrupted"}}],
        ["turn/start", 22, {"turn": 4}],
        ["step/start", 23, {"turn": 4, "step": 1}],
        ["request/header", 24, {"header": {"config": {"provider": "deepseek", "model": "v3"}}}],
        ["shutdown", 25, {}, "ops"],
    ]
    script = """
      const { mapRecordsForTest } = await import(process.env.ASSET_URL)
      const rows = JSON.parse(process.env.RECORDS).map(([type, time, body, channel = 'ledger'], seq) => ({
        channel, time, severity: 'info',
        attributes: {
          'session.id': 'session-1', 'event.type': type, 'event.seq': seq,
          ...(channel === 'ops' ? {'telemetry.op': type} : {}),
        }, body,
      }))
      process.stdout.write(JSON.stringify(mapRecordsForTest(rows)))
    """
    env = os.environ.copy()
    env["TELOS_DSH_TELEMETRY_MODULE_URL"] = stub.resolve().as_uri()
    env["ASSET_URL"] = asset.resolve().as_uri()
    env["RECORDS"] = json.dumps(records)
    env["TELOS_ATTEMPT_ID"] = "attempt-deepseek"
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
    threads = [op["body"] for op in operations if op["entity"] == "thread"]
    assert all(item["attempt_id"] == "attempt-deepseek" for item in threads + traces)

    completed_llms = [span for span in spans if span["type"] == "llm" and span["status"] == "ok"]
    assert len({span["id"] for span in completed_llms}) == 2
    llm = completed_llms[0]
    tool = next(span for span in reversed(spans) if span["type"] == "tool")
    assert llm["model"] == "v3"
    assert llm["input_tokens"] == 10
    assert llm["reasoning_tokens"] == 2
    assert llm["ttft_us"] == 1000
    assert tool["status"] == "error"
    assert tool["parent_span_id"] == llm["parent_span_id"]
    assert {trace["status"] for trace in traces} >= {"ok", "error", "cancelled", "abandoned"}

    # The adapter speaks the store's public snapshot vocabulary, not SQLite's
    # private ``*_json`` column names.
    for operation in operations:
        body = operation["body"]
        body.pop("project_name", None)
        if operation["entity"] in ("thread", "trace"):
            body["project_id"] = "default"
    completed_trace = next(trace for trace in reversed(traces) if trace["status"] == "ok")
    with SQLiteTraceStore(tmp_path / "telos.db") as store:
        run = store.create_task_run(goal="adapter fixture")
        store.create_attempt(
            row_id="attempt-deepseek", task_run_id=run["id"],
            harness="deepseek-harness", status="running",
        )
        store.upsert_batch(operations)
        detail = store.get_trace(completed_trace["id"])
        assert detail is not None
        stored_llm = next(span for span in detail["spans"] if span["type"] == "llm")
        assert stored_llm["ttft_us"] == 1000
        assert stored_llm["usage"]["reasoningTokens"] == 2


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
async def test_adapter_posts_real_batch_and_network_failure_is_fail_open(tmp_path: Path) -> None:
    token = "deepseek-secret"
    token_path = tmp_path / "token"
    token_path.write_text(token, encoding="utf-8")
    token_path.chmod(0o600)
    stub = tmp_path / "telemetry-stub.mjs"
    stub.write_text(
        "export class SessionTelemetryBackend { constructor() {} }\n"
        "export class SessionTelemetryCoordinator { constructor() {} }\n",
        encoding="utf-8",
    )
    asset = Path(__file__).parents[1] / "init" / "assets" / "deepseek_harness_telemetry.mjs"
    app = make_app(
        upstream="http://127.0.0.1:1",
        record=False,
        tracing_db=tmp_path / "telos.db",
        trace_harnesses={
            "deepseek-harness": {
                "enabled": True,
                "tracing_token": token,
                "model_span_source": "adapter",
            }
        },
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    script = """
      const { TelosSessionTelemetryBackend } = await import(process.env.ASSET_URL)
      const backend = new TelosSessionTelemetryBackend(
        {logger: {warn() {}}},
        {endpoint: process.env.ENDPOINT, tokenFile: process.env.TOKEN_FILE,
         requestTimeoutMs: 100, retryDelayMs: 10, shutdownTimeoutMs: 300},
      )
      const events = [
        ['turn/start', {turn: 1}],
        ['user/message', {source: {kind: 'user'}, content: [{type: 'text', text: 'hi'}]}],
        ['step/start', {turn: 1, step: 1}],
        ['request/header', {header: {config: {provider: 'deepseek', model: 'v3'}}}],
        ['assistant/chunk', {turn: 1, step: 1, chunk: {type: 'text-delta', text: 'h'}}],
        ['assistant/message', {turn: 1, step: 1, message: {source: {kind: 'model', provider: 'deepseek', model: 'v3'}, content: []}, usage: {inputTokens: 2, outputTokens: 1}}],
        ['step/end', {turn: 1, step: 1}],
        ['turn/end', {turn: 1, reason: {kind: 'completed'}}],
      ]
      events.forEach(([type, body], seq) => backend.emit({
        channel: 'ledger', time: Date.now() + seq, severity: 'info',
        attributes: {'session.id': 'dsh-session', 'event.type': type, 'event.seq': seq}, body,
      }))
      await backend.shutdown()
    """

    async def run_backend(endpoint: str) -> tuple[int, str]:
        env = os.environ.copy()
        env.update({
            "TELOS_DSH_TELEMETRY_MODULE_URL": stub.resolve().as_uri(),
            "ASSET_URL": asset.resolve().as_uri(),
            "ENDPOINT": endpoint,
            "TOKEN_FILE": str(token_path),
        })
        process = await asyncio.create_subprocess_exec(
            "node", "--input-type=module", "--eval", script,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return process.returncode or 0, (stdout + stderr).decode()

    try:
        code, output = await run_backend(
            f"http://127.0.0.1:{port}/__telos/tracing/v1/batch"
        )
        assert code == 0, output
        async with ClientSession() as client:
            listed = await client.get(f"http://127.0.0.1:{port}/__telos/api/v1/traces")
            trace = (await listed.json())["items"][0]
            detail = await client.get(
                f"http://127.0.0.1:{port}/__telos/api/v1/traces/{trace['id']}"
            )
            spans = (await detail.json())["spans"]
            assert [span["type"] for span in spans].count("llm") == 1
            assert next(span for span in spans if span["type"] == "llm")["input_tokens"] == 2

        code, output = await run_backend("http://127.0.0.1:1/__telos/tracing/v1/batch")
        assert code == 0, output
    finally:
        await runner.cleanup()
