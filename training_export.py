"""Local JSONL exits for SFT, preference, and reward-learning pipelines."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text("".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def export_training_data(
    store: Any, output_dir: str | Path, *, task_type: str | None = None,
) -> dict[str, Any]:
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    where, values = "", []
    if task_type:
        where = "AND (tt.id=? OR tt.name=?)"
        values = [task_type, task_type]
    with store._lock:
        traces = store._connection.execute(f"""
            SELECT tr.id,tr.input_json,tr.output_json,a.id AS attempt_id,
                   r.id AS task_run_id,tt.name AS task_type,
                   (SELECT o.score FROM outcome_resolutions o
                    WHERE o.attempt_id=a.id AND o.outcome='pass'
                    ORDER BY o.created_at_us DESC LIMIT 1) AS outcome_score
            FROM traces tr JOIN attempts a ON a.id=tr.attempt_id
            JOIN task_runs r ON r.id=a.task_run_id
            LEFT JOIN task_types tt ON tt.id=r.task_type_id
            WHERE tr.input_json IS NOT NULL AND tr.output_json IS NOT NULL
              AND EXISTS (SELECT 1 FROM outcome_resolutions o
                          WHERE o.attempt_id=a.id AND o.outcome='pass') {where}
            ORDER BY tr.start_time_us,tr.id
        """, values).fetchall()
        results = store._connection.execute(f"""
            SELECT er.*,c.pack_id,ev.reference_revision_id,ev.candidate_revision_id,
                   tt.name AS task_type
            FROM evaluation_results er
            JOIN evaluation_runs ev ON ev.id=er.evaluation_run_id
            JOIN regression_cases c ON c.id=er.regression_case_id
            JOIN task_types tt ON tt.id=ev.task_type_id
            WHERE 1=1 {where}
            ORDER BY er.evaluation_run_id,er.regression_case_id,er.harness,er.variant
        """, values).fetchall()
    sft = [{
        "messages": [
            {"role": "user", "content": json.loads(row["input_json"])},
            {"role": "assistant", "content": json.loads(row["output_json"])},
        ],
        "task_type": row["task_type"], "score": row["outcome_score"],
        "provenance": {"trace_id": row["id"], "attempt_id": row["attempt_id"],
                       "task_run_id": row["task_run_id"]},
    } for row in traces]
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in results:
        row = store._decode_control_row(raw)
        grouped.setdefault(
            (row["evaluation_run_id"], row["regression_case_id"], row["harness"]), {}
        )[row["variant"]] = row
    preference = []
    for pair in grouped.values():
        if set(pair) != {"reference", "candidate"}:
            continue
        candidate, reference = pair["candidate"], pair["reference"]
        chosen, rejected = (candidate, reference) if candidate["score"] >= reference["score"] else (reference, candidate)
        chosen_revision = store.get_profile_revision(
            chosen["candidate_revision_id"] if chosen["variant"] == "candidate" else chosen["reference_revision_id"]
        )
        rejected_revision = store.get_profile_revision(
            rejected["candidate_revision_id"] if rejected["variant"] == "candidate" else rejected["reference_revision_id"]
        )
        preference.append({
            "context_pack_id": chosen["pack_id"], "harness": chosen["harness"],
            "chosen": Path(chosen_revision["path"]).joinpath("instructions.md").read_text(),
            "rejected": Path(rejected_revision["path"]).joinpath("instructions.md").read_text(),
            "chosen_score": chosen["score"], "rejected_score": rejected["score"],
            "provenance": {"evaluation_run_id": chosen["evaluation_run_id"],
                           "regression_case_id": chosen["regression_case_id"]},
        })
    rl = [{
        "context_pack_id": row["pack_id"], "profile_revision_id": (
            row["candidate_revision_id"] if row["variant"] == "candidate"
            else row["reference_revision_id"]
        ),
        "harness": row["harness"], "reward": row["score"], "outcome": row["outcome"],
        "cost_usd_micros": row["cost_usd_micros"], "latency_us": row["latency_us"],
        "provenance": {"evaluation_run_id": row["evaluation_run_id"],
                       "regression_case_id": row["regression_case_id"],
                       "attempt_id": row["attempt_id"]},
    } for row in (store._decode_control_row(raw) for raw in results)]
    paths = {"sft": output / "sft.jsonl", "preference": output / "preference.jsonl", "rl": output / "rl.jsonl"}
    for name, rows in (("sft", sft), ("preference", preference), ("rl", rl)):
        _write_jsonl(paths[name], rows)
    return {"paths": {name: str(path) for name, path in paths.items()},
            "counts": {"sft": len(sft), "preference": len(preference), "rl": len(rl)}}
