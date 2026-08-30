from __future__ import annotations

import sqlite3

import pytest

from telos.tracing import SQLiteTraceStore


def _task(store: SQLiteTraceStore) -> dict:
    return store.create_task(
        name="blur optimizer",
        goal="make background blur faster without visible damage",
        contract={"acceptance": "quality and speed gates pass"},
        workspace={"root": "/repo"},
    )


def test_only_create_task_defines_a_long_task(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "telos.db") as store:
        run = store.create_task_run(goal="ordinary conversation")
        assert store.list_tasks() == []
        assert store.get_task_run(run["id"])["task_run"]["task_id"] is None

        task = _task(store)
        assert task["status"] == "defined"
        assert task["current_state"]["completed"] == []
        assert task["current_agent"]["state"] == "production"

        execution = store.create_task_execution(task["id"], harness="codex")
        attempt = store.create_attempt(task_execution_id=execution["id"], harness="codex")
        assert attempt["task_run_id"] == execution["task_run_id"]
        assert attempt["task_execution_id"] == execution["id"]


def test_execution_freezes_state_agent_and_bound_wiki_claims(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "telos.db") as store:
        task = _task(store)
        page = store.create_wiki_page(title="Blur quality", category="domains")
        first_claim = store.add_wiki_claim(page["id"], content="SSIM is a quality metric")
        store.bind_task_knowledge(task["id"], [first_claim["id"]])

        first = store.create_task_execution(task["id"], harness="codex")
        store.create_task_state_revision(
            task["id"], state={"status": "running", "next_action": "benchmark"},
            evidence_refs=["trace-state"], task_execution_id=first["id"],
        )
        agent = store.create_task_agent_revision(
            task["id"], agent_md="Always run the quality gate.",
        )
        store.promote_task_agent_revision(agent["id"], evidence_refs=["trace-agent"])
        local = store.add_task_knowledge(
            task["id"], kind="failure-pattern",
            content={"claim": "mean SSIM can hide local damage"},
            execution_id=first["id"], status="verified", source_refs=["trace-local"],
        )
        second_claim = store.add_wiki_claim(
            page["id"], content="Worst-region SSIM catches local damage",
        )
        store.bind_task_knowledge(task["id"], [second_claim["id"]])
        second = store.create_task_execution(task["id"], harness="codex")

        assert first["state_revision_id"] != second["state_revision_id"]
        assert first["agent_revision_id"] != second["agent_revision_id"]
        assert first["knowledge_manifest"] == [{
            "id": first_claim["id"], "page_id": page["id"],
            "revision": 1, "digest": first_claim["digest"], "kind": "fact",
            "content": "SSIM is a quality metric", "source_refs": [], "source": "wiki",
        }]
        assert {item["id"] for item in second["knowledge_manifest"]} == {
            second_claim["id"], local["id"],
        }
        local_snapshot = next(
            item for item in second["knowledge_manifest"] if item["id"] == local["id"]
        )
        assert local_snapshot["source"] == "task"
        assert local_snapshot["content"] == {"claim": "mean SSIM can hide local damage"}

        store._connection.execute(
            "UPDATE wiki_claims SET content='changed later' WHERE id=?", (second_claim["id"],),
        )
        frozen = store.get_task_execution(second["id"])
        assert frozen is not None
        assert frozen["state"]["next_action"] == "benchmark"
        assert frozen["agent"]["agent_md"] == "Always run the quality gate."
        wiki_snapshot = next(item for item in frozen["knowledge"] if item["source"] == "wiki")
        assert wiki_snapshot["content"] == "Worst-region SSIM catches local damage"


def test_state_completion_requires_audit_and_state_revisions_are_immutable(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "telos.db") as store:
        task = _task(store)
        execution = store.create_task_execution(task["id"], harness="codex")

        waiting = store.create_task_state_revision(
            task["id"], state={"requires_user": True, "blockers": ["choose metric"]},
            evidence_refs=["trace-question"], task_execution_id=execution["id"],
        )
        assert waiting["status"] == "waiting_user"
        assert store.get_task(task["id"])["status"] == "waiting_user"

        with pytest.raises(ValueError, match=r"complete \+ clean \+ aligned"):
            store.create_task_state_revision(
                task["id"], state={"status": "completed"},
                evidence_refs=["trace-final"], task_execution_id=execution["id"],
                audit={"complete": True, "clean": False, "aligned": True},
            )

        complete = store.create_task_state_revision(
            task["id"], state={"status": "completed", "completed": ["quality gate"]},
            evidence_refs=["trace-final"], task_execution_id=execution["id"],
            audit={"complete": True, "clean": True, "aligned": True},
        )
        assert store.get_task(task["id"])["status"] == "completed"
        assert store.list_task_executions(task["id"])[0]["trusted"] == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._connection.execute(
                "UPDATE task_state_revisions SET status='running' WHERE id=?",
                (complete["id"],),
            )


def test_skill_requires_three_distinct_trusted_executions(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "telos.db") as store:
        task = _task(store)
        executions = []
        for index in range(3):
            execution = store.create_task_execution(task["id"], harness="codex")
            executions.append(store.set_task_execution_status(
                execution["id"], "completed", outcome="pass",
                evidence_refs=[f"trace-{index}"], trusted=True,
            ))

        with pytest.raises(ValueError, match="three distinct"):
            store.add_task_skill(
                task["id"], name="quality-gated benchmark", content={"stages": 3},
                execution_refs=[executions[0]["id"]], state="candidate",
            )

        refs = [execution["id"] for execution in executions]
        with pytest.raises(ValueError, match="immutable"):
            store.set_task_execution_status(
                executions[0]["id"], "failed", outcome="fail",
                evidence_refs=["replacement"], trusted=True,
            )
        candidate = store.add_task_skill(
            task["id"], name="quality-gated benchmark", content={"stages": 3},
            execution_refs=refs, state="candidate",
        )
        with pytest.raises(ValueError, match="explicit promotion"):
            store.add_task_skill(
                task["id"], name="quality-gated benchmark", content={"stages": 3},
                execution_refs=refs, state="production",
            )
        production = store.promote_task_skill(candidate["id"])
        assert candidate["state"] == "candidate"
        assert production["state"] == "production"


def test_wiki_schema_is_bounded_and_graph_is_one_hop(tmp_path) -> None:
    with SQLiteTraceStore(tmp_path / "telos.db") as store:
        page = store.create_wiki_page(title="Optimizer", category="projects")
        metric = store.add_wiki_claim(page["id"], content="SSIM measures quality", kind="fact")
        procedure = store.add_wiki_claim(
            page["id"], content="Benchmark each candidate", kind="procedure",
        )
        relation = store.add_wiki_relation(
            source_claim_id=procedure["id"], target_claim_id=metric["id"],
            relation="depends-on",
        )
        graph = store.get_wiki_graph(root_id=procedure["id"])
        assert {item["id"] for item in graph["claims"]} == {metric["id"], procedure["id"]}
        assert graph["relations"] == [relation]

        with pytest.raises(ValueError, match="category"):
            store.create_wiki_page(title="Bad", category="misc")
        with pytest.raises(ValueError, match="kind"):
            store.add_wiki_claim(page["id"], content="Bad", kind="note")
        with pytest.raises(ValueError, match="relation"):
            store.add_wiki_relation(
                source_claim_id=procedure["id"], target_claim_id=metric["id"],
                relation="helps",
            )
