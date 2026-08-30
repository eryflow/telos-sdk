"""Knowledge-first evolution for one explicit long Task."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def _task(detail: Mapping[str, Any]) -> Mapping[str, Any]:
    value = detail.get("task")
    return value if isinstance(value, Mapping) else detail


def evolve_task_execution(
    store: Any, *, task_id: str, execution_id: str,
    knowledge_changes: Sequence[Mapping[str, Any]],
    skill_candidates: Sequence[Mapping[str, Any]] = (),
    agent_candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one verified evolution batch in Knowledge → Skill → Agent order."""
    detail = store.get_task(task_id)
    if detail is None:
        raise ValueError(f"Task does not exist: {task_id}")
    if not bool(_task(detail).get("self_evolve")):
        raise ValueError("self-evolve is disabled for this Task")
    execution = next(
        (item for item in store.list_task_executions(task_id) if item["id"] == execution_id),
        None,
    )
    if execution is None:
        raise ValueError("execution does not belong to this Task")
    if not bool(execution.get("trusted")) or not execution.get("outcome"):
        raise ValueError("self-evolve requires a trusted execution outcome")

    if any(not isinstance(change, Mapping) for change in knowledge_changes):
        raise ValueError("knowledge changes must be objects")
    if any(not isinstance(candidate, Mapping) for candidate in skill_candidates):
        raise ValueError("Skill Candidates must be objects")
    if agent_candidate is not None and not isinstance(agent_candidate, Mapping):
        raise ValueError("agent.md Candidate must be an object")

    executions = {item["id"]: item for item in store.list_task_executions(task_id)}
    for change in knowledge_changes:
        if not str(change.get("kind") or "").strip():
            raise ValueError("knowledge kind is required")
        if change.get("content") is None or change.get("content") == "":
            raise ValueError("knowledge content is required")
    for candidate in skill_candidates:
        refs = list(dict.fromkeys(candidate.get("execution_refs") or []))
        if not str(candidate.get("name") or "").strip():
            raise ValueError("Skill Candidate name is required")
        if candidate.get("content") is None or candidate.get("content") == "":
            raise ValueError("Skill Candidate content is required")
        if len(refs) < 3:
            raise ValueError("a Skill Candidate requires three distinct Execution refs")
        if any(
            ref not in executions or not bool(executions[ref].get("trusted"))
            or not executions[ref].get("outcome") or not executions[ref].get("evidence_refs")
            for ref in refs
        ):
            raise ValueError("Skill Candidate refs must be trusted Executions with evidence")
    if agent_candidate is not None:
        refs = list(dict.fromkeys(agent_candidate.get("execution_refs") or []))
        if len(refs) < 3:
            raise ValueError("an agent.md Candidate requires three distinct Execution refs")
        if any(
            ref not in executions or not bool(executions[ref].get("trusted"))
            or not executions[ref].get("outcome") or not executions[ref].get("evidence_refs")
            for ref in refs
        ):
            raise ValueError("agent.md Candidate refs must be trusted Executions with evidence")
        if not agent_candidate.get("knowledge_gap_ruled_out"):
            raise ValueError("agent.md evolution must first rule out a Knowledge gap")
        if not agent_candidate.get("skill_gap_ruled_out"):
            raise ValueError("agent.md evolution must first rule out a Skill gap")
        if not str(agent_candidate.get("agent_md") or "").strip():
            raise ValueError("agent.md Candidate content is required")

    knowledge = []
    for change in knowledge_changes:
        knowledge.append(store.add_task_knowledge(
            task_id,
            kind=str(change.get("kind") or "").strip(),
            content=change.get("content"),
            execution_id=execution_id,
            status=str(change.get("status") or "proposed"),
            source_refs=change.get("source_refs") or execution.get("evidence_refs") or [],
        ))

    skills = []
    for candidate in skill_candidates:
        refs = list(dict.fromkeys(candidate.get("execution_refs") or []))
        skills.append(store.add_task_skill(
            task_id,
            name=str(candidate.get("name") or "").strip(),
            content=candidate.get("content"), execution_refs=refs, state="candidate",
        ))

    agent = None
    if agent_candidate is not None:
        refs = list(dict.fromkeys(agent_candidate.get("execution_refs") or []))
        agent = store.create_task_agent_revision(
            task_id, agent_md=str(agent_candidate.get("agent_md") or "").strip(),
            state="candidate", evidence_refs=refs,
        )

    return {
        "task_id": task_id, "execution_id": execution_id,
        "phases": {"knowledge": knowledge, "skills": skills, "agent": agent},
    }
