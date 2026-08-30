"""Capability negotiation and temporary launch plans for Harness handoff."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
from typing import Any, Mapping
from uuid import uuid4

from telos.context_pack import owned_directory, telos_home, validate_context_pack
from telos.harnesses import executable_path, get_spec


CAPABILITIES: dict[str, dict[str, Any]] = {
    "codex": {
        "instruction_injection": "startup-prompt",
        "conversation_import": "summary-only",
        "workspace_selection": "cwd",
        "tool_visibility": "full",
        "attachments": "paths",
        "lifecycle_hooks": ["session", "turn", "tool", "model"],
        "usage_capture": "gateway",
        "context_budget": "unknown",
    },
    "kimi-code": {
        "instruction_injection": "agent-file",
        "conversation_import": "summary-only",
        "workspace_selection": "cwd",
        "tool_visibility": "full",
        "attachments": "paths",
        "lifecycle_hooks": ["session", "turn", "tool", "model"],
        "usage_capture": "adapter+gateway",
        "context_budget": "unknown",
    },
    "deepseek-harness": {
        "instruction_injection": "startup-prompt",
        "conversation_import": "summary-only",
        "workspace_selection": "cwd",
        "tool_visibility": "full",
        "attachments": "paths",
        "lifecycle_hooks": ["session", "turn", "tool", "model"],
        "usage_capture": "adapter+gateway",
        "context_budget": "unknown",
    },
}


def _read_entries(root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for entry in manifest["entries"]:
        path = entry["path"]
        if path.endswith(".json"):
            result[path] = json.loads((root / path).read_text())
    return result


def compatibility_report(
    pack_path: str | Path, destination: str, *, workspace: str | Path | None = None,
) -> dict[str, Any]:
    if destination not in CAPABILITIES:
        raise ValueError(f"no handoff renderer for {destination!r}")
    root = Path(pack_path).expanduser().resolve()
    manifest = validate_context_pack(root)
    entries = _read_entries(root, manifest)
    capabilities = CAPABILITIES[destination]
    layers: dict[str, dict[str, str]] = {}
    for layer, state in manifest["layers"].items():
        if state == "omitted":
            layers[layer] = {"level": "native", "detail": "not included by the source pack"}
        elif layer == "conversation":
            layers[layer] = {
                "level": "degraded",
                "detail": "normalized conversation is rendered as a bounded summary",
            }
        else:
            layers[layer] = {"level": "native", "detail": f"rendered through {capabilities['instruction_injection']}"}
    required_workspace = manifest.get("requirements", {}).get("workspace")
    state = entries.get("workspace/state.json") or {}
    selected_workspace = Path(workspace or state.get("root") or ".").expanduser().resolve()
    if required_workspace in {"read", "read-only", "read-write"} and not state:
        layers["workspace"] = {
            "level": "blocked", "detail": "the pack requires a workspace but has no workspace snapshot",
        }
    elif required_workspace and not selected_workspace.is_dir():
        layers["workspace"] = {
            "level": "blocked", "detail": f"workspace does not exist: {selected_workspace}",
        }
    required_tools = set(manifest.get("requirements", {}).get("tools") or [])
    tools = {
        "level": "native" if not required_tools or capabilities["tool_visibility"] == "full" else "blocked",
        "detail": "all required tool names remain visible" if required_tools else "no required tools declared",
    }
    budget = {
        "level": "degraded" if manifest.get("requirements", {}).get("minimum_context_tokens") else "native",
        "detail": (
            "destination context budget is not discoverable; startup context is bounded"
            if manifest.get("requirements", {}).get("minimum_context_tokens")
            else "no minimum context budget declared"
        ),
    }
    levels = [item["level"] for item in layers.values()] + [tools["level"], budget["level"]]
    overall = "blocked" if "blocked" in levels else "degraded" if "degraded" in levels else "native"
    warnings = []
    if manifest["capture_status"] in {"partial", "dirty"}:
        warnings.append(f"source pack capture status is {manifest['capture_status']}")
    return {
        "destination": destination,
        "overall": overall,
        "layers": layers,
        "tools": tools,
        "context_budget": budget,
        "capabilities": capabilities,
        "workspace": str(selected_workspace),
        "warnings": warnings,
    }


def render_handoff_context(pack_path: str | Path, report: Mapping[str, Any]) -> str:
    root = Path(pack_path).expanduser().resolve()
    manifest = validate_context_pack(root)
    entries = _read_entries(root, manifest)

    def section(title: str, value: Any) -> str:
        return f"## {title}\n\n```json\n{json.dumps(value, ensure_ascii=False, indent=2)}\n```"

    parts = [
        "# TELOS Context Handoff",
        "Continue the same TaskRun. Treat confirmed facts as evidence, and re-check assumptions or pending actions before executing them.",
        section("User objective and constraints", entries["objective.json"]),
        section("Current progress and next step", entries["progress.json"]),
        section("Confirmed facts, decisions, and assumptions", entries["memory.json"]),
    ]
    if "workspace/state.json" in entries:
        parts.append(section("Workspace state", entries["workspace/state.json"]))
        if (root / "workspace/changes.patch").exists():
            parts.append(
                "Tracked workspace changes are already present in the selected workspace. "
                "A frozen copy is available at `workspace/changes.patch` inside the Context Pack."
            )
    conversation = entries.get("conversation.json")
    if conversation is not None:
        encoded = json.dumps(conversation, ensure_ascii=False, indent=2)
        parts.append("## Necessary conversation summary\n\n" + encoded[-12000:])
    degraded = [
        f"- {name}: {item['detail']}" for name, item in report["layers"].items()
        if item["level"] != "native"
    ]
    degraded.extend(f"- warning: {warning}" for warning in report.get("warnings", []))
    if degraded:
        parts.append("## Capability warnings\n\n" + "\n".join(degraded))
    parts.extend((
        section("Active behavior policy", entries["policy.json"]),
        "## Evidence references\n\n"
        f"- TaskRun: `{manifest['task_run_id']}`\n"
        f"- Source Attempt: `{manifest.get('source_attempt_id') or 'none'}`\n"
        f"- Context Pack: `{manifest['pack_id']}` (`{manifest['digest']}`)\n"
        "- Raw Trace evidence remains in the local TELOS Evidence page.",
        "## First action\n\nBefore changing files, briefly restate the objective, completed work, key decision, and next step. Then continue the task.",
    ))
    return "\n\n".join(parts) + "\n"


def create_launch_plan(
    pack_path: str | Path, destination: str, *, attempt_id: str,
    workspace: str | Path | None = None, home: str | Path | None = None,
    executables: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    report = compatibility_report(pack_path, destination, workspace=workspace)
    if report["overall"] == "blocked":
        raise ValueError("handoff is blocked by destination compatibility")
    spec = get_spec(destination)
    executable = executable_path(spec, dict(executables or {}))
    if executable is None:
        raise ValueError(f"destination executable is not installed: {destination}")
    run_dir = owned_directory(home, "runs") / attempt_id
    run_dir.mkdir(exist_ok=False, mode=0o700)
    os.chmod(run_dir, 0o700)
    context_path = run_dir / "HANDOFF.md"
    context_path.write_text(render_handoff_context(pack_path, report))
    os.chmod(context_path, 0o600)
    selected_workspace = report["workspace"]
    if destination == "codex":
        command = [
            executable, "-C", selected_workspace,
            f"Read {context_path} and continue this TELOS handoff now.",
        ]
    elif destination == "kimi-code":
        agent_path = run_dir / "agent.md"
        agent_path.write_text(
            "---\nname: telos-handoff\ndescription: Continue an immutable TELOS Context Pack\n---\n\n"
            f"Read `{context_path}` completely. Follow it as the current task context. "
            "Before editing, restate the objective, progress, key decision, and next step; then continue.\n"
        )
        os.chmod(agent_path, 0o600)
        command = [executable, "--agent-file", str(agent_path)]
    else:
        command = [
            executable,
            f"Read {context_path} and continue this TELOS handoff now.",
        ]
    plan = {
        "attempt_id": attempt_id,
        "destination": destination,
        "command": command,
        "command_display": (
            f"cd {shlex.quote(selected_workspace)} && "
            f"TELOS_ATTEMPT_ID={shlex.quote(attempt_id)} {shlex.join(command)}"
        ),
        "cwd": selected_workspace,
        "environment": {"TELOS_ATTEMPT_ID": attempt_id},
        "context_file": str(context_path),
        "compatibility": report,
    }
    plan_path = run_dir / "launch.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    os.chmod(plan_path, 0o600)
    return plan


def prepare_handoff(
    store: Any, *, pack_id: str, destination: str, workspace: str | Path | None = None,
    reason: str | None = None, home: str | Path | None = None,
    executables: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    pack = store.get_context_pack(pack_id)
    if pack is None:
        raise ValueError(f"Context Pack does not exist: {pack_id}")
    report = compatibility_report(pack["path"], destination, workspace=workspace)
    if report["overall"] == "blocked":
        handoff = store.create_handoff(
            task_run_id=pack["task_run_id"], context_pack_id=pack_id,
            destination_harness=destination, compatibility=report,
            source_attempt_id=pack.get("source_attempt_id"), reason=reason, status="blocked",
        )
        raise ValueError(f"handoff is blocked; report id: {handoff['id']}")
    attempt_id = str(uuid4())
    plan = create_launch_plan(
        pack["path"], destination, attempt_id=attempt_id, workspace=workspace,
        home=home, executables=executables,
    )
    source_attempt = store.get_attempt(pack.get("source_attempt_id"))
    attempt = store.create_attempt(
        row_id=attempt_id, task_run_id=pack["task_run_id"], harness=destination,
        task_execution_id=(source_attempt or {}).get("task_execution_id"),
        source_attempt_id=pack.get("source_attempt_id"), context_pack_id=pack_id,
        profile_revision_id=pack.get("profile_revision_id"), launch_plan=plan,
    )
    handoff = store.create_handoff(
        task_run_id=pack["task_run_id"], context_pack_id=pack_id,
        destination_harness=destination, compatibility=report,
        source_attempt_id=pack.get("source_attempt_id"),
        destination_attempt_id=attempt_id, reason=reason,
    )
    return {**plan, "handoff_id": handoff["id"]}, attempt


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="telos handoff", description="Continue one TaskRun in another Harness.")
    parser.add_argument("destination")
    parser.add_argument("--pack")
    parser.add_argument("--attempt")
    parser.add_argument("--workspace", default=str(Path.cwd()))
    parser.add_argument("--exclude-workspace-path", action="append", default=[])
    parser.add_argument("--reason")
    parser.add_argument("--plan", action="store_true", help="show compatibility without creating an Attempt")
    parser.add_argument("--no-exec", action="store_true", help="create the Launch Plan without replacing this process")
    args = parser.parse_args(argv)
    aliases = {"kimi": "kimi-code", "deepseek": "deepseek-harness"}
    destination = aliases.get(args.destination, args.destination)
    if destination not in CAPABILITIES:
        parser.error(f"unsupported destination: {destination}")

    from telos.config import load_config
    from telos.context_pack import create_context_pack
    from telos.tracing import SQLiteTraceStore

    home = telos_home(os.environ.get("TELOS_HOME"))
    with SQLiteTraceStore(home / "telos.db") as store:
        pack_id = args.pack
        if pack_id is None:
            attempt_id = args.attempt or os.environ.get("TELOS_ATTEMPT_ID")
            if not attempt_id:
                parser.error("--pack, --attempt, or TELOS_ATTEMPT_ID is required")
            attempt = store.get_attempt(attempt_id)
            if attempt is None:
                parser.error(f"attempt does not exist: {attempt_id}")
            run = store.get_task_run(attempt["task_run_id"])["task_run"]
            evidence = store.get_attempt_evidence(attempt_id)
            manifest, path = create_context_pack(
                home=home, task_run_id=run["id"], source_attempt_id=attempt_id,
                profile_revision_id=attempt.get("profile_revision_id"),
                objective={"goal": run["goal"], "constraints": []},
                policy={"profile_revision_id": attempt.get("profile_revision_id")},
                progress={
                    "done": [f"Observed {evidence['completed_turns']} completed Trace turn(s)"],
                    "in_progress": [], "next": [], "blocked": [],
                    "last_observed_result": evidence["last_output"],
                },
                memory={"facts": [], "decisions": [], "assumptions": []},
                provenance={"harness": attempt["harness"], "attempt_id": attempt_id,
                            "trace_ids": evidence["trace_ids"],
                            "capture_source": "telos handoff CLI"},
                conversation=evidence["conversation"] or None,
                workspace=args.workspace, requirements={
                    "workspace": "read-write", "tools": ["shell", "file-edit"],
                }, capture_status="partial", capture_method="reconstructed",
                workspace_exclude=args.exclude_workspace_path,
            )
            store.register_context_pack(manifest, path)
            pack_id = manifest["pack_id"]
        pack = store.get_context_pack(pack_id)
        if pack is None:
            parser.error(f"Context Pack does not exist: {pack_id}")
        report = compatibility_report(pack["path"], destination, workspace=args.workspace)
        print(f"{destination}: {report['overall']}")
        for layer, result in report["layers"].items():
            print(f"  {layer:<12} {result['level']:<8} {result['detail']}")
        if args.plan:
            return 1 if report["overall"] == "blocked" else 0
        cfg = load_config()
        try:
            plan, attempt = prepare_handoff(
                store, pack_id=pack_id, destination=destination,
                workspace=args.workspace, reason=args.reason, home=home,
                executables=cfg.harness_executables,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(f"created Attempt {attempt['id']} in TaskRun {attempt['task_run_id']}")
        print(f"Launch Plan: {Path(plan['context_file']).parent / 'launch.json'}")
        if args.no_exec:
            print("command: " + plan["command_display"])
            return 0
        from telos.gateway import daemon
        from telos.harnesses import gateway_env
        state = daemon.read_state()
        base_url = state.base_url() if state is not None else cfg.gateway.base_url()
        child_env = {**os.environ, **gateway_env(get_spec(destination), base_url), **plan["environment"]}
        try:
            os.chdir(plan["cwd"])
            os.execvpe(plan["command"][0], plan["command"], child_env)
        except OSError as exc:
            store.set_attempt_status(attempt["id"], "error")
            print(f"failed to launch {destination}: {exc}")
            return 1
    return 0
