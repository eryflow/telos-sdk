"""Anthropic adapter (Claude Opus / Sonnet 4.6+).

Basis:
- prompt-caching docs: 4 explicit ``cache_control`` slots, 5m / 1h TTL,
  ``max_tokens:0`` prewarm, 20-block lookback, hierarchy
  ``tools → system → messages``, with mixed TTL the 1h must precede the 5m.
- Fix R2: for long sessions, insert a mid-rolling anchor (moves every 19
  turns) to cover the lookback window.
- Fix R7: when candidates > 4, trim by the priority
  ``pin-tools > pin-system > ref-pool > rolling-mid > latest-turn``.
"""

from __future__ import annotations

from typing import Any, Mapping

from telos.engine.base import EmitPlan, EngineAdapter, EngineCapabilities, MarkSlot
from telos.ir import Band, TelosIR, UsageReport


_LOOKBACK = 20         # the lookback ceiling explicitly stated in the Anthropic docs
_MID_ANCHOR_STRIDE = 19  # leave a 1-block buffer to guarantee the previous BP is found within the window next time


class AnthropicAdapter(EngineAdapter):
    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            explicit_breakpoints=True,
            ttl_control="presets",          # 5m / 1h only
            prewarmable=True,
            routing_key=False,
            retention_policy="fixed",
            max_breakpoints=4,
            thinking_preserved_across_non_tool_result=True,  # default to 4.6+ behavior
        )

    # ------------------------------------------------------------------
    # plan_marks: priority allocation of the 4 slots
    # ------------------------------------------------------------------

    def plan_marks(self, ir: TelosIR) -> EmitPlan:
        candidates: list[MarkSlot] = []

        # BP-T: end of the tools segment (only when tools exist)
        if ir.tools:
            candidates.append(MarkSlot(
                name="BP-T", segment="tools",
                index=len(ir.tools) - 1, ttl_class="long",
            ))

        # BP-S: the last PIN block in the system segment (excluding ref-pool)
        last_pin_idx = _last_band_index(ir.system, Band.PIN)
        if last_pin_idx is not None:
            candidates.append(MarkSlot(
                name="BP-S", segment="system",
                index=last_pin_idx, ttl_class="long",
            ))

        # BP-R: the last FOLD block in the system segment (i.e. the end of the ref-pool)
        last_fold_idx = _last_band_index(ir.system, Band.FOLD)
        if last_fold_idx is not None:
            candidates.append(MarkSlot(
                name="BP-R", segment="system",
                index=last_fold_idx, ttl_class="long",
            ))

        # BP-X: the last non-DROP block in the last message (5m rolling)
        if ir.messages:
            for msg_idx in range(len(ir.messages) - 1, -1, -1):
                last_keep = _last_non_drop_index(ir.messages[msg_idx].blocks)
                if last_keep is not None:
                    candidates.append(MarkSlot(
                        name="BP-X", segment="message",
                        index=last_keep, message_index=msg_idx, ttl_class="short",
                    ))
                    break

        # Fix R2: once the session is long enough (>= 19 messages), insert a mid-rolling anchor.
        # Position: the last non-DROP block at the current - 19 turn, TTL=5m
        if len(ir.messages) >= _MID_ANCHOR_STRIDE:
            mid_msg_idx = len(ir.messages) - _MID_ANCHOR_STRIDE
            last_keep = _last_non_drop_index(ir.messages[mid_msg_idx].blocks)
            if last_keep is not None:
                candidates.append(MarkSlot(
                    name="BP-mid", segment="message",
                    index=last_keep, message_index=mid_msg_idx, ttl_class="short",
                ))

        # Fix R7: candidates may exceed 4, trim by priority
        priority = {"BP-T": 0, "BP-S": 1, "BP-R": 2, "BP-mid": 3, "BP-X": 4}
        candidates.sort(key=lambda s: priority.get(s.name, 99))
        slots = tuple(candidates[: self.capabilities.max_breakpoints])

        # Physical order: tools → system → message (naturally holds when emit sorts by segment+index)
        # TTL order: the long TTL (1h) must precede the short TTL (5m) — guaranteed by the segment order
        return EmitPlan(slots=slots)

    # ------------------------------------------------------------------
    # emit: translate into an Anthropic /v1/messages request
    # ------------------------------------------------------------------

    def emit(self, ir: TelosIR, plan: EmitPlan) -> Mapping[str, Any]:
        slot_index = _build_slot_index(plan)

        wire_tools = [
            self._render_tool(blk, slot_index.get(("tools", i)))
            for i, blk in enumerate(ir.tools)
        ]

        wire_system = []
        for i, blk in enumerate(ir.system):
            if blk.band is Band.DROP:
                # DROP must come after all BPs; no cache_control attached (§5)
                wire_system.append({"type": "text", "text": str(blk.payload)})
            else:
                wire_system.append(self._render_text_block(blk, slot_index.get(("system", i))))

        wire_messages = []
        for mi, msg in enumerate(ir.messages):
            content = []
            for bi, blk in enumerate(msg.blocks):
                key = ("message", bi, mi)
                slot = slot_index.get(key)
                content.append(self._render_message_block(blk, slot))
            wire_messages.append({"role": msg.role, "content": content})

        request: dict[str, Any] = {
            "model": ir.hints.model or "claude-opus-4-7",
            "tools": wire_tools,
            "system": wire_system,
            "messages": wire_messages,
        }
        return request

    # ------------------------------------------------------------------
    # refresh: max_tokens=0 prewarm
    # ------------------------------------------------------------------

    def refresh(self, ir: TelosIR, plan: EmitPlan) -> None:
        # A real consumer should POST this dict; here we only return the
        # constructed request body and let the upper layer (e.g. a benchmark
        # harness) decide how to send it.
        wire = self.emit(ir, plan)
        wire["max_tokens"] = 0
        wire["stream"] = False
        # tool_choice must be auto; thinking must be disabled — see the Anthropic doc restrictions
        wire["tool_choice"] = {"type": "auto"}
        wire.pop("thinking", None)
        # Real implementation: requests.post(...) ; this demo only constructs it.
        # Attach the constructed request body to an attribute for tests / demo use.
        self.last_refresh_request = wire

    # ------------------------------------------------------------------
    # parse_usage
    # ------------------------------------------------------------------

    def parse_usage(self, response: Mapping[str, Any]) -> UsageReport:
        usage = response.get("usage", {})
        return UsageReport(
            raw_input=int(usage.get("input_tokens", 0)),
            cache_read=int(usage.get("cache_read_input_tokens", 0)),
            cache_write=int(usage.get("cache_creation_input_tokens", 0)),
            output=int(usage.get("output_tokens", 0)),
            raw=usage,
        )

    # ------------------------------------------------------------------
    # Internal rendering helpers
    # ------------------------------------------------------------------

    def _render_tool(self, blk, slot: MarkSlot | None) -> dict[str, Any]:
        # Assume the tool_def payload is already {"name": ..., "input_schema": {...}}.
        # Drop any cache_control the upstream client (e.g. Claude Code) already
        # placed on the block: TELOS is the sole authority on breakpoints, and a
        # carried-over marker would break the tools→system→messages TTL ordering.
        out: dict[str, Any] = _strip_cache_control(blk.payload)
        if slot is not None:
            out["cache_control"] = _cache_control_for(slot)
        return out

    def _render_text_block(self, blk, slot: MarkSlot | None) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "text", "text": str(blk.payload)}
        if slot is not None and blk.band is not Band.DROP:
            out["cache_control"] = _cache_control_for(slot)
        return out

    def _render_message_block(self, blk, slot: MarkSlot | None) -> dict[str, Any]:
        if blk.kind == "text":
            return self._render_text_block(blk, slot)
        if blk.kind == "tool_use":
            out = {"type": "tool_use", **_strip_cache_control(blk.payload)}
            if slot is not None:
                out["cache_control"] = _cache_control_for(slot)
            return out
        if blk.kind == "tool_result":
            out = {"type": "tool_result", **_strip_cache_control(blk.payload)}
            if slot is not None:
                out["cache_control"] = _cache_control_for(slot)
            return out
        if blk.kind == "thinking":
            # thinking blocks cannot have cache_control attached directly; a slot should not land here
            return {"type": "thinking", **_strip_cache_control(blk.payload)}
        if blk.kind == "image":
            # the detail field must be stable (it must come from extra, not be recomputed at emit time)
            out = {"type": "image", "source": blk.payload,
                   **_strip_cache_control(blk.extra)}
            return out
        raise ValueError(f"Unknown block kind: {blk.kind}")


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _last_band_index(blocks, band: Band) -> int | None:
    for i in range(len(blocks) - 1, -1, -1):
        if blocks[i].band is band:
            return i
    return None


def _last_non_drop_index(blocks) -> int | None:
    for i in range(len(blocks) - 1, -1, -1):
        if blocks[i].band is not Band.DROP:
            return i
    return None


def _cache_control_for(slot: MarkSlot) -> dict[str, str]:
    if slot.ttl_class == "long":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}  # default 5m


def _strip_cache_control(payload: Any) -> dict[str, Any]:
    """Shallow-copy a block payload/extra into a dict with any ``cache_control``
    removed.

    Upstream clients (notably Claude Code) place their own ``cache_control``
    breakpoints on tool / tool_use / tool_result blocks. Those markers travel
    through the IR inside the block payload, so when ``emit`` spreads the payload
    (``{"type": "tool_result", **payload}``) the stale marker reappears on the
    wire alongside the breakpoint TELOS itself assigns. With a mix of TTLs that
    violates Anthropic's rule that a 1h block must not follow a 5m block in the
    ``tools → system → messages`` order, yielding HTTP 400. TELOS is the single
    authority on breakpoints, so we drop any inherited marker here.
    """
    if not isinstance(payload, Mapping):
        return dict(payload) if payload else {}
    return {k: v for k, v in payload.items() if k != "cache_control"}


def _build_slot_index(plan: EmitPlan):
    """({segment, index}) or ({segment, block_index, message_index}) → MarkSlot"""
    out: dict[tuple, MarkSlot] = {}
    for s in plan.slots:
        if s.segment == "message":
            out[("message", s.index, s.message_index)] = s
        else:
            out[(s.segment, s.index)] = s
    return out
