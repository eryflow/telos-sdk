"""Kimi Code command-hook adapter for the TELOS Trace/Span ingest API."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, TextIO

from telos.config import load_config, telos_home
from telos.gateway import control, daemon


HARNESS = "kimi-code"
SOURCE = "kimi-code-hook"
HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "TurnStarted",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "StopFailure",
    "Interrupt",
    "SessionEnd",
)
_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://telos.dev/tracing/kimi-code")


def stable_id(kind: str, *parts: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, ":".join((kind, *parts))))


def _require_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Kimi Code hook payload requires non-empty {key!r}")
    return value


def _operation(entity: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"entity": entity, "op": "upsert", "body": body}


def _metadata(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: payload[key] for key in keys if payload.get(key) is not None}


def _thread(payload: dict[str, Any], now_us: int, *, finished: bool = False) -> dict[str, Any]:
    session_id = _require_text(payload, "session_id")
    body: dict[str, Any] = {
        "id": stable_id("thread", session_id),
        "project_name": "default",
        "harness": HARNESS,
        "external_id": session_id,
        "name": str(payload.get("session_title") or f"Kimi Code {session_id[:12]}"),
        "status": "ok" if finished else "running",
        "start_time_us": now_us,
        "metadata": _metadata(payload, "cwd", "client_type", "model", "profile", "source", "reason"),
    }
    if finished:
        body["end_time_us"] = now_us
    return body


def _trace(
    payload: dict[str, Any],
    now_us: int,
    *,
    status: str = "running",
    input_value: Any = None,
    output_value: Any = None,
    error: Any = None,
) -> dict[str, Any]:
    session_id = _require_text(payload, "session_id")
    turn_id = _require_text(payload, "_telos_turn_id")
    body: dict[str, Any] = {
        "id": stable_id("trace", session_id, turn_id),
        "project_name": "default",
        "thread_id": stable_id("thread", session_id),
        "harness": HARNESS,
        "source": SOURCE,
        "external_id": f"{session_id}:{turn_id}",
        "name": f"Kimi Code turn {turn_id[:12]}",
        "status": status,
        "start_time_us": now_us,
        "source_updated_at_us": now_us,
        "metadata": _metadata(payload, "cwd", "client_type", "origin_kind", "origin_name", "model"),
    }
    if input_value is not None:
        body["input"] = input_value
    if output_value is not None:
        body["output"] = output_value
    if error is not None:
        body["error"] = error
    if status != "running":
        body["end_time_us"] = now_us
    return body


def _span(
    payload: dict[str, Any],
    now_us: int,
    *,
    kind: str,
    external_id: str,
    name: str,
    span_type: str,
    status: str,
    input_value: Any = None,
    output_value: Any = None,
    error: Any = None,
) -> dict[str, Any]:
    session_id = _require_text(payload, "session_id")
    turn_id = _require_text(payload, "_telos_turn_id")
    body: dict[str, Any] = {
        "id": stable_id(kind, session_id, turn_id, external_id),
        "trace_id": stable_id("trace", session_id, turn_id),
        "source": SOURCE,
        "external_id": f"{kind}:{external_id}",
        "name": name,
        "type": span_type,
        "status": status,
        "start_time_us": now_us,
        "source_updated_at_us": now_us,
        "metadata": {},
    }
    if input_value is not None:
        body["input"] = input_value
    if output_value is not None:
        body["output"] = output_value
    if error is not None:
        body["error"] = error
    if status != "running":
        body["end_time_us"] = now_us
    return body


def _base(payload: dict[str, Any], now_us: int) -> list[dict[str, Any]]:
    return [
        _operation("thread", _thread(payload, now_us)),
        _operation("trace", _trace(payload, now_us)),
    ]


def _content_key(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def map_kimi_hook(payload: dict[str, Any], now_us: int) -> list[dict[str, Any]]:
    """Map one normalized Kimi Code lifecycle event to idempotent upserts."""
    if not isinstance(payload, dict):
        raise ValueError("Kimi Code hook payload must be an object")
    if not isinstance(now_us, int) or now_us <= 0:
        raise ValueError("now_us must be a positive integer")
    event = _require_text(payload, "hook_event_name")
    if event not in HOOK_EVENTS:
        raise ValueError(f"unsupported Kimi Code hook event: {event}")
    if event == "SessionStart":
        return [_operation("thread", _thread(payload, now_us))]
    if event == "SessionEnd":
        return [_operation("thread", _thread(payload, now_us, finished=True))]

    operations = _base(payload, now_us)
    if event in {"UserPromptSubmit", "TurnStarted"}:
        prompt = payload.get("prompt")
        operations[1] = _operation("trace", _trace(payload, now_us, input_value=prompt))
        return operations
    if event in {"Stop", "StopFailure", "Interrupt"}:
        status = {"Stop": "ok", "StopFailure": "error", "Interrupt": "cancelled"}[event]
        error = None
        if event == "StopFailure":
            error = {
                "type": payload.get("error_type"),
                "message": payload.get("error_message"),
            }
        operations[1] = _operation(
            "trace",
            _trace(
                payload,
                now_us,
                status=status,
                output_value=payload.get("last_assistant_message"),
                error=error,
            ),
        )
        return operations
    if event in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}:
        tool_name = str(payload.get("tool_name") or "tool")
        tool_id = str(payload.get("tool_call_id") or payload.get("tool_use_id") or _content_key({
            "name": tool_name,
            "input": payload.get("tool_input"),
        }))
        if event == "PreToolUse":
            status, output, error = "running", None, None
        elif event == "PostToolUse":
            status, output, error = "ok", payload.get("tool_output"), None
        else:
            status, output, error = "error", None, payload.get("error")
        operations.append(_operation("span", _span(
            payload,
            now_us,
            kind="tool",
            external_id=tool_id,
            name=tool_name,
            span_type="tool",
            status=status,
            input_value=payload.get("tool_input"),
            output_value=output,
            error=error,
        )))
        return operations
    if event == "PermissionRequest":
        request = {"tool_name": payload.get("tool_name"), "tool_input": payload.get("tool_input")}
        operations.append(_operation("span", _span(
            payload,
            now_us,
            kind="approval",
            external_id=str(payload.get("tool_call_id") or _content_key(request)),
            name=f"Approve {payload.get('tool_name') or 'tool'}",
            span_type="approval",
            status="unknown",
            input_value=request,
        )))
        return operations
    if event in {"SubagentStart", "SubagentStop"}:
        name = str(payload.get("agent_name") or "subagent")
        agent_id = str(payload.get("agent_id") or _content_key(name))
        operations.append(_operation("span", _span(
            payload,
            now_us,
            kind="agent",
            external_id=agent_id,
            name=name,
            span_type="agent",
            status="running" if event == "SubagentStart" else "ok",
            input_value=payload.get("prompt"),
            output_value=payload.get("response"),
        )))
        return operations

    trigger = str(payload.get("trigger") or "unknown")
    operations.append(_operation("span", _span(
        payload,
        now_us,
        kind="compaction",
        external_id=trigger,
        name=f"Compaction ({trigger})",
        span_type="compaction",
        status="running" if event == "PreCompact" else "ok",
    )))
    return operations


def _state_path() -> Path:
    return telos_home() / "integrations" / HARNESS / "active-turns.json"


def _load_turns(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(item, dict)}


def _save_turns(path: Path, turns: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(turns, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _normalize_turn(payload: dict[str, Any], now_us: int, path: Path) -> tuple[dict[str, Any], bool]:
    event = _require_text(payload, "hook_event_name")
    if event in {"SessionStart", "SessionEnd"}:
        return payload, False
    session_id = _require_text(payload, "session_id")
    turns = _load_turns(path)
    current = turns.get(session_id) or {}
    starts = event in {"UserPromptSubmit", "TurnStarted"}
    if starts and (event == "UserPromptSubmit" or not current.get("active")):
        turn_id = str(payload.get("turn_id") or uuid.uuid5(_NAMESPACE, f"turn:{session_id}:{now_us}"))
        current = {"turn_id": turn_id, "active": True}
        if payload.get("prompt") is not None:
            current["prompt"] = payload.get("prompt")
    else:
        turn_id = str(current.get("turn_id") or payload.get("turn_id") or uuid.uuid5(
            _NAMESPACE, f"orphan:{session_id}:{now_us}"
        ))
        current = {**current, "turn_id": turn_id, "active": True}
    normalized = dict(payload)
    normalized["_telos_turn_id"] = turn_id
    if not normalized.get("prompt") and current.get("prompt") is not None:
        normalized["prompt"] = current["prompt"]
    terminal = event in {"Stop", "StopFailure", "Interrupt"}
    if terminal:
        current["active"] = False
    turns[session_id] = current
    # ponytail: one atomic file is enough for normal hook sequencing; move to
    # SQLite locking only if concurrent foreground turns become supported.
    _save_turns(path, turns)
    return normalized, terminal


def post_kimi_operations(operations: list[dict[str, Any]]) -> dict[str, Any]:
    config = load_config()
    policy = config.trace_harnesses.get(HARNESS) or {}
    token = policy.get("tracing_token")
    if policy.get("enabled") is not True or not isinstance(token, str) or not token:
        raise RuntimeError("Kimi Code tracing is not enabled")
    state = daemon.read_state()
    if state is None:
        raise RuntimeError("TELOS gateway is not running")
    result = control.post_trace_batch(
        state.host,
        state.port,
        {"schema_version": 1, "operations": operations},
        token=token,
    )
    if not isinstance(result, dict):
        raise RuntimeError("tracing endpoint returned a non-object response")
    return result


def run_kimi_hook(
    stdin: TextIO,
    *,
    sender: Callable[[list[dict[str, Any]]], Any] = post_kimi_operations,
    clock_us: Callable[[], int] = lambda: time.time_ns() // 1_000,
    turn_state_path: Path | None = None,
) -> int:
    """Read, correlate and send one hook event. Always return success (fail-open)."""
    try:
        now_us = clock_us()
        payload = json.loads(stdin.read())
        normalized, _terminal = _normalize_turn(
            payload, now_us, turn_state_path or _state_path()
        )
        sender(map_kimi_hook(normalized, now_us))
    except Exception:  # noqa: BLE001 - tracing must never block Kimi Code
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    del argv
    return run_kimi_hook(sys.stdin)


if __name__ == "__main__":
    raise SystemExit(main())
