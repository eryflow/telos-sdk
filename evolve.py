"""TaskType-level self-evolution policy CLI."""

from __future__ import annotations

import argparse

from telos.config import (
    disable_evolution_task,
    enable_evolution_task,
    load_config,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="telos evolve",
        description="Configure local offline self-evolution for a task type.",
    )
    parser.add_argument("--task", default=None, metavar="TYPE",
                        help="task type, for example '代码缺陷修复'")
    parser.add_argument("--disable", action="store_true",
                        help="disable the task type without deleting its evidence")
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
            state = "disabled" if changed else "already disabled"
            print(f"self-evolve {state} for task type {args.task.strip()!r}; evidence retained")
        else:
            _, changed = enable_evolution_task(args.task)
            state = "enabled" if changed else "already enabled"
            print(f"self-evolve policy {state} for task type {args.task.strip()!r}")
            print("  evaluation policy: offline")
            print("  production promotion: manual")
            print("  evaluator worker: not shipped yet")
        return 0
    except ValueError as e:
        parser.error(str(e))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
