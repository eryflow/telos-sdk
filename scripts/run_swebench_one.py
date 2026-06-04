#!/usr/bin/env python3
"""End-to-end Scope-B runner: telos (Hermes mini_swe_runner) → TELOS → OpenRouter
→ patch → optional evaluate.

Runs a single SWE-bench Verified task, demonstrating that telos-as-harness works
through the telos pipeline; outputs a ``<results-dir>/telos-<instance_id>.patch``
compatible with ``token-efficient-framework/benchmark/scripts/evaluate-patches.py``,
plus telos-side ``usage.jsonl`` / ``result.json``.

Usage::

    export OPENROUTER_API_KEY=sk-or-...
    python -m telos.scripts.run_swebench_one \\
        --instance pallets__flask-5014 \\
        --model deepseek/deepseek-chat \\
        --max-iterations 25 \\
        --results-dir /tmp/telos-telos-run

Assumes the telos repo has already run ``git submodule update --init`` per its
README, i.e. vendor/hermes is in place.

Note: ``mini_swe_runner``'s LocalEnvironment execs a shell directly in ``cwd``;
this script first creates a git worktree under ``/tmp/telos-swebench/<inst>`` and
then passes that directory to the runner. When the run finishes, ``git diff HEAD``
extracts the patch.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Path constants (change as needed)
# ---------------------------------------------------------------------------

TELOS_ROOT = Path("/Users/george/Code/telos-sdk")
HERMES_ROOT = TELOS_ROOT / "vendor" / "hermes"
TEF_ROOT = Path("/Users/george/Code/token-efficient-framework")
DEFAULT_DATASET = TEF_ROOT / "benchmark" / "datasets" / "swe-bench-verified.jsonl"
DEFAULT_RESULTS = Path("/tmp/telos-telos-runs")
WORK_ROOT = Path("/tmp/telos-swebench")
REPO_CACHE = Path("/tmp/swebench-repos")


# ---------------------------------------------------------------------------
# Helper: dataset loading (download on demand)
# ---------------------------------------------------------------------------

def ensure_dataset(dataset_path: Path) -> Path:
    if dataset_path.exists():
        return dataset_path
    print(f"[setup] dataset missing at {dataset_path}; downloading from HF...",
          flush=True)
    try:
        from datasets import load_dataset
    except ImportError:
        sys.exit(
            "datasets package not installed. Run: pip install datasets\n"
            f"Or pre-place the file at {dataset_path}.")
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
    ds.to_json(str(dataset_path))
    return dataset_path


def load_instance(dataset_path: Path, instance_id: str) -> dict[str, Any]:
    with dataset_path.open() as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("instance_id") == instance_id:
                return d
    raise SystemExit(f"instance_id {instance_id!r} not found in {dataset_path}")


# ---------------------------------------------------------------------------
# Git worktree setup
# ---------------------------------------------------------------------------

def _run(cmd: list[str], *, cwd: str | None = None, timeout: int = 120,
         check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, cwd=cwd, check=False, capture_output=capture,
                       text=True, timeout=timeout)
    if check and r.returncode != 0:
        sys.stderr.write(f"$ {' '.join(cmd)}\n{r.stderr}\n")
        raise SystemExit(r.returncode)
    return r


def ensure_repo_mirror(repo: str) -> Path:
    safe = repo.replace("/", "__")
    dst = REPO_CACHE / f"{safe}.git"
    if dst.exists():
        return dst
    REPO_CACHE.mkdir(parents=True, exist_ok=True)
    print(f"[setup] cloning mirror github.com/{repo} → {dst}", flush=True)
    _run(["git", "clone", "--mirror", f"https://github.com/{repo}.git", str(dst)],
         timeout=900, capture=False)
    return dst


def setup_worktree(mirror: Path, base_commit: str, work: Path) -> None:
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "-C", str(mirror), "worktree", "prune"], check=False)
    _run(["git", "-C", str(mirror), "worktree", "add", "-f", "--detach",
          str(work), base_commit], timeout=180)


def teardown_worktree(mirror: Path, work: Path) -> None:
    _run(["git", "-C", str(mirror), "worktree", "remove", "--force", str(work)],
         check=False, timeout=30)
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)


def diff_of(work: Path) -> str:
    _run(["git", "-C", str(work), "add", "-N", "."], check=False, timeout=30)
    r = _run(["git", "-C", str(work), "diff", "HEAD"], check=False, timeout=60)
    return r.stdout


# ---------------------------------------------------------------------------
# Core: run one task
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default="pallets__flask-5014",
                    help="SWE-bench Verified instance_id")
    ap.add_argument("--model", default="deepseek/deepseek-chat",
                    help="OpenRouter model id (default: deepseek/deepseek-chat)")
    ap.add_argument("--dataset", default=str(DEFAULT_DATASET))
    ap.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    ap.add_argument("--max-iterations", type=int, default=25)
    ap.add_argument("--command-timeout", type=int, default=60)
    ap.add_argument("--evaluate", action="store_true",
                    help="run evaluate-patches.py after producing the patch")
    ap.add_argument("--keep-worktree", action="store_true",
                    help="don't tear down the worktree on exit (debug)")
    ap.add_argument("--no-telos", action="store_true",
                    help="bypass TELOS — use a plain OpenAI client (A/B baseline)")
    args = ap.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY"):
        sys.exit("OPENROUTER_API_KEY not set in environment.")

    # vendored Hermes must be on sys.path to import mini_swe_runner
    if not HERMES_ROOT.exists():
        sys.exit(f"vendor/hermes not found at {HERMES_ROOT}; "
                 "run `git submodule update --init` inside telos.")
    sys.path.insert(0, str(HERMES_ROOT))

    dataset_path = ensure_dataset(Path(args.dataset))
    inst = load_instance(dataset_path, args.instance)
    repo = inst["repo"]
    base = inst["base_commit"]
    problem = inst["problem_statement"]

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    tag_prefix = "vanilla" if args.no_telos else "telos"
    tag = f"{tag_prefix}-{args.instance}"
    session_id = str(uuid.uuid4())

    # ---- worktree ----
    mirror = ensure_repo_mirror(repo)
    work = WORK_ROOT / tag
    setup_worktree(mirror, base, work)
    print(f"[setup] worktree ready: {work}", flush=True)

    # ---- import telos's vendored runner & patch its client ----
    from mini_swe_runner import MiniSWERunner  # type: ignore[import-not-found]

    from telos.scripts.telos_transport import (
        TelosOpenAITransport,
        VanillaOpenAITransport,
    )

    usage_log = results_dir / f"{tag}.usage.jsonl"
    trace_log = results_dir / f"{tag}.prompt_trace.jsonl"
    runner = MiniSWERunner(
        model=args.model,
        env_type="local",
        cwd=str(work),
        max_iterations=args.max_iterations,
        command_timeout=args.command_timeout,
        verbose=False,
    )
    if args.no_telos:
        runner.client = VanillaOpenAITransport(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API_KEY"],
            session_id=session_id,
            usage_log=str(usage_log),
            engine_name="deepseek",
            harness_name="vanilla",
        )
    else:
        runner.client = TelosOpenAITransport(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API_KEY"],
            session_id=session_id,
            usage_log=str(usage_log),
            prompt_trace_log=str(trace_log),
            engine_name="deepseek",
            harness_name="telos",
        )

    prompt = (
        f"You are working in {work}.\n"
        f"Repository: {repo} (commit {base[:8]}).\n"
        f"Issue:\n{problem}\n\n"
        f"Fix the source code in this repo to resolve the issue. "
        f"Do NOT modify any test files. "
        f"When done, run `git diff HEAD` then echo MINI_SWE_AGENT_FINAL_OUTPUT "
        f"followed by a one-line summary of what you changed."
    )

    t0 = time.time()
    err: str | None = None
    traj: dict[str, Any] | None = None
    try:
        traj = runner.run_task(prompt)
    except Exception as e:  # noqa: BLE001 — we want to record any failure
        err = f"{type(e).__name__}: {e}"
        sys.stderr.write(f"[run] task raised: {err}\n")
    duration = int(time.time() - t0)

    # ---- collect the patch ----
    patch = diff_of(work)
    (results_dir / f"{tag}.patch").write_text(patch)

    # ---- write trajectory + result.json ----
    if traj is not None:
        (results_dir / f"{tag}.trajectory.json").write_text(
            json.dumps(traj, ensure_ascii=False, indent=2))

    summary = {
        "tag": tag,
        "instance_id": args.instance,
        "repo": repo,
        "base_commit": base,
        "model": args.model,
        "session_id": session_id,
        "harness": "vanilla" if args.no_telos else "telos",
        "engine": "deepseek",
        "duration_s": duration,
        "patch_bytes": len(patch),
        "non_empty_patch": bool(patch.strip()),
        "completed": bool(traj and traj.get("completed")),
        "api_calls": (traj or {}).get("api_calls"),
        "error": err,
        "started_at": datetime.utcfromtimestamp(t0).isoformat() + "Z",
        "usage_log": str(usage_log),
        "prompt_trace_log": str(trace_log),
    }
    (results_dir / f"{tag}.result.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2))

    print(f"\n[done] {tag} duration={duration}s patch_bytes={len(patch)} "
          f"completed={summary['completed']} api_calls={summary['api_calls']}",
          flush=True)
    print(f"[done] artifacts under {results_dir}", flush=True)

    # ---- optional: run evaluate ----
    eval_result: dict[str, Any] | None = None
    if args.evaluate:
        evaluator = TEF_ROOT / "benchmark" / "scripts" / "evaluate-patches.py"
        if not evaluator.exists():
            print(f"[eval] skip: {evaluator} not found", flush=True)
        else:
            print(f"[eval] running {evaluator.name}...", flush=True)
            r = subprocess.run(
                [sys.executable, str(evaluator),
                 "--results-dir", str(results_dir),
                 "--dataset", str(dataset_path),
                 "--filter-agent", "vanilla" if args.no_telos else "telos",
                 "--max-parallel", "1"],
                check=False, capture_output=True, text=True, timeout=1800,
            )
            sys.stdout.write(r.stdout)
            sys.stderr.write(r.stderr)
            ev_path = results_dir / f"{tag}.eval.json"
            if ev_path.exists():
                eval_result = json.loads(ev_path.read_text())
                print(f"[eval] resolved={eval_result.get('resolved')} "
                      f"reason={eval_result.get('reason')}", flush=True)

    if not args.keep_worktree:
        teardown_worktree(mirror, work)


if __name__ == "__main__":
    main()
