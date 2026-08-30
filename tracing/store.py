"""SQLite persistence and queries for local agent traces."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Iterable, Iterator, Mapping
from uuid import NAMESPACE_URL, uuid4, uuid5

from .core import SPAN_TYPES, STATUSES, Span, Trace, TraceProcessor, now_us


_FINAL_STATUSES = frozenset({"ok", "error", "cancelled"})
_LATE_END_FIELDS = frozenset({
    "end_time_us", "output_json", "error_json", "usage_json", "input_tokens",
    "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
    "model", "provider", "cost_usd_micros", "ttft_us",
})
_JSON_COLUMNS = {
    "input": "input_json",
    "output": "output_json",
    "metadata": "metadata_json",
    "tags": "tags_json",
    "error": "error_json",
    "usage": "usage_json",
}
_SENSITIVE_METADATA_KEYS = frozenset({
    "authorization", "cookie", "set-cookie", "token", "access_token",
    "refresh_token", "api_key", "apikey", "secret", "client_secret",
    "password", "env", "environment",
})
_WIKI_CATEGORIES = frozenset({
    "people", "projects", "domains", "playbooks", "decisions", "lessons", "assets",
})
_WIKI_CLAIM_KINDS = frozenset({
    "fact", "preference", "decision", "procedure", "failure-pattern", "asset", "glossary",
})
_WIKI_RELATIONS = frozenset({
    "is-a", "part-of", "belongs-to", "depends-on", "applies-to",
    "contradicts", "supersedes", "derived-from", "used-by", "similar-to",
})


_SCHEMA = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at_us INTEGER NOT NULL
);
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at_us INTEGER NOT NULL,
    last_updated_at_us INTEGER NOT NULL
);
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    harness TEXT NOT NULL,
    external_id TEXT NOT NULL,
    name TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','ok','error','cancelled','abandoned','unknown')),
    start_time_us INTEGER NOT NULL,
    end_time_us INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_us INTEGER NOT NULL,
    last_updated_at_us INTEGER NOT NULL,
    UNIQUE (project_id, harness, external_id),
    CHECK (end_time_us IS NULL OR end_time_us >= start_time_us)
);
CREATE TABLE traces (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    harness TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','ok','error','cancelled','abandoned','unknown')),
    start_time_us INTEGER NOT NULL,
    end_time_us INTEGER,
    input_json TEXT,
    output_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    tags_json TEXT NOT NULL DEFAULT '[]',
    error_json TEXT,
    source_updated_at_us INTEGER NOT NULL,
    created_at_us INTEGER NOT NULL,
    last_updated_at_us INTEGER NOT NULL,
    UNIQUE (project_id, harness, external_id),
    CHECK (end_time_us IS NULL OR end_time_us >= start_time_us)
);
CREATE TABLE spans (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    parent_span_id TEXT REFERENCES spans(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'general'
        CHECK (type IN ('general','agent','llm','tool','approval','compaction')),
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','ok','error','cancelled','abandoned','unknown')),
    start_time_us INTEGER NOT NULL,
    end_time_us INTEGER,
    input_json TEXT,
    output_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    tags_json TEXT NOT NULL DEFAULT '[]',
    usage_json TEXT NOT NULL DEFAULT '{}',
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reasoning_tokens INTEGER,
    model TEXT,
    provider TEXT,
    cost_usd_micros INTEGER,
    ttft_us INTEGER,
    error_json TEXT,
    source_updated_at_us INTEGER NOT NULL,
    created_at_us INTEGER NOT NULL,
    last_updated_at_us INTEGER NOT NULL,
    UNIQUE (trace_id, source, external_id),
    CHECK (end_time_us IS NULL OR end_time_us >= start_time_us)
);
CREATE TABLE feedback_scores (
    id TEXT PRIMARY KEY,
    trace_id TEXT REFERENCES traces(id) ON DELETE CASCADE,
    span_id TEXT REFERENCES spans(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value REAL NOT NULL,
    reason TEXT,
    source TEXT NOT NULL DEFAULT 'user',
    created_at_us INTEGER NOT NULL,
    last_updated_at_us INTEGER NOT NULL,
    CHECK ((trace_id IS NOT NULL AND span_id IS NULL) OR
           (trace_id IS NULL AND span_id IS NOT NULL))
);
CREATE INDEX idx_threads_project_start ON threads(project_id, start_time_us DESC, id DESC);
CREATE INDEX idx_traces_project_start ON traces(project_id, start_time_us DESC, id DESC);
CREATE INDEX idx_traces_thread_start ON traces(thread_id, start_time_us, id);
CREATE INDEX idx_traces_harness_status_start ON traces(harness, status, start_time_us DESC);
CREATE INDEX idx_spans_trace_start ON spans(trace_id, start_time_us, id);
CREATE INDEX idx_spans_parent_start ON spans(parent_span_id, start_time_us, id);
CREATE INDEX idx_spans_type_model_start ON spans(type, model, start_time_us DESC);
CREATE INDEX idx_feedback_trace ON feedback_scores(trace_id, created_at_us);
CREATE INDEX idx_feedback_span ON feedback_scores(span_id, created_at_us);
"""

_MIGRATION_2 = """
CREATE TABLE task_types (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    production_profile_revision_id TEXT,
    evolution_policy_json TEXT NOT NULL DEFAULT '{}',
    created_at_us INTEGER NOT NULL,
    last_updated_at_us INTEGER NOT NULL
);
CREATE TABLE profile_revisions (
    id TEXT PRIMARY KEY,
    task_type_id TEXT NOT NULL REFERENCES task_types(id) ON DELETE CASCADE,
    parent_revision_id TEXT REFERENCES profile_revisions(id),
    digest TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('draft','evaluating','recommended','production','rejected','rolled_back')),
    change_dimension TEXT,
    path TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_us INTEGER NOT NULL,
    last_updated_at_us INTEGER NOT NULL
);
CREATE TABLE task_runs (
    id TEXT PRIMARY KEY,
    task_type_id TEXT REFERENCES task_types(id),
    goal TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','ok','error','cancelled','abandoned','unknown')),
    workspace_json TEXT NOT NULL DEFAULT '{}',
    created_at_us INTEGER NOT NULL,
    finished_at_us INTEGER,
    last_updated_at_us INTEGER NOT NULL
);
CREATE TABLE context_packs (
    id TEXT PRIMARY KEY,
    digest TEXT NOT NULL UNIQUE,
    parent_pack_id TEXT REFERENCES context_packs(id),
    task_run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    source_attempt_id TEXT REFERENCES attempts(id),
    profile_revision_id TEXT REFERENCES profile_revisions(id),
    schema_version INTEGER NOT NULL,
    capture_status TEXT NOT NULL CHECK (capture_status IN ('complete','partial','dirty','invalid')),
    capture_method TEXT NOT NULL CHECK (capture_method IN ('native','cooperative','reconstructed','assisted')),
    path TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at_us INTEGER NOT NULL
);
CREATE TABLE attempts (
    id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    harness TEXT NOT NULL,
    source_attempt_id TEXT REFERENCES attempts(id),
    context_pack_id TEXT REFERENCES context_packs(id),
    profile_revision_id TEXT REFERENCES profile_revisions(id),
    status TEXT NOT NULL CHECK (status IN ('planned','running','ok','error','cancelled','abandoned','unknown')),
    launch_plan_json TEXT NOT NULL DEFAULT '{}',
    started_at_us INTEGER,
    finished_at_us INTEGER,
    created_at_us INTEGER NOT NULL,
    last_updated_at_us INTEGER NOT NULL
);
CREATE TABLE handoffs (
    id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    source_attempt_id TEXT REFERENCES attempts(id),
    destination_attempt_id TEXT REFERENCES attempts(id),
    context_pack_id TEXT NOT NULL REFERENCES context_packs(id),
    destination_harness TEXT NOT NULL,
    compatibility_json TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL CHECK (status IN ('planned','running','ok','error','blocked')),
    created_at_us INTEGER NOT NULL,
    last_updated_at_us INTEGER NOT NULL
);
CREATE TABLE outcome_resolutions (
    id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    attempt_id TEXT REFERENCES attempts(id),
    outcome TEXT NOT NULL CHECK (outcome IN ('pass','fail','unknown')),
    classification TEXT,
    score REAL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    frozen INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0,1)),
    created_at_us INTEGER NOT NULL,
    last_updated_at_us INTEGER NOT NULL
);
CREATE TABLE regression_cases (
    id TEXT PRIMARY KEY,
    task_type_id TEXT NOT NULL REFERENCES task_types(id) ON DELETE CASCADE,
    pack_id TEXT NOT NULL REFERENCES context_packs(id),
    outcome_resolution_id TEXT NOT NULL REFERENCES outcome_resolutions(id),
    digest TEXT NOT NULL UNIQUE,
    protected INTEGER NOT NULL DEFAULT 0 CHECK (protected IN (0,1)),
    policy_json TEXT NOT NULL DEFAULT '{}',
    created_at_us INTEGER NOT NULL
);
CREATE TABLE evaluation_runs (
    id TEXT PRIMARY KEY,
    task_type_id TEXT NOT NULL REFERENCES task_types(id) ON DELETE CASCADE,
    reference_revision_id TEXT NOT NULL REFERENCES profile_revisions(id),
    candidate_revision_id TEXT NOT NULL REFERENCES profile_revisions(id),
    status TEXT NOT NULL CHECK (status IN ('planned','running','passed','failed','error')),
    gates_json TEXT NOT NULL DEFAULT '{}',
    created_at_us INTEGER NOT NULL,
    finished_at_us INTEGER,
    last_updated_at_us INTEGER NOT NULL
);
CREATE TABLE evaluation_results (
    id TEXT PRIMARY KEY,
    evaluation_run_id TEXT NOT NULL REFERENCES evaluation_runs(id) ON DELETE CASCADE,
    regression_case_id TEXT NOT NULL REFERENCES regression_cases(id),
    harness TEXT NOT NULL,
    variant TEXT NOT NULL CHECK (variant IN ('reference','candidate')),
    attempt_id TEXT REFERENCES attempts(id),
    outcome TEXT NOT NULL CHECK (outcome IN ('pass','fail','error')),
    score REAL NOT NULL,
    cost_usd_micros INTEGER NOT NULL DEFAULT 0,
    latency_us INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at_us INTEGER NOT NULL,
    UNIQUE (evaluation_run_id, regression_case_id, harness, variant)
);
CREATE TABLE promotions (
    id TEXT PRIMARY KEY,
    task_type_id TEXT NOT NULL REFERENCES task_types(id) ON DELETE CASCADE,
    from_revision_id TEXT REFERENCES profile_revisions(id),
    to_revision_id TEXT NOT NULL REFERENCES profile_revisions(id),
    action TEXT NOT NULL CHECK (action IN ('promote','rollback')),
    created_at_us INTEGER NOT NULL
);
ALTER TABLE threads ADD COLUMN attempt_id TEXT REFERENCES attempts(id);
ALTER TABLE traces ADD COLUMN attempt_id TEXT REFERENCES attempts(id);
ALTER TABLE traces ADD COLUMN context_pack_id TEXT REFERENCES context_packs(id);
ALTER TABLE traces ADD COLUMN profile_revision_id TEXT REFERENCES profile_revisions(id);
CREATE INDEX idx_attempts_task_run_created ON attempts(task_run_id,created_at_us,id);
CREATE INDEX idx_packs_task_run_created ON context_packs(task_run_id,created_at_us,id);
CREATE INDEX idx_handoffs_task_run_created ON handoffs(task_run_id,created_at_us,id);
CREATE INDEX idx_threads_attempt ON threads(attempt_id);
CREATE INDEX idx_traces_attempt ON traces(attempt_id,start_time_us,id);
"""

_MIGRATION_3 = """
CREATE TABLE evaluation_trials (
    id TEXT PRIMARY KEY,
    evaluation_run_id TEXT NOT NULL REFERENCES evaluation_runs(id) ON DELETE CASCADE,
    regression_case_id TEXT NOT NULL REFERENCES regression_cases(id),
    harness TEXT NOT NULL,
    variant TEXT NOT NULL CHECK (variant IN ('reference','candidate')),
    run_index INTEGER NOT NULL CHECK (run_index >= 1),
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    outcome TEXT NOT NULL CHECK (outcome IN ('pass','fail','error')),
    score REAL NOT NULL,
    cost_usd_micros INTEGER NOT NULL DEFAULT 0,
    latency_us INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at_us INTEGER NOT NULL,
    UNIQUE (evaluation_run_id,regression_case_id,harness,variant,run_index)
);
CREATE INDEX idx_evaluation_trials_run
    ON evaluation_trials(evaluation_run_id,regression_case_id,harness,variant,run_index);
"""

_MIGRATION_4 = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    goal TEXT NOT NULL,
    contract_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN
        ('defined','ready','running','waiting_user','blocked','completed','cancelled')),
    self_evolve INTEGER NOT NULL DEFAULT 1 CHECK (self_evolve IN (0,1)),
    workspace_json TEXT NOT NULL DEFAULT '{}',
    current_state_revision_id TEXT,
    current_agent_revision_id TEXT,
    created_at_us INTEGER NOT NULL,
    last_updated_at_us INTEGER NOT NULL
);
ALTER TABLE task_runs ADD COLUMN task_id TEXT REFERENCES tasks(id);
CREATE INDEX idx_task_runs_task_created ON task_runs(task_id,created_at_us,id);

CREATE TABLE task_state_revisions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    parent_revision_id TEXT REFERENCES task_state_revisions(id),
    task_execution_id TEXT REFERENCES task_executions(id),
    status TEXT NOT NULL CHECK (status IN
        ('defined','ready','running','waiting_user','blocked','completed','cancelled')),
    completed_json TEXT NOT NULL DEFAULT '[]',
    incomplete_json TEXT NOT NULL DEFAULT '[]',
    blockers_json TEXT NOT NULL DEFAULT '[]',
    risks_json TEXT NOT NULL DEFAULT '[]',
    untrusted_do_not_reuse_json TEXT NOT NULL DEFAULT '[]',
    next_action_json TEXT,
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    audit_complete INTEGER NOT NULL DEFAULT 0 CHECK (audit_complete IN (0,1)),
    audit_clean INTEGER NOT NULL DEFAULT 0 CHECK (audit_clean IN (0,1)),
    audit_aligned INTEGER NOT NULL DEFAULT 0 CHECK (audit_aligned IN (0,1)),
    created_at_us INTEGER NOT NULL
);
CREATE INDEX idx_task_state_task_created
    ON task_state_revisions(task_id,created_at_us,id);
CREATE TRIGGER task_state_revisions_immutable_update
BEFORE UPDATE ON task_state_revisions BEGIN
    SELECT RAISE(ABORT, 'task state revisions are immutable');
END;
CREATE TRIGGER task_state_revisions_immutable_delete
BEFORE DELETE ON task_state_revisions BEGIN
    SELECT RAISE(ABORT, 'task state revisions are immutable');
END;

CREATE TABLE task_agent_revisions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    parent_revision_id TEXT REFERENCES task_agent_revisions(id),
    state TEXT NOT NULL CHECK (state IN ('candidate','production','rejected')),
    agent_md TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    created_at_us INTEGER NOT NULL
);
CREATE INDEX idx_task_agent_task_created
    ON task_agent_revisions(task_id,created_at_us,id);

CREATE TABLE task_executions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    task_run_id TEXT NOT NULL UNIQUE REFERENCES task_runs(id) ON DELETE CASCADE,
    harness TEXT NOT NULL,
    state_revision_id TEXT NOT NULL REFERENCES task_state_revisions(id),
    agent_revision_id TEXT NOT NULL REFERENCES task_agent_revisions(id),
    knowledge_manifest_json TEXT NOT NULL DEFAULT '[]',
    skill_manifest_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN
        ('planned','running','waiting_user','blocked','completed','failed','cancelled')),
    outcome TEXT CHECK (outcome IN ('pass','fail','unknown')),
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    trusted INTEGER NOT NULL DEFAULT 0 CHECK (trusted IN (0,1)),
    created_at_us INTEGER NOT NULL,
    finished_at_us INTEGER,
    last_updated_at_us INTEGER NOT NULL
);
CREATE INDEX idx_task_executions_task_created
    ON task_executions(task_id,created_at_us,id);
ALTER TABLE attempts ADD COLUMN task_execution_id TEXT REFERENCES task_executions(id);
CREATE INDEX idx_attempts_task_execution ON attempts(task_execution_id,created_at_us,id);

CREATE TABLE task_knowledge (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    content_json TEXT NOT NULL,
    execution_id TEXT NOT NULL REFERENCES task_executions(id),
    status TEXT NOT NULL CHECK (status IN ('proposed','verified','rejected','promoted')),
    source_refs_json TEXT NOT NULL DEFAULT '[]',
    created_at_us INTEGER NOT NULL
);
CREATE INDEX idx_task_knowledge_task_created ON task_knowledge(task_id,created_at_us,id);

CREATE TABLE task_skills (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    content_json TEXT NOT NULL,
    execution_refs_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('candidate','production','rejected')),
    created_at_us INTEGER NOT NULL
);
CREATE INDEX idx_task_skills_task_created ON task_skills(task_id,created_at_us,id);

CREATE TABLE wiki_pages (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT,
    content TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    digest TEXT NOT NULL,
    created_at_us INTEGER NOT NULL,
    last_updated_at_us INTEGER NOT NULL
);
CREATE TABLE wiki_claims (
    id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    source_refs_json TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    digest TEXT NOT NULL,
    created_at_us INTEGER NOT NULL
);
CREATE INDEX idx_wiki_claims_page_created ON wiki_claims(page_id,created_at_us,id);
CREATE TABLE wiki_relations (
    id TEXT PRIMARY KEY,
    source_claim_id TEXT NOT NULL REFERENCES wiki_claims(id) ON DELETE CASCADE,
    target_claim_id TEXT NOT NULL REFERENCES wiki_claims(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    created_at_us INTEGER NOT NULL,
    UNIQUE (source_claim_id,target_claim_id,relation),
    CHECK (source_claim_id <> target_claim_id)
);
CREATE TABLE task_knowledge_bindings (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    claim_id TEXT NOT NULL REFERENCES wiki_claims(id) ON DELETE CASCADE,
    created_at_us INTEGER NOT NULL,
    PRIMARY KEY (task_id,claim_id)
);
CREATE INDEX idx_task_knowledge_bindings_claim ON task_knowledge_bindings(claim_id,task_id);
"""


def _required(body: Mapping[str, Any], key: str) -> Any:
    value = body.get(key)
    if value is None or value == "":
        raise ValueError(f"{key} is required")
    return value


def _integer(value: Any, key: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    return value


def _sanitize_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            sensitive = (
                normalized in _SENSITIVE_METADATA_KEYS
                or normalized.endswith(("_authorization", "_cookie", "_token", "_api_key", "_secret", "_password"))
                or normalized.startswith("environment_")
            )
            clean[str(key)] = "[REDACTED]" if sensitive else _sanitize_metadata(item)
        return clean
    if isinstance(value, list):
        return [_sanitize_metadata(item) for item in value]
    return value


def _json(value: Any, default: Any) -> str:
    if value is None:
        value = default
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _status_after(current: str, incoming: str) -> str:
    if current in _FINAL_STATUSES:
        return current
    if current in {"unknown", "abandoned"} and incoming not in _FINAL_STATUSES:
        return current
    return incoming


def _encode_cursor(start_time_us: int, row_id: str) -> str:
    raw = json.dumps([start_time_us, row_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        start, row_id = json.loads(raw)
        return _integer(start, "cursor start_time_us", minimum=0), str(row_id)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid trace cursor") from exc


class SQLiteTraceStore:
    """Single-file tracing store. One instance serializes its writes with a short lock."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else Path.home() / ".telos" / "telos.db"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = RLock()
        self._connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._migrate()
        os.chmod(self.path, 0o600)

    def _migrate(self) -> None:
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        applied = now_us()
        if not exists:
            self._connection.executescript(
                f"BEGIN IMMEDIATE;\n{_SCHEMA}\n"
                f"INSERT INTO schema_migrations(version, applied_at_us) VALUES (1, {applied});\nCOMMIT;"
            )
        current = self._connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
        ).fetchone()[0]
        if current < 2:
            self._connection.executescript(
                f"BEGIN IMMEDIATE;\n{_MIGRATION_2}\n"
                f"INSERT INTO schema_migrations(version, applied_at_us) VALUES (2, {applied});\nCOMMIT;"
            )
            current = 2
        if current < 3:
            self._connection.executescript(
                f"BEGIN IMMEDIATE;\n{_MIGRATION_3}\n"
                f"INSERT INTO schema_migrations(version, applied_at_us) VALUES (3, {applied});\nCOMMIT;"
            )
            current = 3
        if current < 4:
            self._connection.executescript(
                f"BEGIN IMMEDIATE;\n{_MIGRATION_4}\n"
                f"INSERT INTO schema_migrations(version, applied_at_us) VALUES (4, {applied});\nCOMMIT;"
            )
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO projects(id,name,created_at_us,last_updated_at_us) VALUES (?,?,?,?)",
                ("default", "default", applied, applied),
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteTraceStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def upsert_project(self, body: Mapping[str, Any]) -> dict[str, Any]:
        with self._transaction():
            return self._upsert_project(body)

    def ensure_project(self, name: str = "default") -> dict[str, Any]:
        project_id = "default" if name == "default" else str(uuid5(NAMESPACE_URL, f"telos:project:{name}"))
        with self._transaction():
            result = self._upsert_project({"id": project_id, "name": name})
            row = self._connection.execute(
                "SELECT * FROM projects WHERE id=?", (result["id"],)
            ).fetchone()
        assert row is not None
        return dict(row)

    def ensure_task_type(
        self, name: str, evolution_policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("task type name is required")
        row_id = str(uuid5(NAMESPACE_URL, f"telos:task-type:{name}"))
        timestamp = now_us()
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM task_types WHERE name=?", (name,)
            ).fetchone()
            if row is None:
                self._connection.execute(
                    """INSERT INTO task_types
                       (id,name,evolution_policy_json,created_at_us,last_updated_at_us)
                       VALUES (?,?,?,?,?)""",
                    (row_id, name, _json(evolution_policy, {}), timestamp, timestamp),
                )
            elif evolution_policy is not None:
                self._connection.execute(
                    "UPDATE task_types SET evolution_policy_json=?,last_updated_at_us=? WHERE id=?",
                    (_json(evolution_policy, {}), timestamp, row["id"]),
                )
                row_id = row["id"]
            else:
                row_id = row["id"]
            row = self._connection.execute(
                "SELECT * FROM task_types WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def create_task(
        self, *, name: str, goal: str, contract: Mapping[str, Any] | None = None,
        workspace: Mapping[str, Any] | None = None, self_evolve: bool = True,
        status: str = "defined", agent_md: str = "", row_id: str | None = None,
    ) -> dict[str, Any]:
        name, goal = name.strip(), goal.strip()
        if not name:
            raise ValueError("name is required")
        if not goal:
            raise ValueError("goal is required")
        if not isinstance(self_evolve, bool):
            raise ValueError("self_evolve must be a boolean")
        self._validate_task_status(status)
        if not agent_md:
            agent_md = (
                "Preserve the Task Contract. Update state only from evidence. "
                "Claim completion only when complete, clean, and aligned."
            )
        row_id, timestamp = row_id or str(uuid4()), now_us()
        state_id, agent_id = str(uuid4()), str(uuid4())
        with self._transaction():
            self._connection.execute(
                """INSERT INTO tasks
                   (id,name,goal,contract_json,status,self_evolve,workspace_json,
                    created_at_us,last_updated_at_us)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (row_id, name, goal, _json(contract, {}), status, int(self_evolve),
                 _json(workspace, {}), timestamp, timestamp),
            )
            self._connection.execute(
                """INSERT INTO task_state_revisions
                   (id,task_id,status,completed_json,incomplete_json,blockers_json,
                    risks_json,untrusted_do_not_reuse_json,next_action_json,
                    evidence_refs_json,created_at_us)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (state_id, row_id, status, "[]", "[]", "[]", "[]", "[]", None,
                 "[]", timestamp),
            )
            self._connection.execute(
                """INSERT INTO task_agent_revisions
                   (id,task_id,state,agent_md,evidence_refs_json,created_at_us)
                   VALUES (?,?,?,?,?,?)""",
                (agent_id, row_id, "production", agent_md, "[]", timestamp),
            )
            self._connection.execute(
                """UPDATE tasks SET current_state_revision_id=?,current_agent_revision_id=?
                   WHERE id=?""", (state_id, agent_id, row_id),
            )
        result = self.get_task(row_id)
        assert result is not None
        return result

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._lock:
            rows = self._connection.execute(
                """SELECT t.*,
                          COUNT(DISTINCT e.id) AS execution_count,
                          COUNT(DISTINCT k.id) AS knowledge_count,
                          COUNT(DISTINCT s.id) AS skill_count
                   FROM tasks t
                   LEFT JOIN task_executions e ON e.task_id=t.id
                   LEFT JOIN task_knowledge k ON k.task_id=t.id
                   LEFT JOIN task_skills s ON s.task_id=t.id
                   GROUP BY t.id ORDER BY t.created_at_us DESC,t.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        result = [self._decode_control_row(row) for row in rows]
        for task in result:
            task["self_evolve"] = bool(task["self_evolve"])
        return result

    def get_task(self, row_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._connection.execute("SELECT * FROM tasks WHERE id=?", (row_id,)).fetchone()
            if task is None:
                return None
            state = self._connection.execute(
                "SELECT * FROM task_state_revisions WHERE id=?",
                (task["current_state_revision_id"],),
            ).fetchone()
            agent = self._connection.execute(
                "SELECT * FROM task_agent_revisions WHERE id=?",
                (task["current_agent_revision_id"],),
            ).fetchone()
            executions = self._connection.execute(
                "SELECT * FROM task_executions WHERE task_id=? ORDER BY created_at_us,id",
                (row_id,),
            ).fetchall()
            knowledge = self._connection.execute(
                "SELECT * FROM task_knowledge WHERE task_id=? ORDER BY created_at_us,id",
                (row_id,),
            ).fetchall()
            skills = self._connection.execute(
                "SELECT * FROM task_skills WHERE task_id=? ORDER BY created_at_us,id",
                (row_id,),
            ).fetchall()
            agents = self._connection.execute(
                "SELECT * FROM task_agent_revisions WHERE task_id=? ORDER BY created_at_us,id",
                (row_id,),
            ).fetchall()
        result = self._decode_control_row(task)
        result["self_evolve"] = bool(result["self_evolve"])
        result["current_state"] = None if state is None else self._decode_control_row(state)
        result["current_agent"] = None if agent is None else self._decode_control_row(agent)
        result["executions"] = [self._decode_control_row(row) for row in executions]
        result["knowledge"] = [self._decode_control_row(row) for row in knowledge]
        result["skills"] = [self._decode_control_row(row) for row in skills]
        result["agent_revisions"] = [self._decode_control_row(row) for row in agents]
        return result

    def create_task_execution(
        self, task_id: str, *, harness: str, task_run_id: str | None = None,
        knowledge_manifest: Any = None, skill_manifest: Any = None,
        row_id: str | None = None,
    ) -> dict[str, Any]:
        harness = harness.strip()
        if not harness:
            raise ValueError("harness is required")
        row_id, timestamp = row_id or str(uuid4()), now_us()
        with self._transaction():
            task = self._validate_reference("tasks", task_id, "task_id")
            assert task is not None
            if task_run_id is None:
                task_run_id = str(uuid4())
                self._connection.execute(
                    """INSERT INTO task_runs
                       (id,task_type_id,task_id,goal,status,workspace_json,
                        created_at_us,last_updated_at_us)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (task_run_id, None, task_id, task["goal"], "running",
                     task["workspace_json"], timestamp, timestamp),
                )
            else:
                run = self._validate_reference("task_runs", task_run_id, "task_run_id")
                assert run is not None
                if run["task_id"] not in {None, task_id}:
                    raise ValueError("task run belongs to another task")
                if run["task_id"] is None:
                    self._connection.execute(
                        "UPDATE task_runs SET task_id=?,last_updated_at_us=? WHERE id=?",
                        (task_id, timestamp, task_run_id),
                    )
            if knowledge_manifest is None:
                knowledge_manifest = self._task_knowledge_manifest(task_id)
            if skill_manifest is None:
                skill_manifest = [
                    {
                        "id": row["id"], "name": row["name"],
                        "content": json.loads(row["content_json"]),
                        "digest": self._content_digest(json.loads(row["content_json"])),
                    }
                    for row in self._connection.execute(
                        """SELECT id,name,content_json FROM task_skills
                           WHERE task_id=? AND state='production'
                           ORDER BY created_at_us,id""", (task_id,),
                    ).fetchall()
                ]
            self._connection.execute(
                """INSERT INTO task_executions
                   (id,task_id,task_run_id,harness,state_revision_id,agent_revision_id,
                    knowledge_manifest_json,skill_manifest_json,status,evidence_refs_json,
                    created_at_us,last_updated_at_us)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row_id, task_id, task_run_id, harness, task["current_state_revision_id"],
                 task["current_agent_revision_id"], _json(knowledge_manifest, []),
                 _json(skill_manifest, []), "planned", "[]", timestamp, timestamp),
            )
            row = self._connection.execute(
                "SELECT * FROM task_executions WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def list_task_executions(self, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM task_executions WHERE task_id=?
                   ORDER BY created_at_us,id LIMIT ?""", (task_id, limit),
            ).fetchall()
        return [self._decode_control_row(row) for row in rows]

    def get_task_execution(self, row_id: str) -> dict[str, Any] | None:
        """Return the exact immutable context selected when an execution was created."""
        with self._lock:
            execution = self._connection.execute(
                "SELECT * FROM task_executions WHERE id=?", (row_id,),
            ).fetchone()
            if execution is None:
                return None
            task = self._connection.execute(
                "SELECT * FROM tasks WHERE id=?", (execution["task_id"],),
            ).fetchone()
            state = self._connection.execute(
                "SELECT * FROM task_state_revisions WHERE id=?",
                (execution["state_revision_id"],),
            ).fetchone()
            agent = self._connection.execute(
                "SELECT * FROM task_agent_revisions WHERE id=?",
                (execution["agent_revision_id"],),
            ).fetchone()
            knowledge = json.loads(execution["knowledge_manifest_json"])
            skills = json.loads(execution["skill_manifest_json"])
            for item in knowledge:
                if "content" in item:
                    continue
                claim = self._connection.execute(
                    "SELECT kind,content,source_refs_json FROM wiki_claims WHERE id=?",
                    (item.get("id"),),
                ).fetchone()
                if claim is not None:
                    item.update(
                        kind=claim["kind"], content=claim["content"],
                        source_refs=json.loads(claim["source_refs_json"]), source="wiki",
                    )
            for item in skills:
                if "content" in item:
                    continue
                skill = self._connection.execute(
                    "SELECT content_json FROM task_skills WHERE id=?", (item.get("id"),),
                ).fetchone()
                if skill is not None:
                    item["content"] = json.loads(skill["content_json"])
        return {
            "execution": self._decode_control_row(execution),
            "task": self._decode_control_row(task),
            "state": self._decode_control_row(state),
            "agent": self._decode_control_row(agent),
            "knowledge": knowledge,
            "skills": skills,
        }

    def set_task_execution_status(
        self, row_id: str, status: str, *, outcome: str | None = None,
        evidence_refs: Iterable[Any] | None = None, trusted: bool = False,
    ) -> dict[str, Any]:
        statuses = {"planned", "running", "waiting_user", "blocked", "completed", "failed", "cancelled"}
        if status not in statuses:
            raise ValueError(f"unsupported task execution status: {status}")
        if outcome not in {None, "pass", "fail", "unknown"}:
            raise ValueError(f"unsupported task execution outcome: {outcome}")
        refs = list(evidence_refs or ())
        if trusted and (outcome not in {"pass", "fail"} or not refs):
            raise ValueError("trusted execution requires a resolved outcome and evidence")
        timestamp = now_us()
        with self._transaction():
            current = self._validate_reference("task_executions", row_id, "task_execution_id")
            assert current is not None
            if current["trusted"]:
                raise ValueError("trusted task execution outcome is immutable")
            finished = timestamp if status in {"completed", "failed", "cancelled"} else None
            self._connection.execute(
                """UPDATE task_executions SET status=?,outcome=?,evidence_refs_json=?,
                   trusted=?,finished_at_us=?,last_updated_at_us=? WHERE id=?""",
                (status, outcome, _json(refs, []), int(trusted), finished, timestamp, row_id),
            )
            row = self._connection.execute(
                "SELECT * FROM task_executions WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def create_task_state_revision(
        self, task_id: str, *, state: Mapping[str, Any], evidence_refs: Iterable[Any],
        task_execution_id: str | None = None, audit: Mapping[str, Any] | None = None,
        row_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(state, Mapping):
            raise ValueError("state must be an object")
        refs, audit = list(evidence_refs), dict(audit or {})
        if not refs:
            raise ValueError("state revision requires evidence")
        row_id, timestamp = row_id or str(uuid4()), now_us()
        with self._transaction():
            task = self._validate_reference("tasks", task_id, "task_id")
            assert task is not None
            execution = self._validate_reference(
                "task_executions", task_execution_id, "task_execution_id"
            )
            if execution is not None and execution["task_id"] != task_id:
                raise ValueError("task execution belongs to another task")
            status = str(state.get("status") or task["status"])
            if state.get("requires_user") or state.get("waiting_for_user"):
                status = "waiting_user"
            self._validate_task_status(status)
            gates = tuple(bool(audit.get(key)) for key in ("complete", "clean", "aligned"))
            if status == "completed" and (not all(gates) or not refs):
                raise ValueError("completed state requires complete + clean + aligned audit evidence")
            self._connection.execute(
                """INSERT INTO task_state_revisions
                   (id,task_id,parent_revision_id,task_execution_id,status,completed_json,
                    incomplete_json,blockers_json,risks_json,untrusted_do_not_reuse_json,
                    next_action_json,evidence_refs_json,audit_complete,audit_clean,
                    audit_aligned,created_at_us)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row_id, task_id, task["current_state_revision_id"], task_execution_id,
                 status, _json(state.get("completed"), []),
                 _json(state.get("incomplete"), []), _json(state.get("blockers"), []),
                 _json(state.get("risks"), []),
                 _json(state.get("untrusted_do_not_reuse"), []),
                 None if state.get("next_action") is None else _json(state["next_action"], None),
                 _json(refs, []), *(int(value) for value in gates), timestamp),
            )
            self._connection.execute(
                """UPDATE tasks SET current_state_revision_id=?,status=?,last_updated_at_us=?
                   WHERE id=?""", (row_id, status, timestamp, task_id),
            )
            if execution is not None and status in {"waiting_user", "blocked", "completed", "cancelled"}:
                execution_status = status
                outcome = "pass" if status == "completed" else execution["outcome"]
                trusted = int(status == "completed")
                finished = timestamp if status in {"completed", "cancelled"} else None
                self._connection.execute(
                    """UPDATE task_executions SET status=?,outcome=?,evidence_refs_json=?,
                       trusted=?,finished_at_us=?,last_updated_at_us=? WHERE id=?""",
                    (execution_status, outcome, _json(refs, []), trusted, finished,
                     timestamp, task_execution_id),
                )
            row = self._connection.execute(
                "SELECT * FROM task_state_revisions WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def create_task_agent_revision(
        self, task_id: str, *, agent_md: str, state: str = "candidate",
        evidence_refs: Iterable[Any] | None = None, row_id: str | None = None,
    ) -> dict[str, Any]:
        if state == "production":
            raise ValueError("production agent revisions require explicit promotion")
        if state not in {"candidate", "rejected"}:
            raise ValueError(f"unsupported task agent revision state: {state}")
        row_id, timestamp = row_id or str(uuid4()), now_us()
        with self._transaction():
            task = self._validate_reference("tasks", task_id, "task_id")
            assert task is not None
            self._connection.execute(
                """INSERT INTO task_agent_revisions
                   (id,task_id,parent_revision_id,state,agent_md,evidence_refs_json,created_at_us)
                   VALUES (?,?,?,?,?,?,?)""",
                (row_id, task_id, task["current_agent_revision_id"], state, agent_md,
                 _json(list(evidence_refs or ()), []), timestamp),
            )
            row = self._connection.execute(
                "SELECT * FROM task_agent_revisions WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def promote_task_agent_revision(
        self, row_id: str, *, evidence_refs: Iterable[Any],
    ) -> dict[str, Any]:
        refs, timestamp = list(evidence_refs), now_us()
        if not refs:
            raise ValueError("agent promotion requires evidence")
        with self._transaction():
            row = self._validate_reference("task_agent_revisions", row_id, "agent_revision_id")
            assert row is not None
            if row["state"] != "candidate":
                raise ValueError("only candidate agent revisions can be promoted")
            self._connection.execute(
                "UPDATE task_agent_revisions SET state='production',evidence_refs_json=? WHERE id=?",
                (_json(refs, []), row_id),
            )
            self._connection.execute(
                """UPDATE tasks SET current_agent_revision_id=?,last_updated_at_us=?
                   WHERE id=?""", (row_id, timestamp, row["task_id"]),
            )
            row = self._connection.execute(
                "SELECT * FROM task_agent_revisions WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def list_task_agent_revisions(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM task_agent_revisions WHERE task_id=? ORDER BY created_at_us,id",
                (task_id,),
            ).fetchall()
        return [self._decode_control_row(row) for row in rows]

    def add_task_knowledge(
        self, task_id: str, *, kind: str, content: Any, execution_id: str,
        status: str = "proposed", source_refs: Iterable[Any] | None = None,
        row_id: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"proposed", "verified", "rejected", "promoted"}:
            raise ValueError(f"unsupported task knowledge status: {status}")
        if not kind.strip():
            raise ValueError("kind is required")
        row_id, timestamp = row_id or str(uuid4()), now_us()
        with self._transaction():
            self._validate_reference("tasks", task_id, "task_id")
            execution = self._validate_reference("task_executions", execution_id, "execution_id")
            assert execution is not None
            if execution["task_id"] != task_id:
                raise ValueError("execution belongs to another task")
            self._connection.execute(
                """INSERT INTO task_knowledge
                   (id,task_id,kind,content_json,execution_id,status,source_refs_json,created_at_us)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (row_id, task_id, kind.strip(), _json(content, None), execution_id, status,
                 _json(list(source_refs or ()), []), timestamp),
            )
            row = self._connection.execute("SELECT * FROM task_knowledge WHERE id=?", (row_id,)).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def list_task_knowledge(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM task_knowledge WHERE task_id=? ORDER BY created_at_us,id",
                (task_id,),
            ).fetchall()
        return [self._decode_control_row(row) for row in rows]

    def add_task_skill(
        self, task_id: str, *, name: str, content: Any,
        execution_refs: Iterable[str], state: str = "candidate",
        row_id: str | None = None,
    ) -> dict[str, Any]:
        if state == "production":
            raise ValueError("production skills require explicit promotion")
        if state not in {"candidate", "rejected"}:
            raise ValueError(f"unsupported task skill state: {state}")
        refs = list(dict.fromkeys(execution_refs))
        if not name.strip():
            raise ValueError("name is required")
        row_id, timestamp = row_id or str(uuid4()), now_us()
        with self._transaction():
            self._validate_reference("tasks", task_id, "task_id")
            executions = []
            for execution_id in refs:
                execution = self._validate_reference(
                    "task_executions", execution_id, "execution_id"
                )
                assert execution is not None
                if execution["task_id"] != task_id:
                    raise ValueError("execution belongs to another task")
                executions.append(execution)
            if state == "candidate" and (
                len(executions) < 3
                or any(not row["trusted"] or row["outcome"] not in {"pass", "fail"}
                       or not json.loads(row["evidence_refs_json"]) for row in executions)
            ):
                raise ValueError("skill candidate requires three distinct trusted executions with evidence")
            self._connection.execute(
                """INSERT INTO task_skills
                   (id,task_id,name,content_json,execution_refs_json,state,created_at_us)
                   VALUES (?,?,?,?,?,?,?)""",
                (row_id, task_id, name.strip(), _json(content, None), _json(refs, []),
                 state, timestamp),
            )
            row = self._connection.execute("SELECT * FROM task_skills WHERE id=?", (row_id,)).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def list_task_skills(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM task_skills WHERE task_id=? ORDER BY created_at_us,id",
                (task_id,),
            ).fetchall()
        return [self._decode_control_row(row) for row in rows]

    def promote_task_skill(self, row_id: str) -> dict[str, Any]:
        with self._transaction():
            row = self._validate_reference("task_skills", row_id, "skill_id")
            assert row is not None
            if row["state"] != "candidate":
                raise ValueError("only candidate skills can be promoted")
            execution_ids = json.loads(row["execution_refs_json"])
            executions = [self._validate_reference(
                "task_executions", execution_id, "execution_id"
            ) for execution_id in execution_ids]
            if len(executions) < 3 or any(
                not execution["trusted"] or execution["outcome"] not in {"pass", "fail"}
                or not json.loads(execution["evidence_refs_json"])
                for execution in executions
            ):
                raise ValueError("skill promotion requires three trusted executions with evidence")
            self._connection.execute(
                "UPDATE task_skills SET state='production' WHERE id=?", (row_id,),
            )
            row = self._connection.execute(
                "SELECT * FROM task_skills WHERE id=?", (row_id,),
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def create_wiki_page(
        self, *, title: str, category: str | None = None, content: str = "",
        row_id: str | None = None,
    ) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise ValueError("title is required")
        category = category or "domains"
        if category not in _WIKI_CATEGORIES:
            raise ValueError(f"unsupported wiki category: {category}")
        row_id, timestamp = row_id or str(uuid4()), now_us()
        digest = self._content_digest({"title": title, "category": category, "content": content})
        with self._transaction():
            self._connection.execute(
                """INSERT INTO wiki_pages
                   (id,title,category,content,revision,digest,created_at_us,last_updated_at_us)
                   VALUES (?,?,?,?,1,?,?,?)""",
                (row_id, title, category, content, digest, timestamp, timestamp),
            )
            row = self._connection.execute("SELECT * FROM wiki_pages WHERE id=?", (row_id,)).fetchone()
        assert row is not None
        return dict(row)

    def list_wiki_pages(self, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._lock:
            rows = self._connection.execute(
                """SELECT p.*,COUNT(c.id) AS claim_count FROM wiki_pages p
                   LEFT JOIN wiki_claims c ON c.page_id=p.id
                   GROUP BY p.id ORDER BY p.last_updated_at_us DESC,p.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_wiki_page(self, row_id: str) -> dict[str, Any] | None:
        with self._lock:
            page = self._connection.execute("SELECT * FROM wiki_pages WHERE id=?", (row_id,)).fetchone()
            if page is None:
                return None
            claims = self._connection.execute(
                "SELECT * FROM wiki_claims WHERE page_id=? ORDER BY created_at_us,id", (row_id,),
            ).fetchall()
            claim_ids = [row["id"] for row in claims]
            relations = [] if not claim_ids else self._connection.execute(
                f"""SELECT * FROM wiki_relations
                    WHERE source_claim_id IN ({','.join('?' for _ in claim_ids)})
                       OR target_claim_id IN ({','.join('?' for _ in claim_ids)})
                    ORDER BY created_at_us,id""",
                (*claim_ids, *claim_ids),
            ).fetchall()
        return {
            "page": dict(page),
            "claims": [self._decode_control_row(row) for row in claims],
            "relations": [dict(row) for row in relations],
        }

    def add_wiki_claim(
        self, page_id: str, *, content: str, kind: str = "fact",
        source_refs: Iterable[Any] | None = None, row_id: str | None = None,
    ) -> dict[str, Any]:
        content, kind = content.strip(), kind.strip()
        if not content:
            raise ValueError("content is required")
        if not kind:
            raise ValueError("kind is required")
        if kind not in _WIKI_CLAIM_KINDS:
            raise ValueError(f"unsupported wiki claim kind: {kind}")
        row_id, timestamp = row_id or str(uuid4()), now_us()
        refs = list(source_refs or ())
        digest = self._content_digest(
            {"page_id": page_id, "kind": kind, "content": content, "source_refs": refs}
        )
        with self._transaction():
            self._validate_reference("wiki_pages", page_id, "page_id")
            self._connection.execute(
                """INSERT INTO wiki_claims
                   (id,page_id,kind,content,source_refs_json,revision,digest,created_at_us)
                   VALUES (?,?,?,?,?,1,?,?)""",
                (row_id, page_id, kind, content, _json(refs, []), digest, timestamp),
            )
            row = self._connection.execute("SELECT * FROM wiki_claims WHERE id=?", (row_id,)).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def add_wiki_relation(
        self, *, source_claim_id: str, target_claim_id: str, relation: str,
        row_id: str | None = None,
    ) -> dict[str, Any]:
        relation = relation.strip()
        if not relation:
            raise ValueError("relation is required")
        if relation not in _WIKI_RELATIONS:
            raise ValueError(f"unsupported wiki relation: {relation}")
        row_id, timestamp = row_id or str(uuid4()), now_us()
        with self._transaction():
            self._validate_reference("wiki_claims", source_claim_id, "source_claim_id")
            self._validate_reference("wiki_claims", target_claim_id, "target_claim_id")
            self._connection.execute(
                """INSERT INTO wiki_relations
                   (id,source_claim_id,target_claim_id,relation,created_at_us)
                   VALUES (?,?,?,?,?)""",
                (row_id, source_claim_id, target_claim_id, relation, timestamp),
            )
            row = self._connection.execute("SELECT * FROM wiki_relations WHERE id=?", (row_id,)).fetchone()
        assert row is not None
        return dict(row)

    def get_wiki_graph(
        self, root_id: str | None = None, limit: int = 100,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._lock:
            if root_id is None:
                claims = self._connection.execute(
                    "SELECT * FROM wiki_claims ORDER BY created_at_us,id LIMIT ?", (limit + 1,),
                ).fetchall()
            else:
                claim = self._connection.execute(
                    "SELECT id FROM wiki_claims WHERE id=?", (root_id,),
                ).fetchone()
                page = self._connection.execute(
                    "SELECT id FROM wiki_pages WHERE id=?", (root_id,),
                ).fetchone()
                if claim is None and page is None:
                    raise ValueError(f"root_id does not exist: {root_id}")
                roots = [root_id] if claim is not None else [
                    row["id"] for row in self._connection.execute(
                        "SELECT id FROM wiki_claims WHERE page_id=?", (root_id,),
                    ).fetchall()
                ]
                if not roots:
                    claims = []
                else:
                    marks = ",".join("?" for _ in roots)
                    claims = self._connection.execute(
                        f"""SELECT DISTINCT c.* FROM wiki_claims c
                            LEFT JOIN wiki_relations r
                              ON (r.source_claim_id IN ({marks}) AND r.target_claim_id=c.id)
                              OR (r.target_claim_id IN ({marks}) AND r.source_claim_id=c.id)
                            WHERE c.id IN ({marks}) OR r.id IS NOT NULL
                            ORDER BY c.created_at_us,c.id LIMIT ?""",
                        (*roots, *roots, *roots, limit + 1),
                    ).fetchall()
            truncated = len(claims) > limit
            claims = claims[:limit]
            claim_ids = [row["id"] for row in claims]
            relations = [] if not claim_ids else self._connection.execute(
                f"""SELECT * FROM wiki_relations
                    WHERE source_claim_id IN ({','.join('?' for _ in claim_ids)})
                      AND target_claim_id IN ({','.join('?' for _ in claim_ids)})
                    ORDER BY created_at_us,id""",
                (*claim_ids, *claim_ids),
            ).fetchall()
            truncated = truncated or len(relations) > 200
            relations = relations[:200]
            page_ids = list(dict.fromkeys(row["page_id"] for row in claims))
            if root_id is not None and self._connection.execute(
                "SELECT 1 FROM wiki_pages WHERE id=?", (root_id,),
            ).fetchone() and root_id not in page_ids:
                page_ids.append(root_id)
            pages = [] if not page_ids else self._connection.execute(
                f"SELECT * FROM wiki_pages WHERE id IN ({','.join('?' for _ in page_ids)})",
                page_ids,
            ).fetchall()
        return {
            "pages": [dict(row) for row in pages],
            "claims": [self._decode_control_row(row) for row in claims],
            "relations": [dict(row) for row in relations],
            "truncated": truncated,
        }

    def bind_task_knowledge(
        self, task_id: str, claim_ids: Iterable[str],
    ) -> list[dict[str, Any]]:
        claim_ids = list(dict.fromkeys(claim_ids))
        timestamp = now_us()
        with self._transaction():
            self._validate_reference("tasks", task_id, "task_id")
            for claim_id in claim_ids:
                self._validate_reference("wiki_claims", claim_id, "claim_id")
            self._connection.execute("DELETE FROM task_knowledge_bindings WHERE task_id=?", (task_id,))
            self._connection.executemany(
                """INSERT INTO task_knowledge_bindings(task_id,claim_id,created_at_us)
                   VALUES (?,?,?)""",
                [(task_id, claim_id, timestamp) for claim_id in claim_ids],
            )
            return self._task_knowledge_manifest(task_id)

    def create_task_run(
        self, *, goal: str, task_type_id: str | None = None,
        workspace: Mapping[str, Any] | None = None, task_id: str | None = None,
        row_id: str | None = None,
    ) -> dict[str, Any]:
        goal = goal.strip()
        if not goal:
            raise ValueError("goal is required")
        row_id, timestamp = row_id or str(uuid4()), now_us()
        with self._transaction():
            self._validate_reference("task_types", task_type_id, "task_type_id")
            self._validate_reference("tasks", task_id, "task_id")
            self._connection.execute(
                """INSERT INTO task_runs
                   (id,task_type_id,task_id,goal,status,workspace_json,
                    created_at_us,last_updated_at_us)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (row_id, task_type_id, task_id, goal, "running", _json(workspace, {}),
                 timestamp, timestamp),
            )
            row = self._connection.execute(
                "SELECT * FROM task_runs WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def set_task_run_status(self, row_id: str, status: str) -> dict[str, Any]:
        if status not in STATUSES:
            raise ValueError(f"unsupported task run status: {status}")
        timestamp = now_us()
        with self._transaction():
            row = self._validate_reference("task_runs", row_id, "task_run_id")
            assert row is not None
            finished = timestamp if status != "running" else None
            self._connection.execute(
                """UPDATE task_runs SET status=?,finished_at_us=?,last_updated_at_us=?
                   WHERE id=?""",
                (_status_after(row["status"], status), finished, timestamp, row_id),
            )
            row = self._connection.execute(
                "SELECT * FROM task_runs WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def create_attempt(
        self, *, task_run_id: str | None = None, harness: str,
        task_execution_id: str | None = None, source_attempt_id: str | None = None,
        context_pack_id: str | None = None, profile_revision_id: str | None = None,
        launch_plan: Mapping[str, Any] | None = None, status: str = "planned",
        row_id: str | None = None,
    ) -> dict[str, Any]:
        harness = harness.strip()
        if not harness:
            raise ValueError("harness is required")
        if status not in {"planned", *STATUSES}:
            raise ValueError(f"unsupported attempt status: {status}")
        row_id, timestamp = row_id or str(uuid4()), now_us()
        with self._transaction():
            execution = self._validate_reference(
                "task_executions", task_execution_id, "task_execution_id"
            )
            if execution is not None:
                if task_run_id is not None and task_run_id != execution["task_run_id"]:
                    raise ValueError("task execution belongs to another task run")
                task_run_id = execution["task_run_id"]
            if task_run_id is None:
                raise ValueError("task_run_id or task_execution_id is required")
            task_run = self._validate_reference("task_runs", task_run_id, "task_run_id")
            assert task_run is not None
            if execution is not None and task_run["task_id"] != execution["task_id"]:
                raise ValueError("task execution belongs to another task")
            source = self._validate_reference("attempts", source_attempt_id, "source_attempt_id")
            pack = self._validate_reference("context_packs", context_pack_id, "context_pack_id")
            if profile_revision_id is None and task_run["task_type_id"] is not None:
                task_type = self._validate_reference(
                    "task_types", task_run["task_type_id"], "task_type_id"
                )
                profile_revision_id = task_type["production_profile_revision_id"]
            profile = self._validate_reference(
                "profile_revisions", profile_revision_id, "profile_revision_id"
            )
            for related, label in ((source, "source attempt"), (pack, "context pack")):
                if related is not None and related["task_run_id"] != task_run_id:
                    raise ValueError(f"{label} belongs to another task run")
            if profile is not None and task_run["task_type_id"] != profile["task_type_id"]:
                raise ValueError("profile revision belongs to another task type")
            started = timestamp if status == "running" else None
            finished = timestamp if status in _FINAL_STATUSES else None
            self._connection.execute(
                """INSERT INTO attempts
                   (id,task_run_id,task_execution_id,harness,source_attempt_id,context_pack_id,
                    profile_revision_id,status,launch_plan_json,started_at_us,finished_at_us,
                    created_at_us,last_updated_at_us)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row_id, task_run_id, task_execution_id, harness, source_attempt_id, context_pack_id,
                 profile_revision_id, status, _json(launch_plan, {}), started, finished,
                 timestamp, timestamp),
            )
            row = self._connection.execute(
                "SELECT * FROM attempts WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def set_attempt_status(self, row_id: str, status: str) -> dict[str, Any]:
        if status not in {"planned", *STATUSES}:
            raise ValueError(f"unsupported attempt status: {status}")
        timestamp = now_us()
        with self._transaction():
            current = self._validate_reference("attempts", row_id, "attempt_id")
            assert current is not None
            if current["status"] in _FINAL_STATUSES:
                status = current["status"]
            started = current["started_at_us"] or (timestamp if status == "running" else None)
            finished = current["finished_at_us"] or (
                timestamp if status in _FINAL_STATUSES else None
            )
            self._connection.execute(
                """UPDATE attempts SET status=?,started_at_us=?,finished_at_us=?,
                   last_updated_at_us=? WHERE id=?""",
                (status, started, finished, timestamp, row_id),
            )
            row = self._connection.execute(
                "SELECT * FROM attempts WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def register_context_pack(
        self, manifest: Mapping[str, Any], path: str | Path, *, detached: bool = False,
    ) -> dict[str, Any]:
        required = (
            "pack_id", "digest", "task_run_id", "schema_version",
            "capture_status", "capture_method",
        )
        for key in required:
            _required(manifest, key)
        owned_path = self._owned_path(path)
        timestamp = now_us()
        with self._transaction():
            task_run_id = str(manifest["task_run_id"])
            self._validate_reference("task_runs", task_run_id, "task_run_id")
            parent_id = manifest.get("parent_pack_id")
            source_id = manifest.get("source_attempt_id")
            profile_id = manifest.get("profile_revision_id")
            if detached:
                parent_id = parent_id if self._connection.execute(
                    "SELECT 1 FROM context_packs WHERE id=?", (parent_id,)
                ).fetchone() else None
                source_id = source_id if self._connection.execute(
                    "SELECT 1 FROM attempts WHERE id=?", (source_id,)
                ).fetchone() else None
                profile_id = profile_id if self._connection.execute(
                    "SELECT 1 FROM profile_revisions WHERE id=?", (profile_id,)
                ).fetchone() else None
            source = self._validate_reference("attempts", source_id, "source_attempt_id")
            if source is not None and source["task_run_id"] != task_run_id:
                raise ValueError("source attempt belongs to another task run")
            self._validate_reference("context_packs", parent_id, "parent_pack_id")
            self._validate_reference("profile_revisions", profile_id, "profile_revision_id")
            self._connection.execute(
                """INSERT INTO context_packs
                   (id,digest,parent_pack_id,task_run_id,source_attempt_id,
                    profile_revision_id,schema_version,capture_status,capture_method,
                    path,manifest_json,created_at_us)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(manifest["pack_id"]), str(manifest["digest"]), parent_id,
                 task_run_id, source_id, profile_id,
                 int(manifest["schema_version"]), str(manifest["capture_status"]),
                 str(manifest["capture_method"]), str(owned_path),
                 _json(manifest, {}), timestamp),
            )
            row = self._connection.execute(
                "SELECT * FROM context_packs WHERE id=?", (manifest["pack_id"],)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def create_handoff(
        self, *, task_run_id: str, context_pack_id: str, destination_harness: str,
        compatibility: Mapping[str, Any], source_attempt_id: str | None = None,
        destination_attempt_id: str | None = None, reason: str | None = None,
        status: str = "planned", row_id: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"planned", "running", "ok", "error", "blocked"}:
            raise ValueError(f"unsupported handoff status: {status}")
        row_id, timestamp = row_id or str(uuid4()), now_us()
        with self._transaction():
            self._validate_reference("task_runs", task_run_id, "task_run_id")
            for table, value, label in (
                ("context_packs", context_pack_id, "context_pack_id"),
                ("attempts", source_attempt_id, "source_attempt_id"),
                ("attempts", destination_attempt_id, "destination_attempt_id"),
            ):
                row = self._validate_reference(table, value, label)
                if row is not None and row["task_run_id"] != task_run_id:
                    raise ValueError(f"{label} belongs to another task run")
            self._connection.execute(
                """INSERT INTO handoffs
                   (id,task_run_id,source_attempt_id,destination_attempt_id,
                    context_pack_id,destination_harness,compatibility_json,reason,status,
                    created_at_us,last_updated_at_us)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (row_id, task_run_id, source_attempt_id, destination_attempt_id,
                 context_pack_id, destination_harness, _json(compatibility, {}), reason,
                 status, timestamp, timestamp),
            )
            row = self._connection.execute(
                "SELECT * FROM handoffs WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def get_task_run(self, row_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._connection.execute(
                "SELECT * FROM task_runs WHERE id=?", (row_id,)
            ).fetchone()
            if run is None:
                return None
            attempts = self._connection.execute(
                "SELECT * FROM attempts WHERE task_run_id=? ORDER BY created_at_us,id",
                (row_id,),
            ).fetchall()
            packs = self._connection.execute(
                "SELECT * FROM context_packs WHERE task_run_id=? ORDER BY created_at_us,id",
                (row_id,),
            ).fetchall()
            handoffs = self._connection.execute(
                "SELECT * FROM handoffs WHERE task_run_id=? ORDER BY created_at_us,id",
                (row_id,),
            ).fetchall()
            traces = self._connection.execute(
                """SELECT t.id,t.attempt_id,t.name,t.harness,t.status,t.start_time_us
                   FROM traces t JOIN attempts a ON a.id=t.attempt_id
                   WHERE a.task_run_id=? ORDER BY t.start_time_us,t.id""",
                (row_id,),
            ).fetchall()
        return {
            "task_run": self._decode_control_row(run),
            "attempts": [self._decode_control_row(row) for row in attempts],
            "packs": [self._decode_control_row(row) for row in packs],
            "handoffs": [self._decode_control_row(row) for row in handoffs],
            "traces": [dict(row) for row in traces],
        }

    def list_task_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._lock:
            rows = self._connection.execute(
                """SELECT r.*,tt.name AS task_type_name,
                          COUNT(DISTINCT a.id) AS attempt_count,
                          COUNT(DISTINCT p.id) AS pack_count,
                          (SELECT harness FROM attempts latest
                           WHERE latest.task_run_id=r.id
                           ORDER BY latest.created_at_us DESC,latest.id DESC LIMIT 1)
                          AS runtime
                   FROM task_runs r LEFT JOIN task_types tt ON tt.id=r.task_type_id
                   LEFT JOIN attempts a ON a.task_run_id=r.id
                   LEFT JOIN context_packs p ON p.task_run_id=r.id
                   GROUP BY r.id ORDER BY r.created_at_us DESC,r.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._decode_control_row(row) for row in rows]

    def get_context_pack(self, row_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM context_packs WHERE id=?", (row_id,)
            ).fetchone()
        return None if row is None else self._decode_control_row(row)

    def list_context_packs(self, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM context_packs ORDER BY created_at_us DESC,id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._decode_control_row(row) for row in rows]

    def get_attempt(self, row_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM attempts WHERE id=?", (row_id,)
            ).fetchone()
        return None if row is None else self._decode_control_row(row)

    def check_attempt_trace_integrity(self, row_id: str) -> dict[str, Any]:
        with self._lock:
            attempt = self._connection.execute(
                "SELECT * FROM attempts WHERE id=?", (row_id,)
            ).fetchone()
            if attempt is None:
                raise ValueError(f"attempt_id does not exist: {row_id}")
            traces = self._connection.execute(
                "SELECT * FROM traces WHERE attempt_id=?", (row_id,)
            ).fetchall()
            llm_count = self._connection.execute(
                """SELECT COUNT(*) FROM spans s JOIN traces t ON t.id=s.trace_id
                   WHERE t.attempt_id=? AND s.type='llm'""", (row_id,)
            ).fetchone()[0]
            duplicates = self._connection.execute(
                """SELECT s.trace_id,s.external_id,COUNT(*) AS n FROM spans s
                   JOIN traces t ON t.id=s.trace_id
                   WHERE t.attempt_id=? AND s.type='llm'
                   GROUP BY s.trace_id,s.external_id HAVING COUNT(*)>1""", (row_id,)
            ).fetchall()
        reasons = []
        if not traces:
            reasons.append("no Trace is linked to the evaluation Attempt")
        if llm_count == 0:
            reasons.append("no authoritative LLM Span is linked to the evaluation Attempt")
        for trace in traces:
            if trace["context_pack_id"] != attempt["context_pack_id"]:
                reasons.append(f"Trace {trace['id']} has the wrong Context Pack")
            if trace["profile_revision_id"] != attempt["profile_revision_id"]:
                reasons.append(f"Trace {trace['id']} has the wrong Profile Revision")
        if duplicates:
            reasons.append("duplicate authoritative LLM Span identities were found")
        return {"passed": not reasons, "trace_count": len(traces),
                "llm_span_count": llm_count, "reasons": reasons}

    def get_attempt_evidence(self, row_id: str, limit: int = 20) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._lock:
            self._validate_reference("attempts", row_id, "attempt_id")
            rows = self._connection.execute(
                """SELECT * FROM traces WHERE attempt_id=?
                   ORDER BY start_time_us DESC,id DESC LIMIT ?""", (row_id, limit),
            ).fetchall()
        traces = [self._decode_entity(row) for row in reversed(rows)]
        conversation = []
        for trace in traces:
            if trace.get("input") is not None:
                conversation.append({"role": "user", "content": trace["input"],
                                     "trace_id": trace["id"]})
            if trace.get("output") is not None:
                conversation.append({"role": "assistant", "content": trace["output"],
                                     "trace_id": trace["id"]})
        return {
            "trace_ids": [trace["id"] for trace in traces],
            "completed_turns": sum(trace["status"] == "ok" for trace in traces),
            "conversation": conversation,
            "last_output": next(
                (trace["output"] for trace in reversed(traces) if trace.get("output") is not None),
                None,
            ),
        }

    def create_profile_revision(
        self, *, task_type_id: str, digest: str, path: str | Path,
        parent_revision_id: str | None = None, state: str = "draft",
        change_dimension: str | None = None, metadata: Mapping[str, Any] | None = None,
        row_id: str | None = None,
    ) -> dict[str, Any]:
        states = {"draft", "evaluating", "recommended", "production", "rejected", "rolled_back"}
        if state not in states:
            raise ValueError(f"unsupported profile revision state: {state}")
        owned_path = self._owned_path(path)
        row_id, timestamp = row_id or str(uuid4()), now_us()
        with self._transaction():
            task_type = self._validate_reference("task_types", task_type_id, "task_type_id")
            parent = self._validate_reference(
                "profile_revisions", parent_revision_id, "parent_revision_id"
            )
            if parent is not None and parent["task_type_id"] != task_type_id:
                raise ValueError("parent revision belongs to another task type")
            if state == "production" and task_type["production_profile_revision_id"] is not None:
                raise ValueError("task type already has a production profile")
            self._connection.execute(
                """INSERT INTO profile_revisions
                   (id,task_type_id,parent_revision_id,digest,state,change_dimension,
                    path,metadata_json,created_at_us,last_updated_at_us)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (row_id, task_type_id, parent_revision_id, digest, state,
                 change_dimension, str(owned_path), _json(metadata, {}), timestamp, timestamp),
            )
            if state == "production":
                self._connection.execute(
                    """UPDATE task_types SET production_profile_revision_id=?,last_updated_at_us=?
                       WHERE id=?""", (row_id, timestamp, task_type_id),
                )
            row = self._connection.execute(
                "SELECT * FROM profile_revisions WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def get_profile_revision(self, row_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM profile_revisions WHERE id=?", (row_id,)
            ).fetchone()
        return None if row is None else self._decode_control_row(row)

    def find_profile_revision_by_digest(self, digest: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM profile_revisions WHERE digest=?", (digest,)
            ).fetchone()
        return None if row is None else self._decode_control_row(row)

    def resolve_outcome(
        self, *, task_run_id: str, outcome: str, attempt_id: str | None = None,
        classification: str | None = None, score: float | None = None,
        evidence: Mapping[str, Any] | None = None, frozen: bool = False,
        row_id: str | None = None,
    ) -> dict[str, Any]:
        if outcome not in {"pass", "fail", "unknown"}:
            raise ValueError(f"unsupported outcome: {outcome}")
        if score is not None and (isinstance(score, bool) or not math.isfinite(score)):
            raise ValueError("score must be finite")
        row_id, timestamp = row_id or str(uuid4()), now_us()
        with self._transaction():
            self._validate_reference("task_runs", task_run_id, "task_run_id")
            attempt = self._validate_reference("attempts", attempt_id, "attempt_id")
            if attempt is not None and attempt["task_run_id"] != task_run_id:
                raise ValueError("attempt belongs to another task run")
            self._connection.execute(
                """INSERT INTO outcome_resolutions
                   (id,task_run_id,attempt_id,outcome,classification,score,evidence_json,
                    frozen,created_at_us,last_updated_at_us)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (row_id, task_run_id, attempt_id, outcome, classification, score,
                 _json(evidence, {}), int(frozen), timestamp, timestamp),
            )
            row = self._connection.execute(
                "SELECT * FROM outcome_resolutions WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def freeze_regression_case(
        self, *, task_type_id: str, pack_id: str, outcome_resolution_id: str,
        digest: str, protected: bool = False, policy: Mapping[str, Any] | None = None,
        row_id: str | None = None,
    ) -> dict[str, Any]:
        row_id, timestamp = row_id or str(uuid4()), now_us()
        with self._transaction():
            self._validate_reference("task_types", task_type_id, "task_type_id")
            pack = self._validate_reference("context_packs", pack_id, "pack_id")
            outcome = self._validate_reference(
                "outcome_resolutions", outcome_resolution_id, "outcome_resolution_id"
            )
            run = self._validate_reference("task_runs", pack["task_run_id"], "task_run_id")
            if run["task_type_id"] != task_type_id or outcome["task_run_id"] != run["id"]:
                raise ValueError("regression case facts do not share one task type/run")
            if outcome["outcome"] == "unknown":
                raise ValueError("an unknown outcome cannot become a regression case")
            self._connection.execute(
                "UPDATE outcome_resolutions SET frozen=1,last_updated_at_us=? WHERE id=?",
                (timestamp, outcome_resolution_id),
            )
            self._connection.execute(
                """INSERT INTO regression_cases
                   (id,task_type_id,pack_id,outcome_resolution_id,digest,protected,
                    policy_json,created_at_us) VALUES (?,?,?,?,?,?,?,?)""",
                (row_id, task_type_id, pack_id, outcome_resolution_id, digest,
                 int(protected), _json(policy, {}), timestamp),
            )
            row = self._connection.execute(
                "SELECT * FROM regression_cases WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def create_evaluation_run(
        self, *, task_type_id: str, reference_revision_id: str,
        candidate_revision_id: str, row_id: str | None = None,
    ) -> dict[str, Any]:
        row_id, timestamp = row_id or str(uuid4()), now_us()
        with self._transaction():
            self._validate_reference("task_types", task_type_id, "task_type_id")
            reference = self._validate_reference(
                "profile_revisions", reference_revision_id, "reference_revision_id"
            )
            candidate = self._validate_reference(
                "profile_revisions", candidate_revision_id, "candidate_revision_id"
            )
            if reference["task_type_id"] != task_type_id or candidate["task_type_id"] != task_type_id:
                raise ValueError("evaluation revisions belong to another task type")
            if candidate["state"] != "draft":
                raise ValueError("candidate revision must be draft")
            self._connection.execute(
                "UPDATE profile_revisions SET state='evaluating',last_updated_at_us=? WHERE id=?",
                (timestamp, candidate_revision_id),
            )
            self._connection.execute(
                """INSERT INTO evaluation_runs
                   (id,task_type_id,reference_revision_id,candidate_revision_id,status,
                    created_at_us,last_updated_at_us) VALUES (?,?,?,?,?,?,?)""",
                (row_id, task_type_id, reference_revision_id, candidate_revision_id,
                 "running", timestamp, timestamp),
            )
            row = self._connection.execute(
                "SELECT * FROM evaluation_runs WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def add_evaluation_result(
        self, *, evaluation_run_id: str, regression_case_id: str, harness: str,
        variant: str, outcome: str, score: float, cost_usd_micros: int = 0,
        latency_us: int = 0, attempt_id: str | None = None,
        evidence: Mapping[str, Any] | None = None, row_id: str | None = None,
    ) -> dict[str, Any]:
        if variant not in {"reference", "candidate"} or outcome not in {"pass", "fail", "error"}:
            raise ValueError("invalid evaluation variant or outcome")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError("score must be finite")
        cost_usd_micros = _integer(cost_usd_micros, "cost_usd_micros", minimum=0)
        latency_us = _integer(latency_us, "latency_us", minimum=0)
        row_id, timestamp = row_id or str(uuid4()), now_us()
        with self._transaction():
            run = self._validate_reference("evaluation_runs", evaluation_run_id, "evaluation_run_id")
            case = self._validate_reference("regression_cases", regression_case_id, "regression_case_id")
            self._validate_reference("attempts", attempt_id, "attempt_id")
            if run["status"] != "running" or case["task_type_id"] != run["task_type_id"]:
                raise ValueError("evaluation is not running or case belongs to another task type")
            self._connection.execute(
                """INSERT INTO evaluation_results
                   (id,evaluation_run_id,regression_case_id,harness,variant,attempt_id,
                    outcome,score,cost_usd_micros,latency_us,evidence_json,created_at_us)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row_id, evaluation_run_id, regression_case_id, harness, variant,
                 attempt_id, outcome, float(score), cost_usd_micros, latency_us,
                 _json(evidence, {}), timestamp),
            )
            row = self._connection.execute(
                "SELECT * FROM evaluation_results WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def add_evaluation_trial(
        self, *, evaluation_run_id: str, regression_case_id: str, harness: str,
        variant: str, run_index: int, attempt_id: str, outcome: str, score: float,
        cost_usd_micros: int = 0, latency_us: int = 0,
        evidence: Mapping[str, Any] | None = None, row_id: str | None = None,
    ) -> dict[str, Any]:
        if variant not in {"reference", "candidate"} or outcome not in {"pass", "fail", "error"}:
            raise ValueError("invalid evaluation variant or outcome")
        run_index = _integer(run_index, "run_index", minimum=1)
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError("score must be finite")
        cost_usd_micros = _integer(cost_usd_micros, "cost_usd_micros", minimum=0)
        latency_us = _integer(latency_us, "latency_us", minimum=0)
        row_id, timestamp = row_id or str(uuid4()), now_us()
        with self._transaction():
            run = self._validate_reference("evaluation_runs", evaluation_run_id, "evaluation_run_id")
            case = self._validate_reference("regression_cases", regression_case_id, "regression_case_id")
            attempt = self._validate_reference("attempts", attempt_id, "attempt_id")
            pack = self._validate_reference("context_packs", case["pack_id"], "pack_id")
            expected_revision = (
                run["reference_revision_id"] if variant == "reference"
                else run["candidate_revision_id"]
            )
            if run["status"] != "running" or case["task_type_id"] != run["task_type_id"]:
                raise ValueError("evaluation is not running or case belongs to another task type")
            if (
                attempt["task_run_id"] != pack["task_run_id"]
                or attempt["harness"] != harness
                or attempt["context_pack_id"] != case["pack_id"]
                or attempt["profile_revision_id"] != expected_revision
            ):
                raise ValueError("evaluation Attempt does not match its Case/Harness/Profile")
            self._connection.execute(
                """INSERT INTO evaluation_trials
                   (id,evaluation_run_id,regression_case_id,harness,variant,run_index,
                    attempt_id,outcome,score,cost_usd_micros,latency_us,evidence_json,created_at_us)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row_id, evaluation_run_id, regression_case_id, harness, variant, run_index,
                 attempt_id, outcome, float(score), cost_usd_micros, latency_us,
                 _json(evidence, {}), timestamp),
            )
            row = self._connection.execute(
                "SELECT * FROM evaluation_trials WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def finish_evaluation(
        self, row_id: str, *, passed: bool, gates: Mapping[str, Any], error: bool = False,
    ) -> dict[str, Any]:
        timestamp = now_us()
        status = "error" if error else "passed" if passed else "failed"
        with self._transaction():
            run = self._validate_reference("evaluation_runs", row_id, "evaluation_run_id")
            if run["status"] != "running":
                raise ValueError("evaluation run is already terminal")
            self._connection.execute(
                """UPDATE evaluation_runs SET status=?,gates_json=?,finished_at_us=?,
                   last_updated_at_us=? WHERE id=?""",
                (status, _json(gates, {}), timestamp, timestamp, row_id),
            )
            candidate_state = "recommended" if passed else "rejected"
            self._connection.execute(
                "UPDATE profile_revisions SET state=?,last_updated_at_us=? WHERE id=?",
                (candidate_state, timestamp, run["candidate_revision_id"]),
            )
            row = self._connection.execute(
                "SELECT * FROM evaluation_runs WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        return self._decode_control_row(row)

    def promote_profile(self, revision_id: str) -> dict[str, Any]:
        timestamp = now_us()
        with self._transaction():
            revision = self._validate_reference(
                "profile_revisions", revision_id, "profile_revision_id"
            )
            if revision["state"] != "recommended":
                raise ValueError("only a recommended revision can be promoted")
            task_type = self._validate_reference(
                "task_types", revision["task_type_id"], "task_type_id"
            )
            previous = task_type["production_profile_revision_id"]
            if previous:
                self._connection.execute(
                    "UPDATE profile_revisions SET state='rolled_back',last_updated_at_us=? WHERE id=?",
                    (timestamp, previous),
                )
            self._connection.execute(
                "UPDATE profile_revisions SET state='production',last_updated_at_us=? WHERE id=?",
                (timestamp, revision_id),
            )
            self._connection.execute(
                """UPDATE task_types SET production_profile_revision_id=?,last_updated_at_us=?
                   WHERE id=?""", (revision_id, timestamp, revision["task_type_id"]),
            )
            promotion_id = str(uuid4())
            self._connection.execute(
                """INSERT INTO promotions
                   (id,task_type_id,from_revision_id,to_revision_id,action,created_at_us)
                   VALUES (?,?,?,?,?,?)""",
                (promotion_id, revision["task_type_id"], previous, revision_id,
                 "promote", timestamp),
            )
        return {"id": promotion_id, "from_revision_id": previous, "to_revision_id": revision_id}

    def rollback_task_type(self, task_type_id: str) -> dict[str, Any]:
        timestamp = now_us()
        with self._transaction():
            task_type = self._validate_reference("task_types", task_type_id, "task_type_id")
            current = task_type["production_profile_revision_id"]
            if current is None:
                raise ValueError("task type has no production profile")
            promotion = self._connection.execute(
                """SELECT * FROM promotions WHERE task_type_id=? AND action='promote'
                   AND to_revision_id=? AND from_revision_id IS NOT NULL
                   ORDER BY created_at_us DESC,id DESC LIMIT 1""",
                (task_type_id, current),
            ).fetchone()
            if promotion is None:
                raise ValueError("no previous production revision to roll back to")
            target = promotion["from_revision_id"]
            self._connection.execute(
                "UPDATE profile_revisions SET state='rolled_back',last_updated_at_us=? WHERE id=?",
                (timestamp, current),
            )
            self._connection.execute(
                "UPDATE profile_revisions SET state='production',last_updated_at_us=? WHERE id=?",
                (timestamp, target),
            )
            self._connection.execute(
                """UPDATE task_types SET production_profile_revision_id=?,last_updated_at_us=?
                   WHERE id=?""", (target, timestamp, task_type_id),
            )
            rollback_id = str(uuid4())
            self._connection.execute(
                """INSERT INTO promotions
                   (id,task_type_id,from_revision_id,to_revision_id,action,created_at_us)
                   VALUES (?,?,?,?,?,?)""",
                (rollback_id, task_type_id, current, target, "rollback", timestamp),
            )
        return {"id": rollback_id, "from_revision_id": current, "to_revision_id": target}

    def get_evolution(self, task_type: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._connection.execute(
                "SELECT * FROM task_types WHERE id=? OR name=?", (task_type, task_type)
            ).fetchone()
            if task is None:
                return None
            revisions = self._connection.execute(
                """SELECT * FROM profile_revisions WHERE task_type_id=?
                   ORDER BY created_at_us DESC,id DESC""", (task["id"],)
            ).fetchall()
            cases = self._connection.execute(
                """SELECT c.*,o.outcome,o.classification,o.score FROM regression_cases c
                   JOIN outcome_resolutions o ON o.id=c.outcome_resolution_id
                   WHERE c.task_type_id=? ORDER BY c.created_at_us DESC,c.id DESC""",
                (task["id"],),
            ).fetchall()
            evaluations = self._connection.execute(
                """SELECT * FROM evaluation_runs WHERE task_type_id=?
                   ORDER BY created_at_us DESC,id DESC""", (task["id"],),
            ).fetchall()
            results = []
            trials = []
            if evaluations:
                results = self._connection.execute(
                    """SELECT * FROM evaluation_results WHERE evaluation_run_id=?
                       ORDER BY regression_case_id,harness,variant""",
                    (evaluations[0]["id"],),
                ).fetchall()
                trials = self._connection.execute(
                    """SELECT * FROM evaluation_trials WHERE evaluation_run_id=?
                       ORDER BY regression_case_id,harness,variant,run_index""",
                    (evaluations[0]["id"],),
                ).fetchall()
        return {
            "task_type": self._decode_control_row(task),
            "revisions": [self._decode_control_row(row) for row in revisions],
            "regression_cases": [self._decode_control_row(row) for row in cases],
            "evaluations": [self._decode_control_row(row) for row in evaluations],
            "latest_results": [self._decode_control_row(row) for row in results],
            "latest_trials": [self._decode_control_row(row) for row in trials],
        }

    def _upsert_project(self, body: Mapping[str, Any]) -> dict[str, Any]:
        project_id, name = str(_required(body, "id")), str(_required(body, "name"))
        by_id = self._connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        by_name = self._connection.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
        if by_id is not None and by_name is not None and by_id["id"] != by_name["id"]:
            raise ValueError("project id and name identify different records")
        existing = by_id or by_name
        timestamp = now_us()
        if existing is None:
            self._connection.execute(
                "INSERT INTO projects(id,name,created_at_us,last_updated_at_us) VALUES (?,?,?,?)",
                (project_id, name, timestamp, timestamp),
            )
            return {"id": project_id, "created": True, "updated": False}
        if existing["id"] != project_id and existing["name"] != name:
            raise ValueError("project id and name identify different records")
        if existing["name"] == name:
            return {"id": existing["id"], "created": False, "updated": False}
        self._connection.execute(
            "UPDATE projects SET name=?,last_updated_at_us=? WHERE id=?",
            (name, timestamp, existing["id"]),
        )
        return {"id": existing["id"], "created": False, "updated": True}

    def upsert_thread(self, body: Mapping[str, Any]) -> dict[str, Any]:
        with self._transaction():
            return self._upsert_thread(body)

    def _upsert_thread(self, body: Mapping[str, Any]) -> dict[str, Any]:
        row_id = str(_required(body, "id"))
        project_id = str(body.get("project_id") or "default")
        harness = str(_required(body, "harness"))
        external_id = str(_required(body, "external_id"))
        status = str(body.get("status") or "running")
        if status not in STATUSES:
            raise ValueError(f"unsupported thread status: {status}")
        start = _integer(_required(body, "start_time_us"), "start_time_us", minimum=0)
        end = body.get("end_time_us")
        if end is not None:
            end = _integer(end, "end_time_us", minimum=start)
        metadata = (
            _json(_sanitize_metadata(body.get("metadata")), {})
            if "metadata" in body else None
        )
        attempt_id = body.get("attempt_id")
        self._validate_reference("attempts", attempt_id, "attempt_id")
        existing = self._identity_row(
            "threads", row_id,
            "project_id=? AND harness=? AND external_id=?",
            (project_id, harness, external_id),
        )
        timestamp = now_us()
        if status != "running" and end is None:
            end = max(start, timestamp)
        if existing is None:
            self._connection.execute(
                """INSERT INTO threads
                   (id,project_id,harness,external_id,name,status,start_time_us,end_time_us,
                    metadata_json,attempt_id,created_at_us,last_updated_at_us)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row_id, project_id, harness, external_id, body.get("name"), status,
                 start, end, metadata or "{}", attempt_id, timestamp, timestamp),
            )
            return {"id": row_id, "created": True, "updated": False}
        self._assert_identity(existing, project_id=project_id, harness=harness, external_id=external_id)
        merged = {
            "name": body.get("name") if body.get("name") is not None else existing["name"],
            "status": _status_after(existing["status"], status),
            "start_time_us": min(existing["start_time_us"], start),
            "end_time_us": end if end is not None else existing["end_time_us"],
            "metadata_json": metadata if metadata is not None else existing["metadata_json"],
            "attempt_id": self._assignment(existing, "attempt_id", attempt_id),
        }
        result = self._update_changed("threads", existing, merged, timestamp)
        if existing["status"] == "running" and merged["status"] != "running":
            closed_at = merged["end_time_us"] or timestamp
            self._abandon_running_children(existing["id"], closed_at, timestamp)
        return result

    def _abandon_running_children(
        self, thread_id: str, closed_at_us: int, updated_at_us: int
    ) -> None:
        self._connection.execute(
            """UPDATE spans SET status='abandoned',
                   end_time_us=COALESCE(end_time_us,MAX(start_time_us,?)),
                   source_updated_at_us=MAX(source_updated_at_us,?),last_updated_at_us=?
               WHERE status='running' AND trace_id IN
                   (SELECT id FROM traces WHERE thread_id=?)""",
            (closed_at_us, closed_at_us, updated_at_us, thread_id),
        )
        self._connection.execute(
            """UPDATE traces SET status='abandoned',
                   end_time_us=COALESCE(end_time_us,MAX(start_time_us,?)),
                   source_updated_at_us=MAX(source_updated_at_us,?),last_updated_at_us=?
               WHERE thread_id=? AND status='running'""",
            (closed_at_us, closed_at_us, updated_at_us, thread_id),
        )

    def upsert_trace(self, body: Mapping[str, Any]) -> dict[str, Any]:
        with self._transaction():
            return self._upsert_trace(body)

    def _upsert_trace(self, body: Mapping[str, Any]) -> dict[str, Any]:
        row_id = str(_required(body, "id"))
        thread_id = str(_required(body, "thread_id"))
        thread = self._connection.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if thread is None:
            raise ValueError(f"thread does not exist: {thread_id}")
        project_id = str(body.get("project_id") or thread["project_id"])
        harness = str(_required(body, "harness"))
        if project_id != thread["project_id"] or harness != thread["harness"]:
            raise ValueError("trace project/harness must match its thread")
        source, external_id = str(_required(body, "source")), str(_required(body, "external_id"))
        attempt_id = body.get("attempt_id") or thread["attempt_id"]
        context_pack_id = body.get("context_pack_id")
        profile_revision_id = body.get("profile_revision_id")
        if attempt_id is not None:
            attempt = self._validate_reference("attempts", attempt_id, "attempt_id")
            if context_pack_id is None:
                context_pack_id = attempt["context_pack_id"]
            if profile_revision_id is None:
                profile_revision_id = attempt["profile_revision_id"]
        self._validate_reference("context_packs", context_pack_id, "context_pack_id")
        self._validate_reference(
            "profile_revisions", profile_revision_id, "profile_revision_id"
        )
        incoming = self._trace_values(body)
        existing = self._identity_row(
            "traces", row_id,
            "project_id=? AND harness=? AND external_id=?",
            (project_id, harness, external_id),
        )
        timestamp = now_us()
        if existing is None:
            incoming["metadata_json"] = incoming["metadata_json"] or "{}"
            incoming["tags_json"] = incoming["tags_json"] or "[]"
            columns = (
                "id", "project_id", "thread_id", "harness", "source", "external_id",
                "attempt_id", "context_pack_id", "profile_revision_id",
                *incoming.keys(), "created_at_us", "last_updated_at_us",
            )
            values = (row_id, project_id, thread_id, harness, source, external_id,
                      attempt_id, context_pack_id, profile_revision_id,
                      *incoming.values(), timestamp, timestamp)
            self._insert("traces", columns, values)
            return {"id": row_id, "created": True, "updated": False}
        self._assert_identity(
            existing, project_id=project_id, thread_id=thread_id, harness=harness,
            source=source, external_id=external_id,
        )
        merged = self._merge_snapshot(existing, incoming)
        merged.update({
            "attempt_id": self._assignment(existing, "attempt_id", attempt_id),
            "context_pack_id": self._assignment(
                existing, "context_pack_id", context_pack_id
            ),
            "profile_revision_id": self._assignment(
                existing, "profile_revision_id", profile_revision_id
            ),
        })
        return self._update_changed("traces", existing, merged, timestamp)

    def _trace_values(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._snapshot_values(body, entity="trace", extra={})

    def upsert_span(self, body: Mapping[str, Any]) -> dict[str, Any]:
        with self._transaction():
            return self._upsert_span(body)

    def _upsert_span(self, body: Mapping[str, Any]) -> dict[str, Any]:
        row_id = str(_required(body, "id"))
        trace_id = str(_required(body, "trace_id"))
        if self._connection.execute("SELECT 1 FROM traces WHERE id=?", (trace_id,)).fetchone() is None:
            raise ValueError(f"trace does not exist: {trace_id}")
        source, external_id = str(_required(body, "source")), str(_required(body, "external_id"))
        parent_id = body.get("parent_span_id")
        self._validate_parent(trace_id, parent_id, row_id)
        span_type = str(body.get("type") or "general")
        if span_type not in SPAN_TYPES:
            raise ValueError(f"unsupported span type: {span_type}")
        token_fields = (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "reasoning_tokens", "cost_usd_micros", "ttft_us",
        )
        extra: dict[str, Any] = {"parent_span_id": parent_id, "type": span_type}
        for key in token_fields:
            value = body.get(key)
            extra[key] = None if value is None else _integer(value, key, minimum=0)
        extra.update({"model": body.get("model"), "provider": body.get("provider")})
        incoming = self._snapshot_values(body, entity="span", extra=extra)
        existing = self._identity_row(
            "spans", row_id, "trace_id=? AND source=? AND external_id=?",
            (trace_id, source, external_id),
        )
        timestamp = now_us()
        if existing is None:
            incoming["metadata_json"] = incoming["metadata_json"] or "{}"
            incoming["tags_json"] = incoming["tags_json"] or "[]"
            incoming["usage_json"] = incoming["usage_json"] or "{}"
            columns = ("id", "trace_id", "source", "external_id", *incoming.keys(),
                       "created_at_us", "last_updated_at_us")
            values = (row_id, trace_id, source, external_id, *incoming.values(), timestamp, timestamp)
            self._insert("spans", columns, values)
            return {"id": row_id, "created": True, "updated": False}
        self._assert_identity(existing, trace_id=trace_id, source=source, external_id=external_id)
        if existing["parent_span_id"] is not None and parent_id not in (None, existing["parent_span_id"]):
            raise ValueError("a span cannot be reparented")
        merged = self._merge_snapshot(existing, incoming)
        if existing["parent_span_id"] is not None:
            merged["parent_span_id"] = existing["parent_span_id"]
        return self._update_changed("spans", existing, merged, timestamp)

    def _snapshot_values(
        self, body: Mapping[str, Any], *, entity: str, extra: Mapping[str, Any]
    ) -> dict[str, Any]:
        status = str(body.get("status") or "running")
        if status not in STATUSES:
            raise ValueError(f"unsupported {entity} status: {status}")
        start = _integer(_required(body, "start_time_us"), "start_time_us", minimum=0)
        end = body.get("end_time_us")
        if end is not None:
            end = _integer(end, "end_time_us", minimum=start)
        source_updated = _integer(
            _required(body, "source_updated_at_us"), "source_updated_at_us", minimum=0
        )
        values: dict[str, Any] = {
            **extra,
            "name": str(_required(body, "name")),
            "status": status,
            "start_time_us": start,
            "end_time_us": end,
            "input_json": None if body.get("input") is None else _json(body["input"], None),
            "output_json": None if body.get("output") is None else _json(body["output"], None),
            "metadata_json": (
                _json(_sanitize_metadata(body.get("metadata")), {})
                if "metadata" in body else None
            ),
            "tags_json": _json(body.get("tags"), []) if "tags" in body else None,
        }
        if entity == "span":
            values["usage_json"] = _json(body.get("usage"), {}) if "usage" in body else None
        values["error_json"] = None if body.get("error") is None else _json(body["error"], None)
        values["source_updated_at_us"] = source_updated
        return values

    def _merge_snapshot(self, existing: sqlite3.Row, incoming: Mapping[str, Any]) -> dict[str, Any]:
        newer = incoming["source_updated_at_us"] >= existing["source_updated_at_us"]
        merged = {key: existing[key] for key in incoming}
        merged["start_time_us"] = min(existing["start_time_us"], incoming["start_time_us"])
        if newer:
            for key, value in incoming.items():
                if value is not None and key != "start_time_us":
                    merged[key] = value
            merged["status"] = _status_after(existing["status"], incoming["status"])
            merged["source_updated_at_us"] = max(
                existing["source_updated_at_us"], incoming["source_updated_at_us"]
            )
        else:
            for key, value in incoming.items():
                if key != "source_updated_at_us" and existing[key] is None and value is not None:
                    merged[key] = value
            if existing["status"] in {"unknown", "abandoned"} and incoming["status"] in _FINAL_STATUSES:
                merged["status"] = incoming["status"]
                for key in _LATE_END_FIELDS:
                    if key in incoming and incoming[key] is not None:
                        merged[key] = incoming[key]
        return merged

    def _identity_row(
        self, table: str, row_id: str, natural_where: str, natural_values: tuple[Any, ...]
    ) -> sqlite3.Row | None:
        by_id = self._connection.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
        natural = self._connection.execute(
            f"SELECT * FROM {table} WHERE {natural_where}", natural_values
        ).fetchone()
        if by_id is not None and natural is not None and by_id["id"] != natural["id"]:
            raise ValueError(f"{table[:-1]} id and external identity identify different records")
        return by_id or natural

    @staticmethod
    def _assert_identity(row: sqlite3.Row, **expected: Any) -> None:
        for key, value in expected.items():
            if row[key] != value:
                raise ValueError(f"{key} is immutable")

    def _validate_parent(self, trace_id: str, parent_id: Any, row_id: str) -> None:
        if parent_id is None:
            return
        if str(parent_id) == row_id:
            raise ValueError("a span cannot parent itself")
        parent = self._connection.execute(
            "SELECT trace_id FROM spans WHERE id=?", (str(parent_id),)
        ).fetchone()
        if parent is None:
            raise ValueError(f"parent span does not exist: {parent_id}")
        if parent["trace_id"] != trace_id:
            raise ValueError("parent span must belong to the same trace")

    def _validate_reference(
        self, table: str, row_id: Any, field: str,
    ) -> sqlite3.Row | None:
        if row_id is None:
            return None
        row = self._connection.execute(
            f"SELECT * FROM {table} WHERE id=?", (str(row_id),)
        ).fetchone()
        if row is None:
            raise ValueError(f"{field} does not exist: {row_id}")
        return row

    @staticmethod
    def _validate_task_status(status: str) -> None:
        if status not in {
            "defined", "ready", "running", "waiting_user", "blocked",
            "completed", "cancelled",
        }:
            raise ValueError(f"unsupported task status: {status}")

    @staticmethod
    def _content_digest(value: Any) -> str:
        return hashlib.sha256(_json(value, None).encode()).hexdigest()

    def _task_knowledge_manifest(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT c.id,c.page_id,c.revision,c.digest,c.kind,c.content,c.source_refs_json
               FROM task_knowledge_bindings b
               JOIN wiki_claims c ON c.id=b.claim_id
               WHERE b.task_id=? ORDER BY c.page_id,c.id""", (task_id,),
        ).fetchall()
        manifest = [{
            "id": row["id"], "page_id": row["page_id"], "revision": row["revision"],
            "digest": row["digest"], "kind": row["kind"], "content": row["content"],
            "source_refs": json.loads(row["source_refs_json"]), "source": "wiki",
        } for row in rows]
        local = self._connection.execute(
            """SELECT id,kind,content_json,execution_id FROM task_knowledge
               WHERE task_id=? AND status IN ('verified','promoted')
               ORDER BY created_at_us,id""", (task_id,),
        ).fetchall()
        manifest.extend({
            "id": row["id"],
            "kind": row["kind"],
            "content": json.loads(row["content_json"]),
            "execution_id": row["execution_id"],
            "digest": self._content_digest(json.loads(row["content_json"])),
            "source": "task",
        } for row in local)
        return manifest

    def _owned_path(self, path: str | Path) -> Path:
        root = self.path.parent.resolve()
        candidate = Path(path).expanduser().resolve()
        if root not in candidate.parents:
            raise ValueError(f"control-plane path must stay under {root}")
        return candidate

    @staticmethod
    def _assignment(existing: sqlite3.Row, field: str, incoming: Any) -> Any:
        current = existing[field]
        if incoming is None:
            return current
        if current is not None and current != incoming:
            raise ValueError(f"{field} is immutable")
        return incoming

    def _insert(self, table: str, columns: tuple[str, ...], values: tuple[Any, ...]) -> None:
        marks = ",".join("?" for _ in columns)
        self._connection.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({marks})", values
        )

    def _update_changed(
        self, table: str, existing: sqlite3.Row, merged: Mapping[str, Any], timestamp: int
    ) -> dict[str, Any]:
        changed = {key: value for key, value in merged.items() if value != existing[key]}
        if not changed:
            return {"id": existing["id"], "created": False, "updated": False}
        changed["last_updated_at_us"] = timestamp
        assignments = ",".join(f"{key}=?" for key in changed)
        self._connection.execute(
            f"UPDATE {table} SET {assignments} WHERE id=?",
            (*changed.values(), existing["id"]),
        )
        return {"id": existing["id"], "created": False, "updated": True}

    def upsert_batch(self, operations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        items = list(operations)
        if not items or len(items) > 256:
            raise ValueError("batch must contain between 1 and 256 operations")
        handlers = {
            "project": self._upsert_project,
            "thread": self._upsert_thread,
            "trace": self._upsert_trace,
            "span": self._upsert_span,
        }
        results = []
        with self._transaction():
            for item in items:
                if item.get("op", "upsert") != "upsert" or item.get("entity") not in handlers:
                    raise ValueError("unsupported batch operation")
                body = item.get("body")
                if not isinstance(body, Mapping):
                    raise ValueError("batch body must be an object")
                result = handlers[str(item["entity"])](body)
                results.append({"entity": item["entity"], **result})
        return results

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM projects ORDER BY name,id").fetchall()
        return [dict(row) for row in rows]

    def list_traces(
        self, *, project_id: str | None = None, harness: str | None = None,
        status: str | None = None, model: str | None = None, attempt_id: str | None = None,
        start_time_from_us: int | None = None, start_time_to_us: int | None = None,
        search: str | None = None, cursor: str | None = None, limit: int = 50,
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        where, values = [], []
        for column, value in (("t.project_id", project_id), ("t.harness", harness), ("t.status", status)):
            if value is not None:
                where.append(f"{column}=?")
                values.append(value)
        if attempt_id is not None:
            where.append("t.attempt_id=?")
            values.append(attempt_id)
        if status is not None and status not in STATUSES:
            raise ValueError(f"unsupported trace status: {status}")
        if start_time_from_us is not None:
            where.append("t.start_time_us>=?")
            values.append(_integer(start_time_from_us, "start_time_from_us", minimum=0))
        if start_time_to_us is not None:
            where.append("t.start_time_us<=?")
            values.append(_integer(start_time_to_us, "start_time_to_us", minimum=0))
        if search:
            where.append(
                "(t.name LIKE ? ESCAPE '\\' OR t.external_id LIKE ? ESCAPE '\\' "
                "OR t.input_json LIKE ? ESCAPE '\\' OR t.output_json LIKE ? ESCAPE '\\')"
            )
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            values.extend((f"%{escaped}%",) * 4)
        if model:
            where.append("EXISTS (SELECT 1 FROM spans sm WHERE sm.trace_id=t.id AND sm.model=?)")
            values.append(model)
        if cursor:
            cursor_start, cursor_id = _decode_cursor(cursor)
            where.append("(t.start_time_us < ? OR (t.start_time_us = ? AND t.id < ?))")
            values.extend((cursor_start, cursor_start, cursor_id))
        clause = " WHERE " + " AND ".join(where) if where else ""
        query = f"""
            SELECT t.*,
                   COALESCE(SUM(s.input_tokens),0) AS input_tokens_total,
                   COALESCE(SUM(s.output_tokens),0) AS output_tokens_total,
                   COALESCE(SUM(s.cost_usd_micros),0) AS cost_usd_micros_total,
                   COUNT(s.id) AS span_count
            FROM traces t LEFT JOIN spans s ON s.trace_id=t.id
            {clause}
            GROUP BY t.id ORDER BY t.start_time_us DESC,t.id DESC LIMIT ?
        """
        with self._lock:
            rows = self._connection.execute(query, (*values, limit + 1)).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [self._decode_entity(row) for row in rows]
        next_cursor = _encode_cursor(rows[-1]["start_time_us"], rows[-1]["id"]) if has_more else None
        return {"items": items, "next_cursor": next_cursor}

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        with self._lock:
            trace = self._connection.execute("SELECT * FROM traces WHERE id=?", (trace_id,)).fetchone()
            if trace is None:
                return None
            spans = self._connection.execute(
                "SELECT * FROM spans WHERE trace_id=? ORDER BY start_time_us,id", (trace_id,)
            ).fetchall()
            feedback = self._connection.execute(
                """SELECT f.* FROM feedback_scores f
                   LEFT JOIN spans s ON s.id=f.span_id
                   WHERE f.trace_id=? OR s.trace_id=? ORDER BY f.created_at_us,f.id""",
                (trace_id, trace_id),
            ).fetchall()
            attempt = None if trace["attempt_id"] is None else self._connection.execute(
                "SELECT * FROM attempts WHERE id=?", (trace["attempt_id"],)
            ).fetchone()
            task_run = None if attempt is None else self._connection.execute(
                "SELECT * FROM task_runs WHERE id=?", (attempt["task_run_id"],)
            ).fetchone()
            context_pack = None if trace["context_pack_id"] is None else self._connection.execute(
                "SELECT * FROM context_packs WHERE id=?", (trace["context_pack_id"],)
            ).fetchone()
            profile = None if trace["profile_revision_id"] is None else self._connection.execute(
                "SELECT * FROM profile_revisions WHERE id=?", (trace["profile_revision_id"],)
            ).fetchone()
        return {
            "trace": self._decode_entity(trace),
            "spans": [self._decode_entity(row) for row in spans],
            "feedback_scores": [dict(row) for row in feedback],
            "attempt": None if attempt is None else self._decode_control_row(attempt),
            "task_run": None if task_run is None else self._decode_control_row(task_run),
            "context_pack": (
                None if context_pack is None else self._decode_control_row(context_pack)
            ),
            "profile_revision": None if profile is None else self._decode_control_row(profile),
            "unassigned_evidence": attempt is None,
        }

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            thread = self._connection.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
            if thread is None:
                return None
            traces = self._connection.execute(
                "SELECT * FROM traces WHERE thread_id=? ORDER BY start_time_us,id", (thread_id,)
            ).fetchall()
        return {
            "thread": self._decode_entity(thread),
            "traces": [self._decode_entity(row) for row in traces],
        }

    def find_active_trace(self, harness: str, thread_external_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT t.* FROM traces t JOIN threads th ON th.id=t.thread_id
                   WHERE th.harness=? AND th.external_id=? AND t.status='running'
                   ORDER BY t.start_time_us DESC,t.id DESC LIMIT 1""",
                (harness, thread_external_id),
            ).fetchone()
        return None if row is None else self._decode_entity(row)

    def ensure_synthetic_trace(
        self, *, harness: str, thread_external_id: str, trace_external_id: str,
        input: Any = None, start_time_us: int | None = None,
    ) -> dict[str, Any]:
        started = start_time_us if start_time_us is not None else now_us()
        if not harness or not thread_external_id or not trace_external_id:
            raise ValueError("harness, thread_external_id, and trace_external_id are required")
        namespace = f"telos:{harness}:{thread_external_id}"
        thread_id = str(uuid5(NAMESPACE_URL, namespace))
        trace_id = str(uuid5(NAMESPACE_URL, f"{namespace}:{trace_external_id}"))
        with self._transaction():
            thread_result = self._upsert_thread({
                "id": thread_id,
                "harness": harness,
                "external_id": thread_external_id,
                "name": thread_external_id,
                "start_time_us": started,
            })
            trace_result = self._upsert_trace({
                "id": trace_id,
                "thread_id": thread_result["id"],
                "harness": harness,
                "source": "gateway",
                "external_id": f"synthetic:{trace_external_id}",
                "name": "Unmatched model call",
                "status": "running",
                "start_time_us": started,
                "input": input,
                "metadata": {"correlation": "unmatched", "synthetic": True},
                "source_updated_at_us": started,
            })
            row = self._connection.execute(
                "SELECT * FROM traces WHERE id=?", (trace_result["id"],)
            ).fetchone()
        assert row is not None
        return self._decode_entity(row)

    def add_feedback_score(self, body: Mapping[str, Any]) -> dict[str, Any]:
        row_id = str(body.get("id") or uuid4())
        trace_id, span_id = body.get("trace_id"), body.get("span_id")
        if (trace_id is None) == (span_id is None):
            raise ValueError("feedback must target exactly one trace or span")
        name = str(_required(body, "name"))
        value = body.get("value")
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            raise ValueError("feedback value must be numeric")
        timestamp = now_us()
        with self._transaction():
            target_table, target_id = ("traces", trace_id) if trace_id is not None else ("spans", span_id)
            if self._connection.execute(
                f"SELECT 1 FROM {target_table} WHERE id=?", (target_id,)
            ).fetchone() is None:
                raise ValueError("feedback target does not exist")
            existing = self._connection.execute(
                "SELECT * FROM feedback_scores WHERE id=?", (row_id,)
            ).fetchone()
            if existing is None:
                self._connection.execute(
                    """INSERT INTO feedback_scores
                       (id,trace_id,span_id,name,value,reason,source,created_at_us,last_updated_at_us)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (row_id, trace_id, span_id, name, float(value), body.get("reason"),
                     body.get("source") or "user", timestamp, timestamp),
                )
                return {"id": row_id, "created": True, "updated": False}
            merged = {
                "trace_id": trace_id,
                "span_id": span_id,
                "name": name,
                "value": float(value),
                "reason": body.get("reason"),
                "source": body.get("source") or "user",
            }
            return self._update_changed("feedback_scores", existing, merged, timestamp)

    @staticmethod
    def _decode_entity(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for public, column in _JSON_COLUMNS.items():
            if column in item:
                raw = item.pop(column)
                item[public] = None if raw is None else json.loads(raw)
        return item

    @staticmethod
    def _decode_control_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for column in tuple(item):
            if column.endswith("_json"):
                raw = item.pop(column)
                item[column[:-5]] = None if raw is None else json.loads(raw)
        return item


class StoreTraceProcessor(TraceProcessor):
    def __init__(self, store: SQLiteTraceStore) -> None:
        self.store = store

    def on_trace_start(self, trace: Trace) -> None:
        self.store.upsert_trace(trace.to_dict())

    def on_trace_end(self, trace: Trace) -> None:
        self.store.upsert_trace(trace.to_dict())

    def on_span_start(self, span: Span) -> None:
        self.store.upsert_span(span.to_dict())

    def on_span_end(self, span: Span) -> None:
        self.store.upsert_span(span.to_dict())

    def force_flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None
