from __future__ import annotations

from pathlib import Path

from telos.context_pack import create_context_pack
from telos.evolution import (
    command_case_runner,
    create_profile,
    evaluate_candidate,
    freeze_case,
    propose_candidate,
    validate_profile,
)
from telos.tracing import SQLiteTraceStore
from telos.training_export import export_training_data


def _fixture(store: SQLiteTraceStore, home: Path):
    production = create_profile(
        store, task_type="code-defect-repair", state="production", home=home,
        instructions="Inspect the failing behavior and verify the fix.",
        context_policy={"recent_messages": 8}, tool_policy={"retry_limit": 1},
        evaluation_policy={
            "minimum_cases": 1, "minimum_improvement": 0.1,
            "max_cost_ratio": 1.2, "max_latency_ratio": 1.2,
        },
    )
    task = store.get_evolution("code-defect-repair")["task_type"]
    run = store.create_task_run(
        goal="repair persistent tab selection", task_type_id=task["id"],
    )
    attempt = store.create_attempt(task_run_id=run["id"], harness="codex")
    assert attempt["profile_revision_id"] == production["id"]
    manifest, pack_path = create_context_pack(
        home=home, task_run_id=run["id"], source_attempt_id=attempt["id"],
        profile_revision_id=production["id"], task_type="code-defect-repair",
        objective={"goal": run["goal"]}, policy={"profile": production["digest"]},
        progress={"done": ["reproduced"], "next": ["fix"]},
        memory={"facts": ["poll refresh resets selection"]},
        provenance={"attempt_id": attempt["id"]},
    )
    store.register_context_pack(manifest, pack_path)
    outcome = store.resolve_outcome(
        task_run_id=run["id"], attempt_id=attempt["id"], outcome="fail",
        classification="tool-error", score=0.4,
        evidence={"trace_ids": ["trace-1"]},
    )
    case = freeze_case(
        store, task_type="code-defect-repair", pack_id=manifest["pack_id"],
        outcome_resolution_id=outcome["id"], protected=True,
        policy={"required_harnesses": ["codex", "kimi-code"]},
    )
    return production, run, case


def _runner(store: SQLiteTraceStore, *, regress: bool = False):
    def run(case, revision, harness, variant):
        del revision
        attempt_id = case["_attempt_id"]
        thread_id, trace_id = f"thread-{attempt_id}", f"trace-{attempt_id}"
        store.upsert_thread({
            "id": thread_id, "harness": harness, "external_id": thread_id,
            "attempt_id": attempt_id, "start_time_us": 100,
        })
        store.upsert_trace({
            "id": trace_id, "thread_id": thread_id, "harness": harness,
            "source": "evaluation", "external_id": trace_id, "name": "evaluation",
            "status": "ok", "start_time_us": 100, "end_time_us": 200,
            "source_updated_at_us": 200,
        })
        store.upsert_span({
            "id": f"span-{attempt_id}", "trace_id": trace_id,
            "source": "evaluation", "external_id": "llm", "name": "model",
            "type": "llm", "status": "ok", "start_time_us": 120,
            "end_time_us": 180, "source_updated_at_us": 180,
        })
        outcome = "fail" if regress and variant == "candidate" else "pass"
        return {
            "outcome": outcome, "score": 0.8 if variant == "candidate" else 0.5,
            "cost_usd_micros": 9 if variant == "candidate" else 10,
            "latency_us": 90 if variant == "candidate" else 100,
            "evidence": {"trace_id": trace_id},
        }
    return run


def test_candidate_matrix_manual_promote_and_history_safe_rollback(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "trace.db") as store:
        production, run, _ = _fixture(store, tmp_path / "telos")
        candidate = propose_candidate(store, task_type="code-defect-repair", home=tmp_path / "telos")
        assert candidate["change_dimension"] == "instructions"
        assert validate_profile(candidate["path"])["digest"] == candidate["digest"]

        evaluation = evaluate_candidate(
            store, task_type="code-defect-repair",
            candidate_revision_id=candidate["id"], runner=_runner(store),
        )
        assert evaluation["status"] == "passed"
        assert all(gate["passed"] for gate in evaluation["gates"].values())
        assert store.get_profile_revision(candidate["id"])["state"] == "recommended"
        state = store.get_evolution("code-defect-repair")
        assert len(state["latest_results"]) == 4
        assert {item["harness"] for item in state["latest_results"]} == {"codex", "kimi-code"}

        store.promote_profile(candidate["id"])
        promoted_attempt = store.create_attempt(task_run_id=run["id"], harness="kimi-code")
        assert promoted_attempt["profile_revision_id"] == candidate["id"]
        store.rollback_task_type(state["task_type"]["id"])
        rolled_back_attempt = store.create_attempt(task_run_id=run["id"], harness="codex")
        assert rolled_back_attempt["profile_revision_id"] == production["id"]
        assert store.get_task_run(run["id"])["attempts"][-2]["profile_revision_id"] == candidate["id"]


def test_protected_regression_rejects_candidate(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "trace.db") as store:
        _fixture(store, tmp_path / "telos")
        candidate = propose_candidate(store, task_type="code-defect-repair", home=tmp_path / "telos")

        evaluation = evaluate_candidate(
            store, task_type="code-defect-repair",
            candidate_revision_id=candidate["id"], runner=_runner(store, regress=True),
        )
        assert evaluation["status"] == "failed"
        assert evaluation["gates"]["critical_regression"]["passed"] is False
        assert store.get_profile_revision(candidate["id"])["state"] == "rejected"


def test_training_exports_keep_evidence_provenance(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "trace.db") as store:
        _, run, _ = _fixture(store, tmp_path / "telos")
        candidate = propose_candidate(store, task_type="code-defect-repair", home=tmp_path / "telos")
        evaluate_candidate(
            store, task_type="code-defect-repair",
            candidate_revision_id=candidate["id"], runner=_runner(store),
        )
        attempt = store.create_attempt(task_run_id=run["id"], harness="codex")
        store.upsert_thread({
            "id": "training-thread", "harness": "codex", "external_id": "training-session",
            "attempt_id": attempt["id"], "start_time_us": 100,
        })
        store.upsert_trace({
            "id": "training-trace", "thread_id": "training-thread", "harness": "codex",
            "source": "test", "external_id": "training-turn", "name": "turn", "status": "ok",
            "start_time_us": 100, "end_time_us": 200, "source_updated_at_us": 200,
            "input": "fix tabs", "output": "fixed and verified",
        })
        store.resolve_outcome(
            task_run_id=run["id"], attempt_id=attempt["id"], outcome="pass", score=1.0,
        )
        result = export_training_data(store, tmp_path / "export", task_type="code-defect-repair")

    assert result["counts"] == {"sft": 1, "preference": 2, "rl": 4}
    sft = Path(result["paths"]["sft"]).read_text()
    preference = Path(result["paths"]["preference"]).read_text()
    rl = Path(result["paths"]["rl"]).read_text()
    assert "training-trace" in sft
    assert "evaluation_run_id" in preference
    assert "attempt_id" in rl


def test_evaluation_cannot_self_attest_trace_integrity(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "trace.db") as store:
        _fixture(store, tmp_path / "telos")
        candidate = propose_candidate(store, task_type="code-defect-repair", home=tmp_path / "telos")

        def untraced(case, revision, harness, variant):
            del case, revision, harness, variant
            return {
                "outcome": "pass", "score": 1.0, "cost_usd_micros": 0,
                "latency_us": 0, "evidence": {"trace_integrity": True},
            }

        evaluation = evaluate_candidate(
            store, task_type="code-defect-repair",
            candidate_revision_id=candidate["id"], runner=untraced,
        )
        assert evaluation["status"] == "failed"
        assert evaluation["gates"]["trace_integrity"]["passed"] is False


def test_command_evaluator_receives_public_view_and_attempt_identity(tmp_path) -> None:
    evaluator = tmp_path / "evaluate.py"
    evaluator.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "payload=json.load(sys.stdin)\n"
        "print(json.dumps({'outcome':'pass','score':0.75,'cost_usd_micros':2,"
        "'evidence':{'attempt':os.environ['TELOS_ATTEMPT_ID'],"
        "'private_gold_visible':'private_gold' in payload['policy']}}))\n"
    )
    evaluator.chmod(0o755)
    result = command_case_runner(
        {
            "id": "case-1", "pack_id": "pack-1", "_attempt_id": "attempt-1",
            "policy": {"command": [str(evaluator)], "private_gold": "do not leak"},
        },
        {"id": "revision-1", "path": str(tmp_path / "profile")},
        "codex", "candidate",
    )
    assert result["outcome"] == "pass"
    assert result["evidence"] == {
        "attempt": "attempt-1", "private_gold_visible": False,
    }
