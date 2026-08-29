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
from typing import Any, Callable, Mapping, Sequence
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
_PRIVATE_POLICY_KEYS = frozenset({"private_gold", "private_rubric"})
_HIDDEN_OPTIMIZER_KEYS = frozenset({
    "command", "runner_command", "evaluator_command", "optimizer_command",
    "cwd", "payload_path", "payload_digest", "private_payload_path",
    "workspace_fixture_path",
})
_PUBLIC_GATE_KEYS = frozenset({
    "passed", "results", "trials", "runs_per_case", "reference", "candidate",
    "improvement", "reference_p95_us", "candidate_p95_us",
})
_MAX_OPTIMIZATION_ROUNDS = 20
_MAX_RUNS_PER_CASE = 20


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
    task = store.ensure_task_type(
        task_type, evolution_policy=evaluation_policy if parent_revision_id is None else None,
    )
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


def _public_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in policy.items()
        if key not in _HIDDEN_OPTIMIZER_KEYS
        and key not in _PRIVATE_POLICY_KEYS
        and not key.startswith("private_")
        and not key.endswith("_path")
    }


def optimizer_evidence_view(
    state: Mapping[str, Any], reference: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the only evidence view an external Optimizer may receive."""
    root = Path(reference["path"])
    return {
        "protocol_version": 1,
        "task_type": state["task_type"]["name"],
        "reference": {
            "id": reference["id"], "digest": reference["digest"],
            "instructions": (root / "instructions.md").read_text(),
            "context_policy": json.loads((root / "context-policy.json").read_text()),
            "tool_policy": json.loads((root / "tool-policy.json").read_text()),
        },
        "cases": [
            {
                "id": case["id"], "digest": case["digest"],
                "protected": bool(case["protected"]), "outcome": case.get("outcome"),
                "classification": case.get("classification"), "score": case.get("score"),
                "policy": _public_policy(case.get("policy") or {}),
            }
            for case in state["regression_cases"]
        ],
        "prior_evaluations": [
            {
                "id": evaluation["id"], "status": evaluation["status"],
                "reference_revision_id": evaluation["reference_revision_id"],
                "candidate_revision_id": evaluation["candidate_revision_id"],
                "gates": {
                    name: {key: value for key, value in gate.items() if key in _PUBLIC_GATE_KEYS}
                    for name, gate in (evaluation.get("gates") or {}).items()
                    if isinstance(gate, dict)
                },
            }
            for evaluation in state["evaluations"]
        ],
        "latest_results": [
            {
                key: result.get(key) for key in (
                    "regression_case_id", "harness", "variant", "outcome", "score",
                    "cost_usd_micros", "latency_us", "attempt_id",
                )
            }
            for result in state.get("latest_results", [])
        ],
    }


def _minimal_environment(**values: str) -> dict[str, str]:
    environment = {
        key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR")
        if os.environ.get(key)
    }
    environment.update(values)
    return environment


def _run_json_command(
    command: Sequence[str], payload: Mapping[str, Any], *, timeout: int,
    cwd: str | Path | None = None, environment: Mapping[str, str] | None = None,
    label: str,
) -> dict[str, Any]:
    if isinstance(command, (str, bytes)) or not command or not all(
        isinstance(item, str) and item for item in command
    ):
        raise ValueError(f"{label} command must be a non-empty string array")
    result = subprocess.run(
        list(command), input=json.dumps(payload), text=True, capture_output=True,
        cwd=cwd, timeout=timeout, check=False,
        env={**_minimal_environment(), **dict(environment or {})},
    )
    if result.returncode:
        raise ValueError(f"{label} command failed: {result.stderr[-4000:]}")
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} command must print one JSON object") from exc
    if not isinstance(output, dict):
        raise ValueError(f"{label} command must print one JSON object")
    return output


def propose_candidate(
    store: Any, *, task_type: str, home: str | Path | None = None,
    reference_revision_id: str | None = None,
    optimizer_command: Sequence[str] | None = None,
) -> dict[str, Any]:
    state = store.get_evolution(task_type)
    if state is None:
        raise ValueError(f"task type does not exist: {task_type}")
    reference_id = reference_revision_id or state["task_type"]["production_profile_revision_id"]
    if reference_id is None:
        raise ValueError("task type has no production profile")
    cases = state["regression_cases"]
    minimum = int(state["task_type"]["evolution_policy"].get("minimum_cases", 1))
    if len(cases) < minimum:
        raise ValueError(f"at least {minimum} frozen regression case(s) are required")
    reference = store.get_profile_revision(reference_id)
    if reference is None or reference["task_type_id"] != state["task_type"]["id"]:
        raise ValueError("reference Profile Revision does not belong to the task type")
    parent_path = Path(reference["path"])
    profile = validate_profile(parent_path)
    instructions = (parent_path / "instructions.md").read_text().rstrip()
    configured_command = (
        optimizer_command or state["task_type"]["evolution_policy"].get("optimizer_command") or []
    )
    if isinstance(configured_command, (str, bytes)):
        raise ValueError("optimizer command must be a string array")
    command = list(configured_command)
    if command:
        proposal = _run_json_command(
            command, optimizer_evidence_view(state, reference),
            timeout=int(state["task_type"]["evolution_policy"].get("optimizer_timeout_seconds", 300)),
            cwd=state["task_type"]["evolution_policy"].get("optimizer_cwd"),
            label="optimizer",
        )
        if proposal.get("change_dimension") != "instructions":
            raise ValueError("the initial Optimizer protocol supports only instructions Candidates")
        instructions = str(proposal.get("instructions") or "").strip()
        hypothesis = proposal.get("hypothesis")
        if not instructions or not isinstance(hypothesis, dict) or not hypothesis.get("prediction"):
            raise ValueError("optimizer output requires instructions and hypothesis.prediction")
        metadata = {
            "optimizer": "external-evidence-protocol-v1",
            "hypothesis": hypothesis,
            "reference_revision_id": reference_id,
        }
    else:
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
        metadata = {
            "optimizer": "deterministic-failure-rule-v2", "classification": dominant,
            "hypothesis": {
                "prediction": f"reduce failures classified as {dominant}",
                "observable": "higher frozen-case score without protected regressions",
            },
            "reference_revision_id": reference_id,
        }
    return create_profile(
        store, task_type=task_type, instructions=instructions,
        context_policy=json.loads((parent_path / "context-policy.json").read_text()),
        tool_policy=json.loads((parent_path / "tool-policy.json").read_text()),
        evaluation_policy=json.loads((parent_path / "evaluation-policy.json").read_text()),
        applicable_harnesses=profile["applicable_harnesses"],
        parent_revision_id=reference_id, change_dimension="instructions",
        metadata=metadata,
        home=home,
    )


def _case_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    for target in sorted(root.rglob("*")):
        if target.is_symlink():
            raise ValueError(f"RegressionCase symlink is not allowed: {target}")
        if not target.is_file() or target.name == "manifest.json":
            continue
        relative = target.relative_to(root).as_posix()
        data = target.read_bytes()
        total += len(data)
        if len(data) > 50 * 1024 * 1024 or total > 200 * 1024 * 1024:
            raise ValueError("RegressionCase payload exceeds size limits")
        _scan_secret(relative, data)
        files[relative] = data
    return files


def _copy_fixture(source: str | Path, destination: Path) -> None:
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"workspace fixture does not exist: {source}")
    ignored = {".git", ".telos", "node_modules", "__pycache__", ".pytest_cache"}
    destination.mkdir(mode=0o700)
    for target in sorted(source.rglob("*")):
        relative = target.relative_to(source)
        if any(part in ignored for part in relative.parts):
            continue
        if target.is_symlink():
            raise ValueError(f"workspace fixture symlink is not allowed: {relative}")
        copied = destination / relative
        if target.is_dir():
            copied.mkdir(parents=True, exist_ok=True, mode=0o700)
        elif target.is_file():
            copied.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            data = target.read_bytes()
            _scan_secret(f"fixture/{relative.as_posix()}", data)
            copied.write_bytes(data)
            os.chmod(copied, 0o600)


def validate_frozen_case(case: Mapping[str, Any]) -> dict[str, Any]:
    policy = case.get("policy") or {}
    root = Path(str(policy.get("payload_path") or "")).expanduser().resolve()
    try:
        manifest = json.loads((root / "manifest.json").read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid or missing RegressionCase payload") from exc
    files = _case_files(root)
    entries = manifest.get("entries")
    expected = {
        item["path"]: {"sha256": item["sha256"], "bytes": item["bytes"]}
        for item in entries if isinstance(item, dict) and "path" in item
    } if isinstance(entries, list) else {}
    actual = {
        path: {"sha256": "sha256:" + hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        for path, data in files.items()
    }
    if manifest.get("case_id") != case.get("id") or actual != expected:
        raise ValueError("RegressionCase payload checksum mismatch")
    semantic = {**manifest, "digest": ""}
    digest = "sha256:" + hashlib.sha256(canonical_json(semantic)).hexdigest()
    if manifest.get("digest") != digest or policy.get("payload_digest") != digest:
        raise ValueError("RegressionCase payload digest mismatch")
    if json.loads((root / "public.json").read_text()) != _public_policy(policy):
        raise ValueError("RegressionCase public policy changed after freeze")
    runtime = {
        key: policy[key] for key in ("command", "runner_command", "evaluator_command", "cwd")
        if key in policy
    }
    if json.loads((root / "runtime.json").read_text()) != runtime:
        raise ValueError("RegressionCase runtime policy changed after freeze")
    return manifest


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
    case_id = str(uuid4())
    raw_policy = dict(policy or {})
    harnesses = raw_policy.get("required_harnesses") or ["codex", "kimi-code"]
    if (
        not isinstance(harnesses, list)
        or not all(isinstance(item, str) and item.strip() for item in harnesses)
        or len(set(harnesses)) < 2
    ):
        raise ValueError("required_harnesses must contain at least two distinct Harness names")
    raw_policy["required_harnesses"] = harnesses
    for key in ("command", "runner_command", "evaluator_command"):
        command = raw_policy.get(key)
        if command is not None and (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
        ):
            raise ValueError(f"{key} must be a non-empty string array")
    private = {
        key: raw_policy.pop(key) for key in tuple(raw_policy)
        if key in _PRIVATE_POLICY_KEYS or key.startswith("private_")
    }
    fixture = raw_policy.pop("workspace_fixture", None)
    base = owned_directory(store.path.parent, "regression-cases")
    temporary = Path(tempfile.mkdtemp(prefix=".case-", dir=base))
    final = base / case_id
    try:
        (temporary / "public.json").write_bytes(canonical_json(_public_policy(raw_policy)))
        runtime = {
            key: raw_policy[key]
            for key in ("command", "runner_command", "evaluator_command", "cwd")
            if key in raw_policy
        }
        (temporary / "private.json").write_bytes(canonical_json(private))
        (temporary / "runtime.json").write_bytes(canonical_json(runtime))
        os.chmod(temporary / "public.json", 0o600)
        os.chmod(temporary / "private.json", 0o600)
        os.chmod(temporary / "runtime.json", 0o600)
        if fixture is not None:
            _copy_fixture(fixture, temporary / "fixture")
        files = _case_files(temporary)
        manifest = {
            "schema_version": 1, "case_id": case_id, "digest": "",
            "entries": [
                {"path": path, "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                 "bytes": len(data)}
                for path, data in sorted(files.items())
            ],
        }
        manifest["digest"] = "sha256:" + hashlib.sha256(canonical_json({
            **manifest, "digest": "",
        })).hexdigest()
        (temporary / "manifest.json").write_bytes(canonical_json(manifest))
        os.chmod(temporary / "manifest.json", 0o600)
        os.replace(temporary, final)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    stored_policy = {
        **raw_policy,
        "payload_path": str(final), "payload_digest": manifest["digest"],
        "workspace_fixture_path": str(final / "fixture") if fixture is not None else None,
    }
    digest = "sha256:" + hashlib.sha256(canonical_json({
        "task_type_id": state["task_type"]["id"],
        "pack_digest": pack["digest"],
        "outcome_resolution_id": outcome_resolution_id,
        "protected": protected,
        "payload_digest": manifest["digest"],
    })).hexdigest()
    try:
        case = store.freeze_regression_case(
            row_id=case_id, task_type_id=state["task_type"]["id"], pack_id=pack_id,
            outcome_resolution_id=outcome_resolution_id, digest=digest,
            protected=protected, policy=stored_policy,
        )
        validate_frozen_case(case)
        return case
    except Exception:
        shutil.rmtree(final, ignore_errors=True)
        raise


def command_case_runner(
    case: Mapping[str, Any], revision: Mapping[str, Any], harness: str, variant: str,
) -> dict[str, Any]:
    policy = case["policy"]
    command = policy.get("runner_command") or policy.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError("regression case policy requires an explicit command array")
    timeout = int(policy.get("timeout_seconds", 300))
    if not 1 <= timeout <= 3600:
        raise ValueError("timeout_seconds must be between 1 and 3600")
    public_policy = _public_policy(policy)
    payload = {
        "protocol_version": 1, "case_id": case["id"], "pack_id": case["pack_id"],
        "attempt_id": case.get("_attempt_id"),
        "run": case.get("_run_index", 1), "workspace": case.get("_workspace"),
        "profile_revision_id": revision["id"], "profile_path": revision["path"],
        "harness": harness, "variant": variant, "policy": public_policy,
    }
    started = time.perf_counter_ns()
    environment = {
        "TELOS_EVALUATION_VARIANT": variant,
        "TELOS_PROFILE_REVISION_ID": revision["id"], "TELOS_HARNESS": harness,
        "TELOS_ATTEMPT_ID": str(case.get("_attempt_id") or ""),
        "TELOS_EVALUATION_RUN": str(case.get("_run_index", 1)),
        "TELOS_EVALUATION_WORKSPACE": str(case.get("_workspace") or ""),
    }
    output = _run_json_command(
        command, payload, timeout=timeout,
        cwd=case.get("_workspace") or policy.get("cwd"), environment=environment,
        label="evaluation runner",
    )
    latency_us = (time.perf_counter_ns() - started) // 1_000
    if policy.get("runner_command"):
        if output.get("harness") != harness or output.get("profile_revision_id") != revision["id"]:
            raise ValueError("evaluation runner returned the wrong Harness/Profile identity")
    evaluator_command = policy.get("evaluator_command")
    if evaluator_command:
        private_path = Path(policy["payload_path"]) / "private.json"
        private = json.loads(private_path.read_text())
        scored = _run_json_command(
            evaluator_command,
            {**payload, "execution": output, "private": private},
            timeout=timeout, cwd=case.get("_workspace") or policy.get("cwd"),
            environment=environment, label="private evaluator",
        )
        scored["evidence"] = {
            **dict(output.get("evidence") or {}),
            **dict(scored.get("evidence") or {}),
        }
        output = scored
    output.setdefault("latency_us", latency_us)
    output.setdefault("cost_usd_micros", 0)
    output.setdefault("evidence", {})
    return output


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    return sorted(values)[max(0, (95 * len(values) + 99) // 100 - 1)]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _evaluation_workspace(
    store: Any, evaluation_run_id: str, attempt_id: str, fixture: str | None,
) -> Path:
    base = owned_directory(store.path.parent, "evaluation-workspaces")
    run_dir = base / evaluation_run_id
    run_dir.mkdir(exist_ok=True, mode=0o700)
    workspace = run_dir / attempt_id
    if fixture:
        shutil.copytree(fixture, workspace)
    else:
        workspace.mkdir(mode=0o700)
    os.chmod(workspace, 0o700)
    return workspace


def evaluate_candidate(
    store: Any, *, task_type: str, candidate_revision_id: str,
    reference_revision_id: str | None = None, runs: int = 1,
    runner: Callable[[Mapping[str, Any], Mapping[str, Any], str, str], Mapping[str, Any]] = command_case_runner,
) -> dict[str, Any]:
    state = store.get_evolution(task_type)
    if state is None:
        raise ValueError(f"task type does not exist: {task_type}")
    task = state["task_type"]
    if isinstance(runs, bool) or not isinstance(runs, int) or not 1 <= runs <= _MAX_RUNS_PER_CASE:
        raise ValueError(f"runs must be between 1 and {_MAX_RUNS_PER_CASE}")
    reference_id = reference_revision_id or task["production_profile_revision_id"]
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
    if any(revision is None for revision in revisions.values()):
        raise ValueError("evaluation Profile Revision does not exist")
    if revisions["candidate"]["parent_revision_id"] != reference_id:
        raise ValueError("Candidate must be based on the selected Reference Revision")
    for revision in revisions.values():
        if revision["task_type_id"] != task["id"]:
            raise ValueError("evaluation Profile Revision belongs to another task type")
        profile = validate_profile(revision["path"])
        if profile["digest"] != revision["digest"]:
            raise ValueError("Profile Revision metadata digest mismatch")
    run = store.create_evaluation_run(
        task_type_id=task["id"], reference_revision_id=reference_id,
        candidate_revision_id=candidate_revision_id,
    )
    trials: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    try:
        for case in cases:
            case_manifest = validate_frozen_case(case)
            harnesses = case["policy"].get("required_harnesses") or ["codex", "kimi-code"]
            if not isinstance(harnesses, list) or len(set(harnesses)) < 2:
                raise ValueError("each regression case must require at least two Harnesses")
            pack = store.get_context_pack(case["pack_id"])
            validate_context_pack(pack["path"])
            for harness in harnesses:
                report = compatibility_report(pack["path"], harness)
                for variant, revision in revisions.items():
                    for run_index in range(1, runs + 1):
                        attempt_id = str(uuid4())
                        workspace = _evaluation_workspace(
                            store, run["id"], attempt_id,
                            case["policy"].get("workspace_fixture_path"),
                        )
                        attempt = store.create_attempt(
                            row_id=attempt_id, task_run_id=pack["task_run_id"], harness=harness,
                            context_pack_id=pack["id"], profile_revision_id=revision["id"],
                            status="running", launch_plan={
                                "evaluation_run_id": run["id"], "variant": variant,
                                "run": run_index, "workspace": str(workspace),
                            },
                        )
                        try:
                            raw = dict(runner({
                                **case, "_attempt_id": attempt["id"],
                                "_run_index": run_index, "_workspace": str(workspace),
                            }, revision, harness, variant))
                            outcome = str(raw.get("outcome") or "error")
                            score = float(raw.get("score", 0.0))
                            if outcome not in {"pass", "fail", "error"}:
                                raise ValueError("evaluation outcome must be pass, fail, or error")
                            if not 0.0 <= score <= 100.0:
                                raise ValueError("evaluation score must be between 0 and 100")
                            store.set_attempt_status(
                                attempt["id"], "ok" if outcome != "error" else "error",
                            )
                        except Exception as exc:
                            outcome, score = "error", 0.0
                            raw = {"evidence": {"error": str(exc)}}
                            store.set_attempt_status(attempt["id"], "error")
                        evidence = dict(raw.get("evidence") or {})
                        evidence["compatibility"] = report["overall"]
                        evidence["trace_integrity"] = store.check_attempt_trace_integrity(attempt["id"])
                        try:
                            profile_after = validate_profile(revision["path"])
                            case_after = validate_frozen_case(case)
                            pack_after = validate_context_pack(pack["path"])
                            evidence["state_integrity"] = {
                                "passed": (
                                    profile_after["digest"] == revision["digest"]
                                    and case_after["digest"] == case_manifest["digest"]
                                    and pack_after["digest"] == pack["digest"]
                                ),
                            }
                        except ValueError as exc:
                            evidence["state_integrity"] = {"passed": False, "error": str(exc)}
                        trial = store.add_evaluation_trial(
                            evaluation_run_id=run["id"], regression_case_id=case["id"],
                            harness=harness, variant=variant, run_index=run_index,
                            attempt_id=attempt["id"], outcome=outcome, score=score,
                            cost_usd_micros=int(raw.get("cost_usd_micros", 0)),
                            latency_us=int(raw.get("latency_us", 0)), evidence=evidence,
                        )
                        trials.append({**trial, "protected": bool(case["protected"])})
                    cell = [
                        trial for trial in trials
                        if trial["regression_case_id"] == case["id"]
                        and trial["harness"] == harness and trial["variant"] == variant
                    ]
                    outcomes = [trial["outcome"] for trial in cell]
                    outcome = "error" if "error" in outcomes else "fail" if "fail" in outcomes else "pass"
                    integrity = all(
                        trial["evidence"].get("trace_integrity", {}).get("passed") is True
                        for trial in cell
                    )
                    state_integrity = all(
                        trial["evidence"].get("state_integrity", {}).get("passed") is True
                        for trial in cell
                    )
                    result = store.add_evaluation_result(
                        evaluation_run_id=run["id"], regression_case_id=case["id"],
                        harness=harness, variant=variant,
                        attempt_id=cell[0]["attempt_id"] if runs == 1 else None,
                        outcome=outcome, score=_mean([trial["score"] for trial in cell]),
                        cost_usd_micros=sum(trial["cost_usd_micros"] for trial in cell),
                        latency_us=_p95([trial["latency_us"] for trial in cell]),
                        evidence={
                            "compatibility": report["overall"],
                            "trace_integrity": {"passed": integrity},
                            "state_integrity": {"passed": state_integrity},
                            "trial_ids": [trial["id"] for trial in cell],
                            "attempt_ids": [trial["attempt_id"] for trial in cell],
                        },
                    )
                    results.append({**result, "protected": bool(case["protected"])})
        policy = task["evolution_policy"]
        reference = [item for item in results if item["variant"] == "reference"]
        candidate = [item for item in results if item["variant"] == "candidate"]
        paired = {
            (item["regression_case_id"], item["harness"]): item for item in reference
        }
        expected_results = sum(
            2 * len(set(case["policy"].get("required_harnesses") or ["codex", "kimi-code"]))
            for case in cases
        )
        validity = (
            len(results) == expected_results and len(trials) == expected_results * runs
            and all(item["outcome"] != "error" for item in results)
        )
        critical = not any(
            item["protected"] and paired[(item["regression_case_id"], item["harness"])]["outcome"] == "pass"
            and item["outcome"] != "pass" for item in candidate
        )
        ref_score = sum(item["score"] for item in reference) / len(reference)
        cand_score = sum(item["score"] for item in candidate) / len(candidate)
        improvement = cand_score - ref_score
        quality = improvement > 0 and improvement >= float(policy.get("minimum_improvement", 0.0))
        applicable = {
            variant: set(validate_profile(revision["path"])["applicable_harnesses"])
            for variant, revision in revisions.items()
        }
        portability = all(
            item["evidence"].get("compatibility") != "blocked"
            and item["harness"] in applicable[item["variant"]]
            for item in results
        )
        ref_cost, cand_cost = sum(item["cost_usd_micros"] for item in reference), sum(item["cost_usd_micros"] for item in candidate)
        cost = cand_cost <= ref_cost * float(policy.get("max_cost_ratio", 1.2)) if ref_cost else cand_cost == 0
        ref_p95 = _p95([item["latency_us"] for item in reference])
        cand_p95 = _p95([item["latency_us"] for item in candidate])
        latency = cand_p95 <= ref_p95 * float(policy.get("max_latency_ratio", 1.2)) if ref_p95 else cand_p95 == 0
        integrity = all(
            item["evidence"].get("trace_integrity", {}).get("passed") is True
            for item in results
        )
        state_integrity = all(
            item["evidence"].get("state_integrity", {}).get("passed") is True
            for item in results
        )
        gates = {
            "validity": {"passed": validity, "results": len(results), "trials": len(trials),
                         "runs_per_case": runs},
            "critical_regression": {"passed": critical},
            "outcome_quality": {"passed": quality, "reference": ref_score,
                                "candidate": cand_score, "improvement": improvement},
            "portability": {"passed": portability},
            "cost": {"passed": cost, "reference": ref_cost, "candidate": cand_cost},
            "latency": {"passed": latency, "reference_p95_us": ref_p95, "candidate_p95_us": cand_p95},
            "trace_integrity": {"passed": integrity},
            "state_integrity": {"passed": state_integrity},
        }
        passed = all(gate["passed"] for gate in gates.values())
        return store.finish_evaluation(run["id"], passed=passed, gates=gates)
    except Exception as exc:
        return store.finish_evaluation(
            run["id"], passed=False, error=True,
            gates={"internal_error": {"passed": False, "detail": str(exc)}},
        )


def optimize_profile(
    store: Any, *, task_type: str, rounds: int = 1, runs: int = 1,
    target_score: float | None = None, home: str | Path | None = None,
    optimizer_command: Sequence[str] | None = None,
    runner: Callable[[Mapping[str, Any], Mapping[str, Any], str, str], Mapping[str, Any]] = command_case_runner,
) -> dict[str, Any]:
    if isinstance(rounds, bool) or not isinstance(rounds, int) or not 1 <= rounds <= _MAX_OPTIMIZATION_ROUNDS:
        raise ValueError(f"rounds must be between 1 and {_MAX_OPTIMIZATION_ROUNDS}")
    if target_score is not None and (
        isinstance(target_score, bool) or not isinstance(target_score, (int, float))
        or not 0 <= target_score <= 100
    ):
        raise ValueError("target_score must be between 0 and 100")
    state = store.get_evolution(task_type)
    if state is None or state["task_type"]["production_profile_revision_id"] is None:
        raise ValueError("task type has no production profile")
    reference_id = state["task_type"]["production_profile_revision_id"]
    has_external_optimizer = bool(
        optimizer_command or state["task_type"]["evolution_policy"].get("optimizer_command")
    )
    history = []
    stop_reason = "round_limit"
    for round_index in range(1, rounds + 1):
        candidate = propose_candidate(
            store, task_type=task_type, home=home,
            reference_revision_id=reference_id, optimizer_command=optimizer_command,
        )
        evaluation = evaluate_candidate(
            store, task_type=task_type, candidate_revision_id=candidate["id"],
            reference_revision_id=reference_id, runs=runs, runner=runner,
        )
        history.append({
            "round": round_index, "reference_revision_id": reference_id,
            "candidate_revision_id": candidate["id"], "evaluation": evaluation,
            "hypothesis": candidate.get("metadata", {}).get("hypothesis"),
        })
        if evaluation["status"] == "passed":
            reference_id = candidate["id"]
            score = float(evaluation["gates"]["outcome_quality"]["candidate"])
            if target_score is not None and score >= target_score:
                stop_reason = "target_score"
                break
        elif not has_external_optimizer:
            stop_reason = "candidate_rejected"
            break
    return {
        "task_type": task_type, "reference_revision_id": reference_id,
        "rounds": history, "stop_reason": stop_reason,
        "promotion_required": reference_id != state["task_type"]["production_profile_revision_id"],
    }
