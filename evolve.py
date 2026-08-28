"""TaskType evolution policy and offline evaluation CLI."""

from __future__ import annotations

import argparse
from pathlib import Path

from telos.config import disable_evolution_task, enable_evolution_task, load_config, telos_home
from telos.evolution import create_profile, evaluate_candidate, freeze_case, propose_candidate
from telos.tracing import SQLiteTraceStore


def _store() -> SQLiteTraceStore:
    return SQLiteTraceStore(telos_home() / "telos.db")


def _status(task: str) -> int:
    with _store() as store:
        state = store.get_evolution(task)
    if state is None:
        print(f"No evolution state for task type {task!r}.")
        return 1
    current = state["task_type"]["production_profile_revision_id"] or "none"
    print(f"task type: {state['task_type']['name']}")
    print(f"production profile: {current}")
    print(f"frozen cases: {len(state['regression_cases'])}")
    print(f"profile revisions: {len(state['revisions'])}")
    if state["evaluations"]:
        latest = state["evaluations"][0]
        print(f"latest evaluation: {latest['id']} ({latest['status']})")
        for name, gate in latest["gates"].items():
            print(f"  {name:<20} {'pass' if gate.get('passed') else 'fail'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
    command = argv[0] if argv and argv[0] in {
        "status", "bootstrap", "run", "promote", "rollback", "outcome", "freeze", "export",
    } else None
    if command is None:
        parser = argparse.ArgumentParser(
            prog="telos evolve", description="Configure local offline self-evolution for a task type.",
        )
        parser.add_argument("--task", default=None, metavar="TYPE")
        parser.add_argument("--disable", action="store_true")
        args = parser.parse_args(argv)
        if args.task is None:
            policies = load_config().evolution_tasks
            if not policies:
                print("No task type has self-evolve configured.")
                return 0
            print("self-evolve task types:")
            for name, policy in policies.items():
                state = "enabled" if policy.get("enabled") is True else "disabled"
                print(f"  {name}: {state} (evaluation=offline, promotion=manual)")
            return 0
        try:
            if args.disable:
                _, changed = disable_evolution_task(args.task)
                print(f"self-evolve {'disabled' if changed else 'already disabled'} for task type {args.task.strip()!r}; evidence retained")
            else:
                _, changed = enable_evolution_task(args.task)
                with _store() as store:
                    store.ensure_task_type(args.task, {
                        "minimum_cases": 1, "minimum_improvement": 0.0,
                        "max_cost_ratio": 1.2, "max_latency_ratio": 1.2,
                    })
                print(f"self-evolve policy {'enabled' if changed else 'already enabled'} for task type {args.task.strip()!r}")
                print("  evaluation policy: offline")
                print("  production promotion: manual")
                print("  evaluator: available via `telos evolve run --task ...`")
            return 0
        except ValueError as exc:
            parser.error(str(exc))

    if command == "status":
        parser = argparse.ArgumentParser(prog="telos evolve status")
        parser.add_argument("--task", required=True)
        args = parser.parse_args(argv[1:])
        return _status(args.task)

    if command == "bootstrap":
        parser = argparse.ArgumentParser(prog="telos evolve bootstrap")
        parser.add_argument("--task", required=True)
        parser.add_argument("--instructions", required=True)
        args = parser.parse_args(argv[1:])
        with _store() as store:
            revision = create_profile(
                store, task_type=args.task, instructions=args.instructions,
                evaluation_policy={"minimum_cases": 1, "minimum_improvement": 0.0,
                                   "max_cost_ratio": 1.2, "max_latency_ratio": 1.2},
                state="production", home=telos_home(),
            )
        print(f"created production Profile Revision {revision['id']} ({revision['digest']})")
        return 0

    if command == "run":
        parser = argparse.ArgumentParser(prog="telos evolve run")
        parser.add_argument("--task", required=True)
        parser.add_argument("--candidate")
        args = parser.parse_args(argv[1:])
        with _store() as store:
            candidate = (
                store.get_profile_revision(args.candidate) if args.candidate
                else propose_candidate(store, task_type=args.task, home=telos_home())
            )
            if candidate is None:
                parser.error(f"candidate does not exist: {args.candidate}")
            result = evaluate_candidate(
                store, task_type=args.task, candidate_revision_id=candidate["id"],
            )
        print(f"candidate: {candidate['id']}")
        print(f"evaluation: {result['id']} ({result['status']})")
        for name, gate in result["gates"].items():
            print(f"  {name:<20} {'pass' if gate.get('passed') else 'fail'}")
        return 0 if result["status"] == "passed" else 1

    if command == "promote":
        parser = argparse.ArgumentParser(prog="telos evolve promote")
        parser.add_argument("revision_id")
        args = parser.parse_args(argv[1:])
        with _store() as store:
            result = store.promote_profile(args.revision_id)
        print(f"promoted {result['to_revision_id']} (previous {result['from_revision_id'] or 'none'})")
        return 0

    if command == "rollback":
        parser = argparse.ArgumentParser(prog="telos evolve rollback")
        parser.add_argument("--task", required=True)
        args = parser.parse_args(argv[1:])
        with _store() as store:
            state = store.get_evolution(args.task)
            if state is None:
                parser.error(f"task type does not exist: {args.task}")
            result = store.rollback_task_type(state["task_type"]["id"])
        print(f"rolled back {result['from_revision_id']} → {result['to_revision_id']}")
        return 0

    if command == "outcome":
        parser = argparse.ArgumentParser(prog="telos evolve outcome")
        parser.add_argument("--run", required=True)
        parser.add_argument("--attempt")
        parser.add_argument("--outcome", choices=("pass", "fail", "unknown"), required=True)
        parser.add_argument("--classification")
        parser.add_argument("--score", type=float)
        args = parser.parse_args(argv[1:])
        with _store() as store:
            result = store.resolve_outcome(
                task_run_id=args.run, attempt_id=args.attempt, outcome=args.outcome,
                classification=args.classification, score=args.score,
            )
        print(f"Outcome Resolution {result['id']} ({result['outcome']})")
        return 0

    if command == "export":
        parser = argparse.ArgumentParser(prog="telos evolve export")
        parser.add_argument("--task")
        parser.add_argument("--output", required=True)
        args = parser.parse_args(argv[1:])
        from telos.training_export import export_training_data
        with _store() as store:
            result = export_training_data(store, args.output, task_type=args.task)
        for name, count in result["counts"].items():
            print(f"{name:<10} {count:>5} → {result['paths'][name]}")
        return 0

    parser = argparse.ArgumentParser(prog="telos evolve freeze")
    parser.add_argument("--task", required=True)
    parser.add_argument("--pack", required=True)
    parser.add_argument("--outcome-id", required=True)
    parser.add_argument("--protected", action="store_true")
    parser.add_argument("--harness", action="append", dest="harnesses", default=[])
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv[1:])
    policy = {
        "required_harnesses": args.harnesses or ["codex", "kimi-code"],
        "timeout_seconds": args.timeout,
    }
    if args.command:
        policy["command"] = args.command
    with _store() as store:
        case = freeze_case(
            store, task_type=args.task, pack_id=args.pack,
            outcome_resolution_id=args.outcome_id, protected=args.protected,
            policy=policy,
        )
    print(f"frozen RegressionCase {case['id']} ({case['digest']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
