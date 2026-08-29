#!/usr/bin/env python3
"""Real Kimi Code acceptance: Context Pack -> handoff -> work -> linked evidence."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any
from urllib.request import urlopen
from uuid import uuid4


def executable(value: str, fallback: Path | None = None) -> str:
    found = shutil.which(value)
    if found:
        return str(Path(found).resolve())
    if fallback and fallback.is_file():
        return str(fallback.resolve())
    raise RuntimeError(f"executable not found: {value}")


def run(
    command: list[str], *, env: dict[str, str], cwd: Path | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
        )
    return result


def output_id(text: str, label: str) -> str:
    match = re.search(rf"(?m)^{re.escape(label)} ([^\s]+)", text)
    if not match:
        raise RuntimeError(f"missing {label!r} in command output:\n{text}")
    return match.group(1)


def get_json(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=5) as response:  # noqa: S310 - loopback URL created below
        return json.load(response)


def free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def wait_for_gateway(base_url: str, timeout: int = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            get_json(base_url + "/__telos/api/v1/task-runs")
            return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"gateway did not become ready: {base_url}")


def wait_for_kimi_evidence(
    base_url: str, run_id: str, attempt_id: str, timeout: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = get_json(f"{base_url}/__telos/api/v1/task-runs/{run_id}")
        trace_ids = [
            trace["id"] for trace in last.get("traces", [])
            if trace.get("attempt_id") == attempt_id
        ]
        details = [
            get_json(f"{base_url}/__telos/api/v1/traces/{trace_id}")
            for trace_id in trace_ids
        ]
        if any(
            detail["trace"]["status"] == "ok"
            and any(span["type"] == "tool" and span["status"] == "ok" for span in detail["spans"])
            for detail in details
        ):
            return last, details
        time.sleep(0.5)
    raise RuntimeError(f"no completed Kimi tool trace after {timeout}s; last TaskRun={last}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an isolated, paid Kimi Code E2E against a temporary TELOS gateway.",
    )
    parser.add_argument("--telos-bin", default=os.environ.get("TELOS_BIN", "telos"))
    parser.add_argument("--kimi-bin", default=os.environ.get("KIMI_BIN", "kimi"))
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--strict-llm", action="store_true",
        help="also require an authoritative gateway LLM span (needs a routed API-key provider)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the temporary workspace")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    telos = executable(args.telos_bin)
    kimi = executable(args.kimi_bin, Path.home() / ".kimi-code/bin/kimi")
    root = Path(tempfile.mkdtemp(prefix="telos-kimi-e2e-"))
    workspace, home = root / "workspace", root / "telos-home"
    workspace.mkdir()
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    path = str(Path(telos).parent) + os.pathsep + os.environ.get("PATH", "")
    env = {
        **os.environ,
        "PATH": path,
        "TELOS_HOME": str(home),
        "TELOS_GATEWAY_URL": base_url,
    }
    gateway_started = False
    succeeded = False
    markers = {
        "goal": "TELOS_KIMI_GOAL_" + uuid4().hex,
        "done": "TELOS_KIMI_DONE_" + uuid4().hex,
        "decision": "TELOS_KIMI_DECISION_" + uuid4().hex,
        "next": "TELOS_KIMI_NEXT_" + uuid4().hex,
        "file": "TELOS_KIMI_FILE_" + uuid4().hex,
    }
    try:
        run(["git", "init", "-q", str(workspace)], env=env)
        run(["git", "-C", str(workspace), "config", "user.email", "kimi-e2e@example.invalid"], env=env)
        run(["git", "-C", str(workspace), "config", "user.name", "TELOS Kimi E2E"], env=env)
        (workspace / "TASK.md").write_text("# Kimi handoff E2E\n\nThe destination must continue this workspace.\n")
        run(["git", "-C", str(workspace), "add", "TASK.md"], env=env)
        run(["git", "-C", str(workspace), "commit", "-qm", "initial fixture"], env=env)

        # Registration happens before startup so the isolated gateway loads the Kimi ingest token.
        run([
            telos, "init", "--harness", "kimi-code", "--gateway-url", base_url,
            "--no-gateway",
        ], env=env)
        run([telos, "gateway", "start", "--host", "127.0.0.1", "--port", str(port)], env=env)
        gateway_started = True
        wait_for_gateway(base_url)

        started = run([
            telos, "run", "start", "--task", "kimi-code-e2e",
            "--goal", markers["goal"], "--harness", "codex",
            "--workspace", str(workspace), "--no-exec",
        ], env=env)
        run_id = output_id(started.stdout, "TaskRun")
        source_attempt_id = output_id(started.stdout, "Attempt")

        packed = run([
            telos, "pack", "--attempt", source_attempt_id,
            "--workspace", str(workspace), "--capture-method", "native",
            "--done", markers["done"], "--decision", markers["decision"],
            "--next", markers["next"],
        ], env=env, cwd=workspace)
        pack_id = output_id(packed.stdout, "created Context Pack")

        handed_off = run([
            telos, "handoff", "kimi-code", "--pack", pack_id,
            "--workspace", str(workspace), "--no-exec",
        ], env=env, cwd=workspace)
        destination_attempt_id = output_id(handed_off.stdout, "created Attempt")
        plan = json.loads((home / "runs" / destination_attempt_id / "launch.json").read_text())
        prompt = (
            "Continue the TELOS handoff. Read its context, then use the Shell tool to run exactly "
            f"`printf '%s\\n' '{markers['file']}' > kimi-e2e-result.txt && pwd`. "
            "Finally report the objective, completed item, decision, and next step from the handoff "
            "as GOAL=..., DONE=..., DECISION=..., NEXT=..., followed by FILE=<file marker>."
        )
        kimi_result = run([
            *plan["command"], "--add-dir", str(home),
            "--prompt", prompt, "--output-format", "text",
        ], env={**env, **plan["environment"]}, cwd=workspace, timeout=args.timeout)

        result_file = workspace / "kimi-e2e-result.txt"
        if not result_file.is_file() or result_file.read_text().strip() != markers["file"]:
            raise RuntimeError("Kimi did not create the expected workspace artifact")
        for name in ("goal", "done", "decision", "next", "file"):
            if markers[name] not in kimi_result.stdout:
                raise RuntimeError(f"Kimi output did not recover {name}: {markers[name]}")

        run_detail, trace_details = wait_for_kimi_evidence(
            base_url, run_id, destination_attempt_id, args.timeout,
        )
        destination = next(
            item for item in run_detail["attempts"] if item["id"] == destination_attempt_id
        )
        handoff = next(
            item for item in run_detail["handoffs"]
            if item["destination_attempt_id"] == destination_attempt_id
        )
        if handoff["context_pack_id"] != pack_id or destination["context_pack_id"] != pack_id:
            raise RuntimeError("Handoff/Attempt lost the Context Pack identity")
        if destination["source_attempt_id"] != source_attempt_id:
            raise RuntimeError("destination Attempt lost its source lineage")

        matching = [
            detail for detail in trace_details
            if detail["trace"].get("attempt_id") == destination_attempt_id
            and detail["trace"].get("context_pack_id") == pack_id
            and detail.get("task_run", {}).get("id") == run_id
        ]
        if not matching:
            raise RuntimeError("no Trace preserved TaskRun/Attempt/Context Pack identity")
        spans = [span for detail in matching for span in detail["spans"]]
        llm_spans = [span for span in spans if span["type"] == "llm"]
        if args.strict_llm and not llm_spans:
            raise RuntimeError(
                "strict LLM evidence failed: Kimi managed OAuth is hooks-only; "
                "select an API-key provider routed through TELOS"
            )

        run([telos, "run", "finish", run_id], env=env)
        print("PASS: Kimi Code Context Pack handoff E2E")
        print(f"  TaskRun:     {run_id}")
        print(f"  source:      codex/{source_attempt_id}")
        print(f"  destination: kimi-code/{destination_attempt_id}")
        print(f"  Context Pack: {pack_id}")
        print(f"  evidence:    {len(matching)} Trace(s), {len(spans)} Span(s), {len(llm_spans)} LLM Span(s)")
        print(f"  workspace:   {result_file} = {markers['file']}")
        if not llm_spans:
            print("  note:        lifecycle/tool evidence passed; managed OAuth exposes no gateway LLM span")
        print(f"  pages:       {base_url}/__telos/  |  {base_url}/__telos/traces")
        succeeded = True
        return 0
    finally:
        if gateway_started:
            subprocess.run([telos, "gateway", "stop"], env=env, capture_output=True, text=True)
        if succeeded and not args.keep:
            shutil.rmtree(root)
        else:
            print(f"  retained:    {root}")


if __name__ == "__main__":
    raise SystemExit(main())
