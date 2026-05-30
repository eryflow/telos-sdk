"""TelosAnthropicTransport: wires Anthropic ``messages.create`` through telos.

OpenClaw / Hermes agents call:

    self.client.messages.create(model=..., system=..., messages=[...], tools=[...])

This transport implements the same interface, but internally routes through the
corresponding harness → TELOS Bridge → canonicalize / band-reorder → uses
``AnthropicAdapter.emit()`` to regenerate the wire with ``cache_control`` markers
→ actually sends it to Anthropic's ``/v1/messages``.

Automatic harness detection:
- If the ``system`` field contains ``<system-reminder>`` / ``<command-message>``
  tags, or a message contains a ``thinking`` block → ``hermes`` (Claude Code)
- Otherwise → ``openclaw``

A ``harness_name`` can also be passed explicitly at construction time to
override automatic detection.

Design notes (aligned with telos_transport.py):
- Uses ``engine.emit(ir2, plan)`` rather than a custom wire builder, ensuring the
  Anthropic ``cache_control`` breakpoints are inserted correctly
  (§4.2 BP-T / BP-S / BP-R / BP-X).
- ``max_tokens``: a required Anthropic field, passed in by the caller; defaults
  to 8192 when not passed.
- usage records both raw and normalized, aligned with the ``compute-metrics.py`` schema.
"""

from __future__ import annotations

import json
import os
import re
import time
from os.path import commonprefix
from pathlib import Path
from typing import Any, Iterable, Mapping

from telos import Bridge, load_engine, load_harness
from telos.bridge import BridgeSessionState


# ---------------------------------------------------------------------------
# Harness auto-detection
# ---------------------------------------------------------------------------
#
# Claude Code (Hermes) injects envelope tags in more than one place:
#   - the `system` segment: appears under a few client configurations
#   - the text block of a `user message`: **the vast majority of turns are here**
#                                  — a fresh `<system-reminder>` /
#                                    `<command-message>` per round
# The old implementation only scanned `system`, causing the vast majority of
# Claude Code traffic to be misclassified as openclaw (the real reason
# ``openclaw/*`` source_tags showed up on the dashboard).
#
# Fix approach:
#   1) Use (open + close) paired regexes, to avoid false positives when a user
#      discusses the tags inside a prompt
#   2) Scan the system segment **as well as** the text content of all user messages
#   3) Add `<command-name>` (the slash-command palette injects this tag separately)
#   4) Fallback: an assistant already has a `thinking` block
#   5) Fallback: the tools list hits ≥ 3 of Claude Code's intrinsic tool set
#
# But content detection has a theoretical blind spot: Claude Code's auxiliary
# requests sent with Haiku (conversation-title generation / topic detection etc.)
# have neither tools nor envelope tags, so all 5 rules above miss → misclassified
# as openclaw. Such requests carry no harness signature at all; content detection
# cannot solve it.
#   6) Highest priority — HTTP header fingerprint: the same agent process uses the
#      same HTTP client, so `User-Agent` / `x-app` are identical on the main
#      conversation and auxiliary requests — a per-client signal that content
#      detection cannot obtain yet is stable and reliable for every request. The
#      proxy path passes the request headers in.

_CLAUDE_CODE_MARKER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"<{tag}>.*?</{tag}>", re.DOTALL)
    for tag in ("system-reminder", "command-message", "command-name")
)

# Claude Code always carries several of this tool set; ≥ 3 hits is treated as Claude Code.
_CLAUDE_CODE_TOOL_FINGERPRINT: frozenset[str] = frozenset({
    "Bash", "Edit", "Read", "Write", "Grep", "Glob",
    "TodoWrite", "Task", "WebFetch", "WebSearch", "NotebookEdit",
})
_CLAUDE_CODE_TOOL_HITS_REQUIRED = 3


def _flatten_system_text(raw_request: Mapping[str, Any]) -> str:
    system = raw_request.get("system", [])
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for item in system:
            if isinstance(item, dict):
                t = item.get("text", "")
                if isinstance(t, str):
                    parts.append(t)
            else:
                parts.append(str(item))
        return " ".join(parts)
    return ""


def _iter_user_text(raw_request: Mapping[str, Any]) -> Iterable[str]:
    """Yield, one by one, the plain strings of text blocks inside user messages."""
    for msg in raw_request.get("messages", []) or []:
        if not isinstance(msg, Mapping) or msg.get("role") != "user":
            continue
        content = msg.get("content", [])
        if isinstance(content, str):
            yield content
            continue
        if not isinstance(content, list):
            continue
        for blk in content:
            if isinstance(blk, Mapping) and blk.get("type") == "text":
                text = blk.get("text")
                if isinstance(text, str):
                    yield text


def _has_thinking_block(raw_request: Mapping[str, Any]) -> bool:
    for msg in raw_request.get("messages", []) or []:
        if not isinstance(msg, Mapping):
            continue
        content = msg.get("content", [])
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, Mapping) and blk.get("type") == "thinking":
                    return True
    return False


def _has_claude_code_marker(text: str) -> bool:
    return any(p.search(text) for p in _CLAUDE_CODE_MARKER_PATTERNS)


def _tool_fingerprint_matches_claude_code(raw_request: Mapping[str, Any]) -> bool:
    names: set[str] = set()
    for t in raw_request.get("tools", []) or []:
        if isinstance(t, Mapping):
            n = t.get("name")
            if isinstance(n, str):
                names.add(n)
    return len(names & _CLAUDE_CODE_TOOL_FINGERPRINT) >= _CLAUDE_CODE_TOOL_HITS_REQUIRED


# HTTP header fingerprint of the official Claude Code CLI. The same agent process
# uses the same HTTP client, so these headers are identical on the main
# conversation and auxiliary requests (Haiku title generation / topic detection
# etc.) — exactly the per-client signal that content detection cannot obtain yet
# is stable and reliable for every request.
_CLAUDE_CODE_USER_AGENT_SUBSTR = "claude-cli"   # User-Agent looks like claude-cli/1.x.x ...
_CLAUDE_CODE_X_APP_VALUE = "cli"                # Claude Code sets x-app: cli


def _detect_harness_from_headers(headers: Mapping[str, str]) -> str | None:
    """Identify the harness from the HTTP request headers. Hit on Claude Code → ``"claude-code"``, otherwise ``None``.

    Header keys are case-insensitive (HTTP spec); here they are lowercased
    uniformly before lookup, compatible with aiohttp's ``CIMultiDict`` and the
    plain dicts passed in by unit tests.
    """
    if not headers:
        return None
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    ua = lowered.get("user-agent", "").lower()
    if _CLAUDE_CODE_USER_AGENT_SUBSTR in ua:
        return "claude-code"
    if lowered.get("x-app", "").lower() == _CLAUDE_CODE_X_APP_VALUE:
        return "claude-code"
    return None


def _detect_harness_signal(
    raw_request: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
) -> str | None:
    """Return the **confidently identified** harness, or ``None`` if not identifiable.

    The difference from ``_detect_harness``: here, "no signal hit at all" is
    faithfully returned as ``None`` rather than being mixed into the ``openclaw``
    fallback. The proxy uses this for per-client memory — only a confident signal
    may pin a client, otherwise a Claude Code client whose first request happens
    to be a tool-less auxiliary request would be permanently mis-pinned as openclaw.
    """
    # 0) HTTP header fingerprint — highest priority, reliable for every request (including tool-less auxiliary requests)
    if headers is not None:
        from_headers = _detect_harness_from_headers(headers)
        if from_headers is not None:
            return from_headers

    # 1) envelope tags (system segment + all user message text)
    if _has_claude_code_marker(_flatten_system_text(raw_request)):
        return "claude-code"
    for ut in _iter_user_text(raw_request):
        if _has_claude_code_marker(ut):
            return "claude-code"

    # 2) assistant thinking block
    if _has_thinking_block(raw_request):
        return "claude-code"

    # 3) tool-set fingerprint (works even when the first turn has no reminder injected)
    if _tool_fingerprint_matches_claude_code(raw_request):
        return "claude-code"

    return None


def _detect_harness(
    raw_request: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
) -> str:
    """Identify the harness; fall back to ``openclaw`` when it cannot be identified.

    ``headers`` is optional — the proxy path passes the HTTP request headers,
    while the SDK transport / replay / direct test calls have no headers and run
    content detection only. The signature is backward-compatible.
    """
    return _detect_harness_signal(raw_request, headers) or "openclaw"



# ---------------------------------------------------------------------------
# Usage normalization: aligned with the Anthropic usage schema
# ---------------------------------------------------------------------------

def _normalize_usage(response_usage: Mapping[str, Any]) -> dict[str, int]:
    if not response_usage:
        return {"raw_input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
    return {
        "raw_input": int(response_usage.get("input_tokens", 0)),
        "cache_read": int(response_usage.get("cache_read_input_tokens", 0)),
        "cache_write": int(response_usage.get("cache_creation_input_tokens", 0)),
        "output": int(response_usage.get("output_tokens", 0)),
    }


# ---------------------------------------------------------------------------
# IR summary helpers (reused from telos_transport.py)
# ---------------------------------------------------------------------------

def _summarize_ir(ir) -> dict[str, Any]:
    from telos.ir import Band

    def _bucket():
        return {b.name: {"blocks": 0, "chars": 0} for b in Band}

    def _add(buck, blocks):
        for b in blocks:
            slot = buck[b.band.name]
            slot["blocks"] += 1
            try:
                slot["chars"] += len(
                    json.dumps(b.payload, ensure_ascii=False)
                    if not isinstance(b.payload, str)
                    else b.payload
                )
            except Exception:  # noqa: BLE001
                slot["chars"] += len(str(b.payload))

    tools = _bucket(); _add(tools, ir.tools)
    system = _bucket(); _add(system, ir.system)
    msgs_band = _bucket()
    msg_kinds: dict[str, int] = {}
    for m in ir.messages:
        _add(msgs_band, m.blocks)
        for b in m.blocks:
            msg_kinds[b.kind] = msg_kinds.get(b.kind, 0) + 1
    return {
        "n_tools": len(ir.tools),
        "n_system_blocks": len(ir.system),
        "n_messages": len(ir.messages),
        "bands": {"tools": tools, "system": system, "messages": msgs_band},
        "msg_block_kinds": msg_kinds,
    }


def _flatten_regions(ir_summary: Mapping[str, Any]) -> dict[str, Any]:
    from telos.ir import Band

    bands = ir_summary.get("bands") or {}
    regions: dict[str, Any] = {}
    band_totals = {b.name: 0 for b in Band}
    grand = 0
    for seg in ("tools", "system", "messages"):
        seg_buck = bands.get(seg) or {}
        seg_entry = {b.name: int((seg_buck.get(b.name) or {}).get("chars", 0)) for b in Band}
        seg_entry["total"] = sum(seg_entry.values())
        regions[seg] = seg_entry
        for b in Band:
            band_totals[b.name] += seg_entry[b.name]
        grand += seg_entry["total"]
    return {"by_segment": regions, "by_band": band_totals, "total": grand}


def _summarize_messages(raw_request: Mapping[str, Any]) -> dict[str, Any]:
    msgs = raw_request.get("messages", [])
    by_role: dict[str, dict[str, int]] = {}
    for m in msgs:
        role = str(m.get("role", "?"))
        slot = by_role.setdefault(role, {"count": 0, "chars": 0})
        slot["count"] += 1
        content = m.get("content", "")
        if isinstance(content, str):
            slot["chars"] += len(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict):
                    slot["chars"] += len(blk.get("text", "") or str(blk.get("content", "")))
    return {
        "n_messages": len(msgs),
        "total_chars": sum(s["chars"] for s in by_role.values()),
        "by_role": by_role,
        "n_tools": len(raw_request.get("tools", [])),
    }


def _summarize_plan(plan) -> dict[str, Any]:
    return {
        "routing_key": plan.routing_key,
        "n_slots": len(plan.slots),
        "slots": [
            {
                "name": s.name,
                "segment": s.segment,
                "index": s.index,
                "message_index": s.message_index,
                "ttl_class": s.ttl_class,
            }
            for s in plan.slots
        ],
        "extras": dict(plan.extras) if plan.extras else {},
    }


def _wire_text(wire: Mapping[str, Any]) -> str:
    parts = []
    system = wire.get("system", [])
    if isinstance(system, list):
        for blk in system:
            if isinstance(blk, dict):
                parts.append(f"[system]\n{blk.get('text', '')}")
    elif isinstance(system, str):
        parts.append(f"[system]\n{system}")
    for m in wire.get("messages", []):
        content = m.get("content", "")
        if isinstance(content, str):
            parts.append(f"[{m.get('role', '?')}]\n{content}")
        elif isinstance(content, list):
            text = "\n".join(
                b.get("text", "") or json.dumps(b, ensure_ascii=False, sort_keys=True)
                for b in content
                if isinstance(b, dict)
            )
            parts.append(f"[{m.get('role', '?')}]\n{text}")
    if wire.get("tools"):
        parts.append(json.dumps(wire["tools"], ensure_ascii=False, sort_keys=True))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class TelosAnthropicTransport:
    """A client with an Anthropic-shaped duck interface, internally routing through TELOS (openclaw or hermes harness).

    Args:
        api_key:      passed explicitly when not readable from an envvar.
        base_url:     override the default Anthropic API URL (for debugging).
        session_id:   reuse Bridge stats within the same session.
        harness_name: ``"openclaw"`` / ``"hermes"`` / ``None`` (auto-detect).
        engine_name:  defaults to ``"anthropic"``.
        usage_log:    path that appends one jsonl line per call; ``None`` means don't write.
        prompt_trace_log: path for the structured prompt trace log.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        session_id: str = "telos-session",
        harness_name: str | None = None,
        engine_name: str = "anthropic",
        usage_log: str | None = None,
        prompt_trace_log: str | None = None,
        session_state: BridgeSessionState | None = None,
    ):
        import anthropic  # deferred import

        kwargs: dict[str, Any] = {
            "api_key": api_key or os.environ.get("ANTHROPIC_API_KEY", ""),
        }
        if base_url is not None:
            kwargs["base_url"] = base_url

        self._inner = anthropic.Anthropic(**kwargs)
        self._engine = load_engine(engine_name)
        self._explicit_harness = harness_name
        self._session_id = session_id
        self._usage_log = Path(usage_log) if usage_log else None
        self._trace_log = Path(prompt_trace_log) if prompt_trace_log else None
        self._call_count = 0
        self._prev_wire_text: str = ""
        self._prev_regions: dict[str, Any] | None = None

        # Bridge cross-turn state: one transport instance = one session, so the
        # state naturally accumulates over this instance's lifetime. The caller
        # may also bring its own state (the scenario of multiple transports
        # sharing one conversation).
        self._session_state = (
            session_state if session_state is not None else BridgeSessionState()
        )

        # harness cache (may differ per call when auto-detecting; built only once when explicit)
        self._harness_cache: dict[str, Any] = {}

        # duck interface
        self.messages = _MessagesNS(self)

    @property
    def session_state(self) -> BridgeSessionState:
        return self._session_state

    def _get_harness(self, name: str):
        if name not in self._harness_cache:
            self._harness_cache[name] = load_harness(name)
        return self._harness_cache[name]

    # ------------------------------------------------------------------
    # Internal: execute one create
    # ------------------------------------------------------------------

    def _do_create(self, kwargs: dict[str, Any]):
        from telos.ir import Band

        self._call_count += 1
        model = kwargs.get("model", "")
        max_tokens = kwargs.get("max_tokens", 8192)

        # ---- 0. snapshot of the caller's raw input ----
        input_summary = _summarize_messages(kwargs)

        # ---- 1. pick the harness (explicit > sticky > auto-detect) ----
        if self._explicit_harness:
            harness_name = self._explicit_harness
        elif self._session_state.sticky_harness:
            harness_name = self._session_state.sticky_harness
        else:
            harness_name = _detect_harness(kwargs)
            self._session_state.sticky_harness = harness_name
        harness = self._get_harness(harness_name)

        # ---- 2. parse → IR ----
        ir = harness.parse(
            kwargs,
            session_id=self._session_id,
            engine="anthropic",
            model=model,
        )
        ir_in_summary = _summarize_ir(ir)

        # ---- 3. Bridge: canonicalize + plan + emit ----
        # Key point: pass session_state so the ref-pool / R8 counters accumulate
        # across turns; using emit_with_plan() is required to run _canonicalize_ir
        # (tools ordering, payload key ordering) — calling engine.emit(ir2, plan)
        # directly would skip this step.
        bridge = Bridge(ir, self._engine, session_state=self._session_state)
        wire_dict, plan = bridge.emit_with_plan()
        plan_summary = _summarize_plan(plan)

        # ---- 4. assemble wire / log snapshot ----
        ir2 = bridge.snapshot_ir()
        ir_out_summary = _summarize_ir(ir2)
        regions = _flatten_regions(ir_out_summary)

        if self._prev_regions is None:
            region_deltas: dict[str, Any] = {"first_call": True}
        else:
            prev = self._prev_regions
            region_deltas = {
                "first_call": False,
                "by_segment": {
                    seg: regions["by_segment"][seg]["total"]
                         - prev["by_segment"][seg]["total"]
                    for seg in ("tools", "system", "messages")
                },
                "by_band": {
                    b.name: regions["by_band"][b.name] - prev["by_band"][b.name]
                    for b in Band
                },
                "total": regions["total"] - prev["total"],
            }

        wire: dict[str, Any] = dict(wire_dict)
        wire["max_tokens"] = max_tokens

        # pass through non-telos fields supplied by the caller
        for k in ("temperature", "top_p", "stream", "stop_sequences",
                  "tool_choice", "thinking", "metadata", "timeout"):
            if k in kwargs and kwargs[k] is not None:
                wire[k] = kwargs[k]

        wire_text = _wire_text(wire)
        prefix_match_chars = (
            len(commonprefix([self._prev_wire_text, wire_text]))
            if self._prev_wire_text else 0
        )

        # ---- 5. actually send the request ----
        t0 = time.time()
        response = self._inner.messages.create(**wire)
        dt = time.time() - t0

        # ---- 6. usage normalization + cross-turn accumulation ----
        usage_obj = getattr(response, "usage", None)
        usage_dict = usage_obj.model_dump() if usage_obj is not None else {}
        normalized = _normalize_usage(usage_dict)
        inp_total = normalized["raw_input"] + normalized["cache_read"]
        cache_share = (normalized["cache_read"] / inp_total) if inp_total else 0.0

        # bridge.absorb_usage: calls engine.parse_usage + state.cumulative_cache_creation += ...
        # Wrap it in a dict to feed it (the anthropic engine expects ``{"usage": {...}}``).
        try:
            bridge.absorb_usage({"usage": usage_dict})
        except Exception:  # noqa: BLE001
            pass  # an accumulation failure does not affect the main path

        if self._usage_log is not None:
            self._usage_log.parent.mkdir(parents=True, exist_ok=True)
            with self._usage_log.open("a") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "session_id": self._session_id,
                    "call_index": self._call_count,
                    "model": model,
                    "harness": harness_name,
                    "latency_s": round(dt, 3),
                    "routing_key": plan.routing_key,
                    "raw_usage": usage_dict,
                    "normalized": normalized,
                    "cumulative": {
                        "cache_creation":
                            self._session_state.stats.cumulative_cache_creation,
                        "real_requests_since_refresh":
                            self._session_state.stats.real_requests_since_refresh,
                        "refpool_slugs": sorted(self._session_state.refpool.slugs),
                    },
                }, ensure_ascii=False) + "\n")

        if self._trace_log is not None:
            self._trace_log.parent.mkdir(parents=True, exist_ok=True)
            with self._trace_log.open("a") as f:
                f.write(json.dumps({
                    "session_id": self._session_id,
                    "call_index": self._call_count,
                    "model": model,
                    "harness": harness_name,
                    "latency_s": round(dt, 3),
                    "input": input_summary,
                    "ir_after_parse": ir_in_summary,
                    "ir_after_canonicalize": ir_out_summary,
                    "regions": regions,
                    "region_deltas": region_deltas,
                    "plan": plan_summary,
                    "breakpoints": plan_summary["slots"],
                    "prefix": {
                        "prev_wire_chars": len(self._prev_wire_text),
                        "this_wire_chars": len(wire_text),
                        "common_prefix_chars": prefix_match_chars,
                        "prefix_stability": (
                            prefix_match_chars / len(self._prev_wire_text)
                            if self._prev_wire_text else None
                        ),
                    },
                    "cache": {
                        "raw_input": normalized["raw_input"],
                        "cache_read": normalized["cache_read"],
                        "cache_write": normalized["cache_write"],
                        "output": normalized["output"],
                        "input_total": inp_total,
                        "cache_share": round(cache_share, 4),
                    },
                    "cumulative": {
                        "cache_creation":
                            self._session_state.stats.cumulative_cache_creation,
                        "real_requests_since_refresh":
                            self._session_state.stats.real_requests_since_refresh,
                        "refpool_slugs": sorted(self._session_state.refpool.slugs),
                    },
                }, ensure_ascii=False) + "\n")

        self._prev_wire_text = wire_text
        self._prev_regions = regions
        return response


class _MessagesNS:
    def __init__(self, t: TelosAnthropicTransport):
        self._t = t

    def create(self, **kwargs):
        return self._t._do_create(kwargs)
