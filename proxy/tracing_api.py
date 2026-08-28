"""aiohttp ingress/read API for the local Trace store."""

from __future__ import annotations

import hmac
import json
import logging
import time
from typing import Any, Mapping

from aiohttp import web

from telos.tracing import SQLiteTraceStore


_log = logging.getLogger(__name__)
_MAX_BATCH_BYTES = 1024 * 1024
_MAX_BATCH_OPERATIONS = 256


class TracingAPI:
    def __init__(
        self,
        store: SQLiteTraceStore,
        policies: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self._policies = policies

    @staticmethod
    def _is_loopback(request: web.Request) -> bool:
        return (request.remote or "") in (
            "127.0.0.1", "::1", "::ffff:127.0.0.1",
        )

    def _policies_now(self) -> Mapping[str, Mapping[str, Any]]:
        if self._policies is not None:
            return self._policies
        from telos.config import load_config
        return load_config().trace_harnesses

    def _authorized_harness(self, request: web.Request) -> str | None:
        authorization = request.headers.get("Authorization", "")
        if not authorization.lower().startswith("bearer "):
            return None
        supplied = authorization[7:].strip()
        if not supplied:
            return None
        for harness, policy in self._policies_now().items():
            expected = policy.get("tracing_token")
            if (
                policy.get("enabled") is True
                and isinstance(expected, str)
                and hmac.compare_digest(supplied, expected)
            ):
                return harness
        return None

    async def batch(self, request: web.Request) -> web.Response:
        if not self._is_loopback(request):
            return web.json_response({"error": "tracing ingest is loopback-only"}, status=403)
        harness = self._authorized_harness(request)
        if harness is None:
            return web.json_response({"error": "invalid tracing token"}, status=401)
        body = await request.read()
        if len(body) > _MAX_BATCH_BYTES:
            return web.json_response({"error": "tracing batch is too large"}, status=413)
        try:
            payload = json.loads(body)
            operations = payload["operations"]
            if payload.get("schema_version") != 1:
                raise ValueError("schema_version must be 1")
            if not isinstance(operations, list) or not 1 <= len(operations) <= _MAX_BATCH_OPERATIONS:
                raise ValueError("operations must contain 1..256 items")
            normalized = self._normalize_operations(operations, harness)
            accepted = self.store.upsert_batch(normalized)
            for operation in normalized:
                body = operation["body"]
                if operation["entity"] == "thread" and body.get("attempt_id"):
                    self.store.set_attempt_status(str(body["attempt_id"]), "running")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return web.json_response({"error": str(exc) or "invalid tracing batch"}, status=400)
        except Exception:  # noqa: BLE001
            _log.exception("tracing batch write failed")
            return web.json_response({"error": "local tracing write failed"}, status=500)
        return web.json_response({"accepted": accepted})

    def _normalize_operations(
        self, operations: list[Any], authorized_harness: str,
    ) -> list[dict[str, Any]]:
        now = time.time_ns() // 1_000
        trace_harnesses: dict[str, str] = {}
        normalized: list[dict[str, Any]] = []
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("op", "upsert") != "upsert":
                raise ValueError("each operation must be an upsert object")
            entity = operation.get("entity")
            if entity not in ("thread", "trace", "span"):
                raise ValueError(f"unsupported tracing entity: {entity!r}")
            original = operation.get("body")
            if not isinstance(original, dict):
                raise ValueError("operation body must be an object")
            item = dict(original)
            project_name = item.pop("project_name", "default")
            if not isinstance(project_name, str) or not project_name:
                raise ValueError("project_name must be a non-empty string")
            if entity in ("thread", "trace"):
                if item.get("harness") != authorized_harness:
                    raise ValueError("operation harness does not match its token")
                item["project_id"] = self.store.ensure_project(project_name)["id"]
            if entity == "trace":
                trace_harnesses[str(item.get("id") or "")] = authorized_harness
                item.setdefault("source_updated_at_us", now)
            elif entity == "span":
                trace_id = str(item.get("trace_id") or "")
                trace_harness = trace_harnesses.get(trace_id)
                if trace_harness is None:
                    detail = self.store.get_trace(trace_id)
                    trace_harness = (
                        detail.get("trace", {}).get("harness") if detail else None
                    )
                if trace_harness != authorized_harness:
                    raise ValueError("span trace does not match the authorized harness")
                item.setdefault("source_updated_at_us", now)
            normalized.append({"entity": entity, "op": "upsert", "body": item})
        return normalized

    async def projects(self, request: web.Request) -> web.Response:
        if not self._is_loopback(request):
            return web.json_response({"error": "tracing API is loopback-only"}, status=403)
        return web.json_response({"items": self.store.list_projects()})

    async def traces(self, request: web.Request) -> web.Response:
        if not self._is_loopback(request):
            return web.json_response({"error": "tracing API is loopback-only"}, status=403)
        try:
            result = self.store.list_traces(
                project_id=request.query.get("project_id"),
                harness=request.query.get("harness"),
                status=request.query.get("status"),
                model=request.query.get("model"),
                start_time_from_us=_optional_int(request.query.get("start_time_from_us")),
                start_time_to_us=_optional_int(request.query.get("start_time_to_us")),
                search=request.query.get("q"),
                cursor=request.query.get("cursor"),
                limit=_optional_int(request.query.get("limit")) or 50,
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        for item in result["items"]:
            item["total_tokens"] = (
                item.pop("input_tokens_total", 0) + item.pop("output_tokens_total", 0)
            )
            item["cost_usd_micros"] = item.pop("cost_usd_micros_total", 0)
            for key in ("input", "output", "metadata", "tags", "error"):
                item.pop(key, None)
        return web.json_response(result)

    async def trace_detail(self, request: web.Request) -> web.Response:
        if not self._is_loopback(request):
            return web.json_response({"error": "tracing API is loopback-only"}, status=403)
        detail = self.store.get_trace(request.match_info["trace_id"])
        if detail is None:
            return web.json_response({"error": "trace not found"}, status=404)
        trace = detail["trace"]
        spans = detail["spans"]
        trace["total_tokens"] = sum(
            int(span.get("input_tokens") or 0) + int(span.get("output_tokens") or 0)
            for span in spans
        )
        trace["cost_usd_micros"] = sum(
            int(span.get("cost_usd_micros") or 0) for span in spans
        )
        return web.json_response(detail)

    async def thread_detail(self, request: web.Request) -> web.Response:
        if not self._is_loopback(request):
            return web.json_response({"error": "tracing API is loopback-only"}, status=403)
        detail = self.store.get_thread(request.match_info["thread_id"])
        if detail is None:
            return web.json_response({"error": "thread not found"}, status=404)
        return web.json_response(detail)

    async def feedback(self, request: web.Request) -> web.Response:
        if not self._is_loopback(request):
            return web.json_response({"error": "tracing API is loopback-only"}, status=403)
        try:
            payload = json.loads(await request.read())
            if not isinstance(payload, dict):
                raise ValueError("feedback body must be an object")
            result = self.store.add_feedback_score(payload)
        except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(result, status=201 if result["created"] else 200)

    async def page(self, request: web.Request) -> web.Response:
        if not self._is_loopback(request):
            return web.Response(text="Trace explorer is loopback-only", status=403)
        from telos.scripts.build_trace_explorer import render_trace_explorer
        return web.Response(text=render_trace_explorer(), content_type="text/html")


def _optional_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"expected integer, got {value!r}") from exc
