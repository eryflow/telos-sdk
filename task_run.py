"""Create and inspect the TaskRun identity above Harness sessions."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from telos.config import telos_home
from telos.evolution import create_profile
from telos.harnesses import HARNESS_NAMES
from telos.tracing import SQLiteTraceStore


def ensure_task_profile(store: SQLiteTraceStore, name: str, home: Path) -> dict:
    task = store.ensure_task_type(name)
    if store.get_evolution(task["id"])["task_type"]["production_profile_revision_id"] is None:
        create_profile(
            store, task_type=name, state="production", home=home,
            instructions=(
                "Follow the user's explicit constraints. Preserve existing user changes. "
                "Verify concrete acceptance criteria before declaring completion."
            ),
            evaluation_policy={
                "minimum_cases": 1, "minimum_improvement": 0.0,
                "max_cost_ratio": 1.2, "max_latency_ratio": 1.2,
            },
        )
    return task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="telos run", description="Own one goal across Harness Attempts.")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--task", required=True)
    start.add_argument("--goal", required=True)
    start.add_argument("--harness", choices=HARNESS_NAMES, required=True)
    start.add_argument("--workspace", default=str(Path.cwd()))
    start.add_argument("--no-exec", action="store_true")
    launch = sub.add_parser("launch")
    launch.add_argument("attempt_id")
    show = sub.add_parser("show")
    show.add_argument("run_id")
    sub.add_parser("list")
    finish = sub.add_parser("finish")
    finish.add_argument("run_id")
    finish.add_argument("--status", choices=("ok", "error", "cancelled", "abandoned"), default="ok")
    args = parser.parse_args(argv)
    home = telos_home()
    with SQLiteTraceStore(home / "telos.db") as store:
        if args.command == "list":
            for run in store.list_task_runs():
                print(f"{run['id']}  {run['status']:<9} {run.get('task_type_name') or 'unclassified'}  {run['goal']}")
            return 0
        if args.command == "show":
            detail = store.get_task_run(args.run_id)
            if detail is None:
                parser.error(f"TaskRun does not exist: {args.run_id}")
            run = detail["task_run"]
            print(f"TaskRun {run['id']}  {run['status']}\n  {run['goal']}")
            for attempt in detail["attempts"]:
                print(f"  Attempt {attempt['id']}  {attempt['harness']}  {attempt['status']}")
            for pack in detail["packs"]:
                print(f"  Pack    {pack['id']}  {pack['capture_status']}  {pack['digest']}")
            return 0
        if args.command == "finish":
            run = store.set_task_run_status(args.run_id, args.status)
            print(f"TaskRun {run['id']} → {run['status']}")
            return 0
        if args.command == "launch":
            attempt = store.get_attempt(args.attempt_id)
            if attempt is None:
                parser.error(f"Attempt does not exist: {args.attempt_id}")
            run = store.get_task_run(attempt["task_run_id"])["task_run"]
            workspace = Path(run.get("workspace", {}).get("root") or Path.cwd()).resolve()
            if not workspace.is_dir():
                parser.error(f"Workspace is not a directory: {workspace}")
            store.set_attempt_status(attempt["id"], "running")
            os.chdir(workspace)
            os.environ["TELOS_ATTEMPT_ID"] = attempt["id"]
            os.environ["TELOS_TASK_PLUGINS"] = json.dumps(
                attempt.get("launch_plan", {}).get("plugins", [])
            )
            from telos.cli import _cmd_launch_harness
            arguments = {
                "codex": ["exec", "--approve-for-me", "--skip-git-repo-check", run["goal"]],
                "kimi-code": ["--auto", "--prompt", run["goal"]],
                "deepseek-harness": ["--profile", "headless", run["goal"]],
            }.get(attempt["harness"], [])
            result = _cmd_launch_harness(attempt["harness"], arguments, replace=False)
            status = "error" if result else "ok"
            store.set_attempt_status(attempt["id"], status)
            store.set_task_run_status(run["id"], status)
            return result
        task = ensure_task_profile(store, args.task, home)
        run = store.create_task_run(
            goal=args.goal, task_type_id=task["id"], workspace={"root": str(Path(args.workspace).resolve())},
        )
        attempt = store.create_attempt(task_run_id=run["id"], harness=args.harness)
        print(f"TaskRun {run['id']}")
        print(f"Attempt {attempt['id']} ({attempt['harness']})")
        if args.no_exec:
            print(f"export TELOS_ATTEMPT_ID={attempt['id']}")
            return 0
        os.environ["TELOS_ATTEMPT_ID"] = attempt["id"]
        from telos.cli import _cmd_launch_harness
        result = _cmd_launch_harness(args.harness)
        if result:
            store.set_attempt_status(attempt["id"], "error")
        return result


if __name__ == "__main__":
    raise SystemExit(main())
