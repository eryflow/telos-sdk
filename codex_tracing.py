"""Codex command-hook adapter for the TELOS Trace/Span ingest API."""

from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from typing import Any, Callable, TextIO

from telos.config import load_config
from telos.gateway import control, daemon


HARNESS = "codex"
SOURCE = "codex-hook"
HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "Interrupt",
    "SessionEnd",
)
_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://telos.dev/tracing/codex")


def stable_id(kind: str, *parts: str) -> str:
    """Return the same internal UUID for the same Codex source identity."""
    return str(uuid.uuid5(_ID_NAMESPACE, ":".join((kind, *parts))))


def _require_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Codex hook payload requires non-empty {key!r}")
    return value


def _metadata(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: payload[key] for key in keys if payload.get(key) is not None}


def _operation(entity: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"entity": entity, "op": "upsert", "body": body}


def _thread_body(
    payload: dict[str, Any], now_us: int, *, finished: bool = False
) -> dict[str, Any]:
    session_id = _require_text(payload, "session_id")
    body: dict[str, Any] = {
        "id": stable_id("thread", session_id),
        "project_name": "default",
        "harness": HARNESS,
        "external_id": session_id,
        "name": f"Codex {session_id[:12]}",
        "status": "ok" if finished else "running",
        "start_time_us": now_us,
        "metadata": _metadata(
            payload,
            "cwd",
            "transcript_path",
            "model",
            "permission_mode",
            "source",
            "reason",
        ),
    }
    if finished:
        body["end_time_us"] = now_us
    return body


def _trace_body(
    payload: dict[str, Any],
    now_us: int,
    *,
    status: str = "running",
    prompt: str | None = None,
    output: Any = None,
) -> dict[str, Any]:
    session_id = _require_text(payload, "session_id")
    turn_id = _require_text(payload, "turn_id")
    name = f"Codex turn {turn_id[:12]}"
    body: dict[str, Any] = {
        "id": stable_id("trace", session_id, turn_id),
        "project_name": "default",
        "thread_id": stable_id("thread", session_id),
        "harness": HARNESS,
        "source": SOURCE,
        "external_id": f"{session_id}:{turn_id}",
        "name": name,
        "status": status,
        "start_time_us": now_us,
        "source_updated_at_us": now_us,
        "metadata": _metadata(
            payload, "cwd", "model", "permission_mode", "agent_id", "agent_type"
        ),
    }
    if prompt is not None:
        body["input"] = prompt
    if output is not None:
        body["output"] = output
    if status != "running":
        body["end_time_us"] = now_us
    return body


def _span_body(
    payload: dict[str, Any],
    now_us: int,
    *,
    kind: str,
    external_id: str,
    name: str,
    span_type: str,
    status: str = "running",
    input_value: Any = None,
    output_value: Any = None,
    error: Any = None,
    parent_span_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session_id = _require_text(payload, "session_id")
    turn_id = _require_text(payload, "turn_id")
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
        "metadata": metadata or {},
    }
    if parent_span_id:
        body["parent_span_id"] = parent_span_id
    if input_value is not None:
        body["input"] = input_value
    if output_value is not None:
        body["output"] = output_value
    if error is not None:
        body["error"] = error
    if status != "running":
        body["end_time_us"] = now_us
    return body


def _base_turn_operations(payload: dict[str, Any], now_us: int) -> list[dict[str, Any]]:
    return [
        _operation("thread", _thread_body(payload, now_us)),
        _operation("trace", _trace_body(payload, now_us)),
    ]


def _agent_body(
    payload: dict[str, Any], now_us: int, *, status: str = "running", output: Any = None
) -> dict[str, Any]:
    agent_id = _require_text(payload, "agent_id")
    return _span_body(
        payload,
        now_us,
        kind="agent",
        external_id=agent_id,
        name=str(payload.get("agent_type") or "subagent"),
        span_type="agent",
        status=status,
        output_value=output,
        metadata=_metadata(payload, "agent_id", "agent_type", "agent_transcript_path"),
    )


def _parent_agent_operation(
    payload: dict[str, Any], now_us: int
) -> tuple[list[dict[str, Any]], str | None]:
    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        return [], None
    body = _agent_body(payload, now_us)
    return [_operation("span", body)], body["id"]


def _tool_error(response: Any) -> Any:
    if not isinstance(response, dict):
        return None
    if response.get("is_error") is True or response.get("success") is False:
        return response.get("error") or response
    status = str(response.get("status") or "").lower()
    if status in {"error", "failed", "cancelled"}:
        return response.get("error") or response
    if response.get("error") not in (None, False, ""):
        return response["error"]
    return None


def _content_key(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def map_codex_hook(payload: dict[str, Any], now_us: int) -> list[dict[str, Any]]:
    """Purely map one of Codex's 12 command-hook payloads to upserts.

    Each turn-scoped event includes its Thread and Trace prerequisites so a
    failed earlier hook delivery cannot make this event violate foreign keys.
    """
    if not isinstance(payload, dict):
        raise ValueError("Codex hook payload must be an object")
    if not isinstance(now_us, int) or now_us <= 0:
        raise ValueError("now_us must be a positive integer")
    event = _require_text(payload, "hook_event_name")
    if event not in HOOK_EVENTS:
        raise ValueError(f"unsupported Codex hook event: {event}")

    if event == "SessionStart":
        return [_operation("thread", _thread_body(payload, now_us))]
    if event == "SessionEnd":
        return [_operation("thread", _thread_body(payload, now_us, finished=True))]

    operations = _base_turn_operations(payload, now_us)
    if event == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or "")
        operations[1] = _operation(
            "trace", _trace_body(payload, now_us, prompt=prompt)
        )
        return operations
    if event in {"Stop", "Interrupt"}:
        status = "ok" if event == "Stop" else "cancelled"
        operations[1] = _operation(
            "trace",
            _trace_body(
                payload,
                now_us,
                status=status,
                output=payload.get("last_assistant_message") if event == "Stop" else None,
            ),
        )
        return operations
    if event in {"SubagentStart", "SubagentStop"}:
        status = "running" if event == "SubagentStart" else "ok"
        output = payload.get("last_assistant_message") if event == "SubagentStop" else None
        operations.append(_operation("span", _agent_body(payload, now_us, status=status, output=output)))
        return operations

    parent_operations, parent_id = _parent_agent_operation(payload, now_us)
    operations.extend(parent_operations)
    if event in {"PreToolUse", "PostToolUse"}:
        tool_use_id = _require_text(payload, "tool_use_id")
        tool_name = str(payload.get("tool_name") or "tool")
        response = payload.get("tool_response")
        error = _tool_error(response) if event == "PostToolUse" else None
        status = "running" if event == "PreToolUse" else ("error" if error is not None else "ok")
        operations.append(
            _operation(
                "span",
                _span_body(
                    payload,
                    now_us,
                    kind="tool",
                    external_id=tool_use_id,
                    name=tool_name,
                    span_type="tool",
                    status=status,
                    input_value=payload.get("tool_input"),
                    output_value=response if event == "PostToolUse" else None,
                    error=error,
                    parent_span_id=parent_id,
                    metadata=_metadata(payload, "tool_name", "model", "permission_mode"),
                ),
            )
        )
        return operations
    if event == "PermissionRequest":
        request = {
            "tool_name": payload.get("tool_name"),
            "tool_input": payload.get("tool_input"),
        }
        # ponytail: identical approvals in one turn collapse; Codex exposes no
        # approval id, so upgrade when its hook schema gains one.
        request_id = _content_key(request)
        operations.append(
            _operation(
                "span",
                _span_body(
                    payload,
                    now_us,
                    kind="approval",
                    external_id=request_id,
                    name=f"Approve {payload.get('tool_name') or 'tool'}",
                    span_type="approval",
                    status="unknown",
                    input_value=request,
                    parent_span_id=parent_id,
                    metadata={"decision": "requested"},
                ),
            )
        )
        return operations

    trigger = str(payload.get("trigger") or "unknown")
    # ponytail: repeated compactions with the same trigger/agent in one turn
    # collapse; Codex currently exposes no compaction id.
    compact_id = f"{payload.get('agent_id') or 'root'}:{trigger}"
    operations.append(
        _operation(
            "span",
            _span_body(
                payload,
                now_us,
                kind="compaction",
                external_id=compact_id,
                name=f"Compaction ({trigger})",
                span_type="compaction",
                status="running" if event == "PreCompact" else "ok",
                parent_span_id=parent_id,
                metadata=_metadata(payload, "trigger", "model", "agent_id", "agent_type"),
            ),
        )
    )
    return operations


def post_codex_operations(operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Send one authenticated batch; callers deliberately handle failures."""
    config = load_config()
    policy = config.trace_harnesses.get(HARNESS) or {}
    token = policy.get("tracing_token")
    if policy.get("enabled") is not True or not isinstance(token, str) or not token:
        raise RuntimeError("Codex tracing is not enabled")
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


def run_codex_hook(
    stdin: TextIO,
    *,
    sender: Callable[[list[dict[str, Any]]], Any] = post_codex_operations,
    clock_us: Callable[[], int] = lambda: time.time_ns() // 1_000,
) -> int:
    """Read stdin, map and send. Always succeed so tracing is fail-open."""
    try:
        payload = json.loads(stdin.read())
        operations = map_codex_hook(payload, clock_us())
        sender(operations)
    except Exception:  # noqa: BLE001 - a hook must never block Codex
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point to be registered by ``telos trace-hook codex``."""
    del argv
    return run_codex_hook(sys.stdin)


if __name__ == "__main__":
    raise SystemExit(main())
