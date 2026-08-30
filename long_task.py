"""Explicit long-horizon Task CLI.

``telos run`` remains the compatibility command for one execution.  This
module is the only CLI path that creates the durable Task above executions.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from telos.config import telos_home
from telos.harnesses import HARNESS_NAMES
from telos.tracing import SQLiteTraceStore


def _json_file(path: str | None, *, default: Any) -> Any:
    if path is None:
        return default
    try:
        return json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON file: {path}") from exc


def _task_record(detail: dict[str, Any]) -> dict[str, Any]:
    return detail.get("task", detail)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telos task", description="Explicitly create and advance a long-horizon Task.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("--name", required=True)
    goal = create.add_mutually_exclusive_group(required=True)
    goal.add_argument("--goal")
    goal.add_argument("--goal-file")
    create.add_argument("--contract-file")
    create.add_argument("--workspace", default=str(Path.cwd()))
    create.add_argument("--no-self-evolve", action="store_true")

    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("task_id")

    execute = sub.add_parser("execute")
    execute.add_argument("task_id")
    execute.add_argument("--harness", choices=HARNESS_NAMES, required=True)
    execute.add_argument("--no-exec", action="store_true")

    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("task_id")
    checkpoint.add_argument("--state-file", required=True)
    checkpoint.add_argument("--execution-id")
    checkpoint.add_argument("--evidence", action="append", default=[])
    checkpoint.add_argument("--audit-file")

    evolve = sub.add_parser("evolve")
    evolve.add_argument("task_id")
    evolve.add_argument("--execution-id", required=True)
    evolve.add_argument("--changes-file", required=True)

    outcome = sub.add_parser("outcome")
    outcome.add_argument("execution_id")
    outcome.add_argument("--outcome", choices=("pass", "fail"), required=True)
    outcome.add_argument("--evidence", action="append", required=True)

    promote_skill = sub.add_parser("promote-skill")
    promote_skill.add_argument("skill_id")

    promote_agent = sub.add_parser("promote-agent")
    promote_agent.add_argument("revision_id")
    promote_agent.add_argument("--evidence", action="append", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    home = telos_home()
    try:
        with SQLiteTraceStore(home / "telos.db") as store:
            if args.command == "create":
                goal = args.goal if args.goal is not None else Path(args.goal_file).read_text()
                workspace = Path(args.workspace).expanduser().resolve()
                if not workspace.is_dir():
                    raise ValueError(f"workspace is not a directory: {workspace}")
                contract = _json_file(args.contract_file, default={})
                if not isinstance(contract, dict):
                    raise ValueError("contract file must contain one JSON object")
                task = store.create_task(
                    name=args.name, goal=goal,
                    contract=contract,
                    workspace={"root": str(workspace)},
                    self_evolve=not args.no_self_evolve,
                )
                print(f"Task {task['id']}  {task['status']}\n  {task['name']}: {task['goal']}")
                return 0

            if args.command == "list":
                for task in store.list_tasks():
                    print(f"{task['id']}  {task['status']:<12} {task['name']}  {task['goal']}")
                return 0

            if args.command == "show":
                detail = store.get_task(args.task_id)
                if detail is None:
                    raise ValueError(f"Task does not exist: {args.task_id}")
                task = _task_record(detail)
                print(f"Task {task['id']}  {task['status']}\n  {task['name']}: {task['goal']}")
                state = detail.get("current_state")
                if state:
                    print(f"  State r{state['revision']}  {state['status']}")
                print(f"  Executions {len(detail.get('executions', []))}")
                print(f"  Knowledge  {len(detail.get('knowledge', []))}")
                print(f"  Skills     {len(detail.get('skills', []))}")
                return 0

            if args.command == "checkpoint":
                revision = store.create_task_state_revision(
                    args.task_id,
                    state=_json_file(args.state_file, default={}),
                    evidence_refs=args.evidence,
                    task_execution_id=args.execution_id,
                    audit=_json_file(args.audit_file, default=None),
                )
                print(f"Task {args.task_id} state r{revision['revision']} → {revision['status']}")
                return 0

            if args.command == "evolve":
                from telos.task_evolution import evolve_task_execution
                changes = _json_file(args.changes_file, default={})
                if not isinstance(changes, dict):
                    raise ValueError("changes file must contain one JSON object")
                result = evolve_task_execution(
                    store, task_id=args.task_id, execution_id=args.execution_id,
                    knowledge_changes=changes.get("knowledge") or [],
                    skill_candidates=changes.get("skills") or [],
                    agent_candidate=changes.get("agent"),
                )
                phases = result["phases"]
                print(
                    f"Task {args.task_id} evolved: knowledge={len(phases['knowledge'])} "
                    f"skills={len(phases['skills'])} agent={int(phases['agent'] is not None)}"
                )
                return 0

            if args.command == "outcome":
                result = store.set_task_execution_status(
                    args.execution_id,
                    "completed" if args.outcome == "pass" else "failed",
                    outcome=args.outcome, evidence_refs=args.evidence, trusted=True,
                )
                print(f"TaskExecution {result['id']} → trusted {result['outcome']}")
                return 0

            if args.command == "promote-skill":
                result = store.promote_task_skill(args.skill_id)
                print(f"Skill {result['id']} → {result['state']}")
                return 0

            if args.command == "promote-agent":
                result = store.promote_task_agent_revision(
                    args.revision_id, evidence_refs=args.evidence,
                )
                print(f"Agent {result['id']} → {result['state']}")
                return 0

            detail = store.get_task(args.task_id)
            if detail is None:
                raise ValueError(f"Task does not exist: {args.task_id}")
            task = _task_record(detail)
            run = store.create_task_run(
                goal=task["goal"], workspace=task.get("workspace") or {}, task_id=task["id"],
            )
            execution = store.create_task_execution(
                task["id"], harness=args.harness, task_run_id=run["id"],
            )
            attempt = store.create_attempt(
                task_run_id=run["id"], task_execution_id=execution["id"], harness=args.harness,
                launch_plan={"task_execution_id": execution["id"]},
            )
            print(f"TaskExecution {execution['id']}\nAttempt {attempt['id']} ({attempt['harness']})")
            if args.no_exec:
                print(f"telos run launch {attempt['id']}")
                return 0
            attempt_id = attempt["id"]
        from telos.task_run import main as run_main
        return run_main(["launch", attempt_id])
    except (OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
