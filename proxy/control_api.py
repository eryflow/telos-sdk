"""Loopback API for Context Packs, handoffs, TaskRuns, and evolution."""

from __future__ import annotations

import asyncio
import difflib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any

from aiohttp import web

from telos.context_pack import create_context_pack, validate_context_pack
from telos.evolution import evaluate_candidate, optimize_profile, propose_candidate
from telos.handoff import CAPABILITIES, compatibility_report, prepare_handoff


class ControlAPI:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.home = store.path.parent.resolve()
        self.token_path = self.home / "control.token"
        try:
            descriptor = os.open(
                self.token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
            )
        except FileExistsError:
            descriptor = None
        if descriptor is not None:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_urlsafe(32) + "\n")
        mode = self.token_path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise RuntimeError("control token path must be a regular file")
        os.chmod(self.token_path, 0o600)
        self.token = self.token_path.read_text().strip()
        if not self.token:
            raise RuntimeError("control write token is empty")

    @staticmethod
    def _loopback(request: web.Request) -> bool:
        return (request.remote or "") in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

    def _read_allowed(self, request: web.Request) -> web.Response | None:
        if not self._loopback(request):
            return web.json_response({"error": "control API is loopback-only"}, status=403)
        return None

    def _write_allowed(self, request: web.Request) -> web.Response | None:
        denied = self._read_allowed(request)
        if denied is not None:
            return denied
        supplied = request.headers.get("Authorization", "")
        if not supplied.lower().startswith("bearer ") or not hmac.compare_digest(
            supplied[7:].strip(), self.token,
        ):
            return web.json_response({"error": "invalid control write token"}, status=401)
        return None

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        raw = await request.read()
        if len(raw) > 1024 * 1024:
            raise ValueError("control request is too large")
        value = json.loads(raw or b"{}")
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return value

    async def page(self, request: web.Request) -> web.Response:
        denied = self._read_allowed(request)
        if denied is not None:
            return web.Response(text="Context control plane is loopback-only", status=403)
        from telos.scripts.build_context_control import render_context_control
        return web.Response(text=render_context_control(self.token), content_type="text/html")

    async def task_runs(self, request: web.Request) -> web.Response:
        if denied := self._read_allowed(request):
            return denied
        try:
            limit = int(request.query.get("limit", "50"))
            return web.json_response({"items": self.store.list_task_runs(limit)})
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def task_run(self, request: web.Request) -> web.Response:
        if denied := self._read_allowed(request):
            return denied
        result = self.store.get_task_run(request.match_info["run_id"])
        return web.json_response(result or {"error": "task run not found"}, status=200 if result else 404)

    async def packs(self, request: web.Request) -> web.Response:
        if denied := self._read_allowed(request):
            return denied
        if request.method == "GET":
            return web.json_response({"items": self.store.list_context_packs()})
        if denied := self._write_allowed(request):
            return denied
        try:
            body = await self._body(request)
            attempt_id = str(body.get("attempt_id") or "")
            if not attempt_id:
                raise ValueError("attempt_id is required; TELOS never guesses the active Attempt")
            attempt = self.store.get_attempt(attempt_id)
            if attempt is None:
                raise ValueError(f"attempt does not exist: {attempt_id}")
            run = self.store.get_task_run(attempt["task_run_id"])["task_run"]
            evidence = self.store.get_attempt_evidence(attempt_id)
            manifest, path = create_context_pack(
                home=self.home, task_run_id=run["id"], source_attempt_id=attempt_id,
                profile_revision_id=attempt.get("profile_revision_id"),
                task_type=body.get("task_type"), parent_pack_id=body.get("parent_pack_id"),
                objective=body.get("objective") or {"goal": run["goal"], "constraints": []},
                policy=body.get("policy") or {"profile_revision_id": attempt.get("profile_revision_id")},
                progress=body.get("progress") or {
                    "done": [f"Observed {evidence['completed_turns']} completed Trace turn(s)"],
                    "in_progress": [], "next": [], "blocked": [],
                    "last_observed_result": evidence["last_output"],
                },
                memory=body.get("memory") or {"facts": [], "decisions": [], "assumptions": []},
                conversation=body.get("conversation") or evidence["conversation"] or None,
                provenance=body.get("provenance") or {
                    "harness": attempt["harness"], "attempt_id": attempt_id,
                    "trace_ids": evidence["trace_ids"],
                },
                requirements=body.get("requirements"), workspace=body.get("workspace"),
                workspace_exclude=body.get("workspace_exclude") or (),
                capture_status=body.get("capture_status") or (
                    None if body.get("progress") and body.get("memory") else "partial"
                ),
                capture_method=body.get("capture_method") or "reconstructed",
            )
            result = self.store.register_context_pack(manifest, path)
            return web.json_response(result, status=201)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def pack(self, request: web.Request) -> web.Response:
        if denied := self._read_allowed(request):
            return denied
        result = self.store.get_context_pack(request.match_info["pack_id"])
        if result is None:
            return web.json_response({"error": "Context Pack not found"}, status=404)
        result["portability"] = {
            harness: compatibility_report(result["path"], harness)
            for harness in CAPABILITIES
        }
        return web.json_response(result)

    async def pack_validate(self, request: web.Request) -> web.Response:
        if denied := self._read_allowed(request):
            return denied
        pack = self.store.get_context_pack(request.match_info["pack_id"])
        if pack is None:
            return web.json_response({"error": "Context Pack not found"}, status=404)
        try:
            manifest = validate_context_pack(pack["path"])
            return web.json_response({"valid": True, "digest": manifest["digest"]})
        except ValueError as exc:
            return web.json_response({"valid": False, "error": str(exc)}, status=409)

    async def handoff_plan(self, request: web.Request) -> web.Response:
        if denied := self._read_allowed(request):
            return denied
        try:
            body = await self._body(request)
            pack = self.store.get_context_pack(str(body.get("pack_id") or ""))
            if pack is None:
                raise ValueError("Context Pack not found")
            report = compatibility_report(
                pack["path"], str(body.get("destination") or ""),
                workspace=body.get("workspace"),
            )
            return web.json_response(report)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def handoff(self, request: web.Request) -> web.Response:
        if denied := self._write_allowed(request):
            return denied
        try:
            body = await self._body(request)
            plan, attempt = prepare_handoff(
                self.store, pack_id=str(body.get("pack_id") or ""),
                destination=str(body.get("destination") or ""),
                workspace=body.get("workspace"), reason=body.get("reason"), home=self.home,
            )
            return web.json_response({"plan": plan, "attempt": attempt}, status=201)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def evolution(self, request: web.Request) -> web.Response:
        if denied := self._read_allowed(request):
            return denied
        result = self.store.get_evolution(request.match_info["task_type"])
        if result:
            by_id = {revision["id"]: revision for revision in result["revisions"]}
            profile_root = (self.home / "profiles").resolve()
            for revision in result["revisions"]:
                path = Path(revision["path"]).resolve()
                if profile_root not in path.parents:
                    continue
                instructions = (path / "instructions.md").read_text()
                revision["instructions"] = instructions
                parent = by_id.get(revision.get("parent_revision_id"))
                if parent:
                    parent_path = Path(parent["path"]).resolve()
                    if profile_root not in parent_path.parents:
                        continue
                    before = (parent_path / "instructions.md").read_text().splitlines()
                    revision["diff"] = "\n".join(difflib.unified_diff(
                        before, instructions.splitlines(), fromfile="production", tofile="candidate", lineterm="",
                    ))
        return web.json_response(result or {"error": "task type not found"}, status=200 if result else 404)

    async def candidate(self, request: web.Request) -> web.Response:
        if denied := self._write_allowed(request):
            return denied
        try:
            body = await self._body(request)
            result = propose_candidate(
                self.store, task_type=str(body.get("task_type") or ""), home=self.home,
                reference_revision_id=body.get("reference_revision_id"),
                optimizer_command=body.get("optimizer_command"),
            )
            return web.json_response(result, status=201)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def evaluation(self, request: web.Request) -> web.Response:
        if denied := self._write_allowed(request):
            return denied
        try:
            body = await self._body(request)
            if body.get("candidate_revision_id"):
                result = await asyncio.to_thread(
                    evaluate_candidate, self.store,
                    task_type=str(body.get("task_type") or ""),
                    candidate_revision_id=str(body["candidate_revision_id"]),
                    reference_revision_id=body.get("reference_revision_id"),
                    runs=int(body.get("runs", 1)),
                )
            else:
                result = await asyncio.to_thread(
                    optimize_profile, self.store,
                    task_type=str(body.get("task_type") or ""),
                    rounds=int(body.get("rounds", 1)), runs=int(body.get("runs", 1)),
                    target_score=body.get("target_score"), home=self.home,
                    optimizer_command=body.get("optimizer_command"),
                )
            return web.json_response(result, status=201)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def promote(self, request: web.Request) -> web.Response:
        if denied := self._write_allowed(request):
            return denied
        try:
            return web.json_response(
                self.store.promote_profile(request.match_info["revision_id"]), status=201,
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def rollback(self, request: web.Request) -> web.Response:
        if denied := self._write_allowed(request):
            return denied
        try:
            return web.json_response(
                self.store.rollback_task_type(request.match_info["task_type_id"]), status=201,
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
