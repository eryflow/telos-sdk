"""Immutable Agent Profiles and the offline reference/candidate evaluation loop."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from telos.context_pack import _scan_secret, canonical_json, owned_directory, validate_context_pack
from telos.handoff import compatibility_report


CHANGE_DIMENSIONS = frozenset({
    "instructions", "context-selection", "compaction", "tool-policy", "harness-rendering",
})
_FILES = (
    "profile.json", "instructions.md", "context-policy.json", "tool-policy.json",
    "evaluation-policy.json",
)


def _digest(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path, data in sorted(files.items()):
        digest.update(path.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return "sha256:" + digest.hexdigest()


def validate_profile(path: str | Path) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    files: dict[str, bytes] = {}
    for name in _FILES:
        target = (root / name).resolve()
        if root not in target.parents or not target.is_file() or target.is_symlink():
            raise ValueError(f"profile file is missing or unsafe: {name}")
        files[name] = target.read_bytes()
        _scan_secret(name, files[name])
        if b"/.telos/packs/" in files[name]:
            raise ValueError(f"profile cannot reference a private Context Pack path: {name}")
    try:
        profile = json.loads(files["profile.json"])
        json.loads(files["context-policy.json"])
        json.loads(files["tool-policy.json"])
        json.loads(files["evaluation-policy.json"])
    except json.JSONDecodeError as exc:
        raise ValueError("profile contains invalid JSON") from exc
    if profile.get("schema_version") != 1 or profile.get("digest") != _digest({
        **files, "profile.json": canonical_json({**profile, "digest": ""}),
    }):
        raise ValueError("profile digest mismatch")
    if not files["instructions.md"].strip():
        raise ValueError("profile instructions are empty")
    return profile


def create_profile(
    store: Any, *, task_type: str, instructions: str,
    context_policy: Mapping[str, Any] | None = None,
    tool_policy: Mapping[str, Any] | None = None,
    evaluation_policy: Mapping[str, Any] | None = None,
    applicable_harnesses: list[str] | None = None,
    parent_revision_id: str | None = None, change_dimension: str | None = None,
    state: str = "draft", metadata: Mapping[str, Any] | None = None,
    home: str | Path | None = None,
) -> dict[str, Any]:
    if not instructions.strip():
        raise ValueError("instructions are required")
    if parent_revision_id and change_dimension not in CHANGE_DIMENSIONS:
        raise ValueError("a candidate must declare one supported change_dimension")
    if not parent_revision_id and change_dimension is not None:
        raise ValueError("the initial profile has no change_dimension")
    task = store.ensure_task_type(task_type, evolution_policy=evaluation_policy)
    profile_id = str(uuid4())
    profile_json = {
        "schema_version": 1,
        "task_type": task["name"],
        "parent_revision_id": parent_revision_id,
        "change_dimension": change_dimension,
        "applicable_harnesses": applicable_harnesses or ["codex", "kimi-code"],
        "digest": "",
    }
    files = {
        "profile.json": canonical_json(profile_json),
        "instructions.md": (instructions.rstrip() + "\n").encode(),
        "context-policy.json": canonical_json(context_policy or {}),
        "tool-policy.json": canonical_json(tool_policy or {}),
        "evaluation-policy.json": canonical_json(evaluation_policy or {}),
    }
    profile_json["digest"] = _digest(files)
    files["profile.json"] = canonical_json(profile_json)
    existing = store.find_profile_revision_by_digest(profile_json["digest"])
    if existing is not None:
        if state == "draft" and existing["state"] in {"draft", "evaluating", "recommended"}:
            return existing
        raise ValueError(
            f"identical Profile Revision already exists in state {existing['state']}: {existing['id']}"
        )
    base = owned_directory(home, "profiles")
    temporary = Path(tempfile.mkdtemp(prefix=".profile-", dir=base))
    final = base / profile_id
    try:
        for name, data in files.items():
            target = temporary / name
            target.write_bytes(data)
            os.chmod(target, 0o600)
        validate_profile(temporary)
        os.replace(temporary, final)
        return store.create_profile_revision(
            row_id=profile_id, task_type_id=task["id"], digest=profile_json["digest"],
            path=final, parent_revision_id=parent_revision_id, state=state,
            change_dimension=change_dimension, metadata=metadata,
        )
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        raise


def propose_candidate(
    store: Any, *, task_type: str, home: str | Path | None = None,
) -> dict[str, Any]:
    state = store.get_evolution(task_type)
    if state is None:
        raise ValueError(f"task type does not exist: {task_type}")
    production_id = state["task_type"]["production_profile_revision_id"]
    if production_id is None:
        raise ValueError("task type has no production profile")
    cases = state["regression_cases"]
    minimum = int(state["task_type"]["evolution_policy"].get("minimum_cases", 1))
    if len(cases) < minimum:
        raise ValueError(f"at least {minimum} frozen regression case(s) are required")
    production = store.get_profile_revision(production_id)
    parent_path = Path(production["path"])
    profile = validate_profile(parent_path)
    instructions = (parent_path / "instructions.md").read_text().rstrip()
    classifications = [case.get("classification") or "unknown" for case in cases]
    dominant = max(set(classifications), key=classifications.count)
    additions = {
        "context-loss": "Before acting, restate the objective, confirmed progress, and next verifiable step from the Context Pack.",
        "tool-error": "After a tool failure, inspect its concrete error, choose one focused recovery, and verify the result before continuing.",
        "regression": "Before completion, run the frozen acceptance checks relevant to the files changed and cite their results.",
    }
    addition = additions.get(
        dominant,
        "Before completion, verify the user's explicit acceptance criteria and preserve a traceable result.",
    )
    if addition not in instructions:
        instructions += f"\n\n- {addition}"
    return create_profile(
        store, task_type=task_type, instructions=instructions,
        context_policy=json.loads((parent_path / "context-policy.json").read_text()),
        tool_policy=json.loads((parent_path / "tool-policy.json").read_text()),
        evaluation_policy=json.loads((parent_path / "evaluation-policy.json").read_text()),
        applicable_harnesses=profile["applicable_harnesses"],
        parent_revision_id=production_id, change_dimension="instructions",
        metadata={"optimizer": "deterministic-failure-rule-v1", "classification": dominant},
        home=home,
    )


def freeze_case(
    store: Any, *, task_type: str, pack_id: str, outcome_resolution_id: str,
    protected: bool = False, policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    state = store.get_evolution(task_type)
    if state is None:
        raise ValueError(f"task type does not exist: {task_type}")
    pack = store.get_context_pack(pack_id)
    if pack is None:
        raise ValueError(f"Context Pack does not exist: {pack_id}")
    digest = "sha256:" + hashlib.sha256(canonical_json({
        "task_type_id": state["task_type"]["id"],
        "pack_digest": pack["digest"],
        "outcome_resolution_id": outcome_resolution_id,
        "protected": protected,
        "policy": policy or {},
    })).hexdigest()
    return store.freeze_regression_case(
        task_type_id=state["task_type"]["id"], pack_id=pack_id,
        outcome_resolution_id=outcome_resolution_id, digest=digest,
        protected=protected, policy=policy,
    )


def command_case_runner(
    case: Mapping[str, Any], revision: Mapping[str, Any], harness: str, variant: str,
) -> dict[str, Any]:
    policy = case["policy"]
    command = policy.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError("regression case policy requires an explicit command array")
    timeout = int(policy.get("timeout_seconds", 300))
    if not 1 <= timeout <= 3600:
        raise ValueError("timeout_seconds must be between 1 and 3600")
    public_policy = {key: value for key, value in policy.items() if key not in {"private_gold", "command"}}
    payload = {
        "case_id": case["id"], "pack_id": case["pack_id"],
        "attempt_id": case.get("_attempt_id"),
        "profile_revision_id": revision["id"], "profile_path": revision["path"],
        "harness": harness, "variant": variant, "policy": public_policy,
    }
    started = time.perf_counter_ns()
    result = subprocess.run(
        command, input=json.dumps(payload), text=True, capture_output=True,
        cwd=policy.get("cwd"), timeout=timeout, check=False,
        env={**os.environ, "TELOS_EVALUATION_VARIANT": variant,
             "TELOS_PROFILE_REVISION_ID": revision["id"], "TELOS_HARNESS": harness,
             "TELOS_ATTEMPT_ID": str(case.get("_attempt_id") or "")},
    )
    latency_us = (time.perf_counter_ns() - started) // 1_000
    if result.returncode:
        return {
            "outcome": "error", "score": 0.0, "cost_usd_micros": 0,
            "latency_us": latency_us,
            "evidence": {"stderr": result.stderr[-4000:], "trace_integrity": False},
        }
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("evaluation command must print one JSON object") from exc
    if not isinstance(output, dict):
        raise ValueError("evaluation command must print one JSON object")
    output.setdefault("latency_us", latency_us)
    output.setdefault("cost_usd_micros", 0)
    output.setdefault("evidence", {})
    return output


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    return sorted(values)[max(0, (95 * len(values) + 99) // 100 - 1)]


def evaluate_candidate(
    store: Any, *, task_type: str, candidate_revision_id: str,
    runner: Callable[[Mapping[str, Any], Mapping[str, Any], str, str], Mapping[str, Any]] = command_case_runner,
) -> dict[str, Any]:
    state = store.get_evolution(task_type)
    if state is None:
        raise ValueError(f"task type does not exist: {task_type}")
    task = state["task_type"]
    reference_id = task["production_profile_revision_id"]
    if reference_id is None:
        raise ValueError("task type has no production profile")
    cases = state["regression_cases"]
    minimum = int(task["evolution_policy"].get("minimum_cases", 1))
    if len(cases) < minimum:
        raise ValueError(f"at least {minimum} frozen regression case(s) are required")
    revisions = {
        "reference": store.get_profile_revision(reference_id),
        "candidate": store.get_profile_revision(candidate_revision_id),
    }
    run = store.create_evaluation_run(
        task_type_id=task["id"], reference_revision_id=reference_id,
        candidate_revision_id=candidate_revision_id,
    )
    results: list[dict[str, Any]] = []
    try:
        for case in cases:
            harnesses = case["policy"].get("required_harnesses") or ["codex", "kimi-code"]
            if len(set(harnesses)) < 2:
                raise ValueError("each regression case must require at least two Harnesses")
            pack = store.get_context_pack(case["pack_id"])
            validate_context_pack(pack["path"])
            for harness in harnesses:
                report = compatibility_report(pack["path"], harness)
                for variant, revision in revisions.items():
                    attempt = store.create_attempt(
                        task_run_id=pack["task_run_id"], harness=harness,
                        context_pack_id=pack["id"], profile_revision_id=revision["id"],
                        status="running", launch_plan={"evaluation_run_id": run["id"], "variant": variant},
                    )
                    try:
                        raw = dict(runner({**case, "_attempt_id": attempt["id"]}, revision, harness, variant))
                        outcome = str(raw.get("outcome") or "error")
                        score = float(raw.get("score", 0.0))
                        store.set_attempt_status(attempt["id"], "ok" if outcome != "error" else "error")
                    except Exception as exc:
                        outcome, score = "error", 0.0
                        raw = {"evidence": {"error": str(exc), "trace_integrity": False}}
                        store.set_attempt_status(attempt["id"], "error")
                    evidence = dict(raw.get("evidence") or {})
                    evidence["compatibility"] = report["overall"]
                    evidence["trace_integrity"] = store.check_attempt_trace_integrity(attempt["id"])
                    result = store.add_evaluation_result(
                        evaluation_run_id=run["id"], regression_case_id=case["id"],
                        harness=harness, variant=variant, attempt_id=attempt["id"],
                        outcome=outcome if outcome in {"pass", "fail", "error"} else "error",
                        score=score, cost_usd_micros=int(raw.get("cost_usd_micros", 0)),
                        latency_us=int(raw.get("latency_us", 0)), evidence=evidence,
                    )
                    results.append({**result, "protected": bool(case["protected"])})
        policy = task["evolution_policy"]
        reference = [item for item in results if item["variant"] == "reference"]
        candidate = [item for item in results if item["variant"] == "candidate"]
        paired = {
            (item["regression_case_id"], item["harness"]): item for item in reference
        }
        validity = len(results) == sum(
            2 * len(set(case["policy"].get("required_harnesses") or ["codex", "kimi-code"]))
            for case in cases
        ) and all(item["outcome"] != "error" for item in results)
        critical = not any(
            item["protected"] and paired[(item["regression_case_id"], item["harness"])]["outcome"] == "pass"
            and item["outcome"] != "pass" for item in candidate
        )
        ref_score = sum(item["score"] for item in reference) / len(reference)
        cand_score = sum(item["score"] for item in candidate) / len(candidate)
        quality = cand_score - ref_score >= float(policy.get("minimum_improvement", 0.0))
        portability = all(item["evidence"].get("compatibility") != "blocked" for item in results)
        ref_cost, cand_cost = sum(item["cost_usd_micros"] for item in reference), sum(item["cost_usd_micros"] for item in candidate)
        cost = cand_cost <= ref_cost * float(policy.get("max_cost_ratio", 1.2)) if ref_cost else cand_cost == 0
        ref_p95 = _p95([item["latency_us"] for item in reference])
        cand_p95 = _p95([item["latency_us"] for item in candidate])
        latency = cand_p95 <= ref_p95 * float(policy.get("max_latency_ratio", 1.2)) if ref_p95 else cand_p95 == 0
        integrity = all(
            item["evidence"].get("trace_integrity", {}).get("passed") is True
            for item in results
        )
        gates = {
            "validity": {"passed": validity, "results": len(results)},
            "critical_regression": {"passed": critical},
            "outcome_quality": {"passed": quality, "reference": ref_score, "candidate": cand_score},
            "portability": {"passed": portability},
            "cost": {"passed": cost, "reference": ref_cost, "candidate": cand_cost},
            "latency": {"passed": latency, "reference_p95_us": ref_p95, "candidate_p95_us": cand_p95},
            "trace_integrity": {"passed": integrity},
        }
        passed = all(gate["passed"] for gate in gates.values())
        return store.finish_evaluation(run["id"], passed=passed, gates=gates)
    except Exception as exc:
        return store.finish_evaluation(
            run["id"], passed=False, error=True,
            gates={"internal_error": {"passed": False, "detail": str(exc)}},
        )
