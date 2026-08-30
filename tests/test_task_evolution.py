from __future__ import annotations

import pytest

from telos.task_evolution import evolve_task_execution
from telos.tracing import SQLiteTraceStore


def _trusted_execution(store: SQLiteTraceStore, task_id: str, index: int) -> dict:
    execution = store.create_task_execution(task_id, harness="codex")
    return store.set_task_execution_status(
        execution["id"], "completed", outcome="pass",
        evidence_refs=[f"trace-{index}"], trusted=True,
    )


def test_task_evolution_is_explicit_trusted_and_knowledge_first(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "telos.db") as store:
        task = store.create_task(
            name="blur optimizer", goal="make background blur faster without visible damage",
            contract={"quality_gate": "ssim"}, self_evolve=True,
        )
        first = _trusted_execution(store, task["id"], 1)

        with pytest.raises(ValueError, match="three distinct"):
            evolve_task_execution(
                store, task_id=task["id"], execution_id=first["id"],
                knowledge_changes=[{
                    "kind": "failure-pattern",
                    "content": {"claim": "mean SSIM hides local damage"},
                }],
                skill_candidates=[{
                    "name": "quality-gated benchmark", "content": {"stages": 3},
                    "execution_refs": [first["id"]],
                }],
            )
        assert store.list_task_knowledge(task["id"]) == []

        second = _trusted_execution(store, task["id"], 2)
        third = _trusted_execution(store, task["id"], 3)
        result = evolve_task_execution(
            store, task_id=task["id"], execution_id=third["id"],
            knowledge_changes=[],
            skill_candidates=[{
                "name": "quality-gated benchmark", "content": {"stages": 3},
                "execution_refs": [first["id"], second["id"], third["id"]],
            }],
        )
        assert result["phases"]["skills"][0]["state"] == "candidate"


def test_agent_candidate_requires_residual_behavior_evidence(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "telos.db") as store:
        task = store.create_task(name="optimizer", goal="improve", contract={})
        executions = [_trusted_execution(store, task["id"], i) for i in range(3)]
        candidate = {
            "agent_md": "Reject completion until the quality audit passes.",
            "execution_refs": [item["id"] for item in executions],
            "knowledge_gap_ruled_out": True,
            "skill_gap_ruled_out": False,
        }
        with pytest.raises(ValueError, match="Skill gap"):
            evolve_task_execution(
                store, task_id=task["id"], execution_id=executions[-1]["id"],
                knowledge_changes=[], agent_candidate=candidate,
            )
