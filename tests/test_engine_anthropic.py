"""Anthropic breakpoint planning: capability cap, R7 trim, R2 mid-anchor.

``test_smoke.py`` checks ``plan_marks`` on a single short request (≤4 slots,
TTL ordering). This file forces the >4-candidate trim and the long-session
mid-rolling anchor, which were otherwise uncovered. See
docs/ARCHITECTURE.md §7.3 / §13 (R2, R7).
"""

from __future__ import annotations

from telos.engine.anthropic import AnthropicAdapter, _MID_ANCHOR_STRIDE
from telos.ir import Band, TelosBlock, TelosHints, TelosIR, TelosMessage


def _msg(band: Band = Band.FOLD) -> TelosMessage:
    return TelosMessage(role="user", blocks=(TelosBlock(id="b", band=band, kind="text", payload="x"),))


def _ir(*, n_msgs: int, with_tools: bool = True, with_fold_system: bool = True) -> TelosIR:
    tools = (TelosBlock(id="t0", band=Band.PIN, kind="tool_def",
                        payload={"name": "t0", "input_schema": {"type": "object"}}),) if with_tools else ()
    system = [TelosBlock(id="s-pin", band=Band.PIN, kind="text", payload="sys")]
    if with_fold_system:
        system.append(TelosBlock(id="s-fold", band=Band.FOLD, kind="text", payload="doc", ref_slug="d"))
    return TelosIR(
        session_id="s", tools=tools, system=tuple(system),
        messages=tuple(_msg() for _ in range(n_msgs)),
        ref_pool={}, hints=TelosHints(engine="anthropic"),
    )


def test_capability_caps_at_four_breakpoints() -> None:
    assert AnthropicAdapter().capabilities.max_breakpoints == 4


def test_trim_keeps_four_and_drops_latest_turn_bp_x() -> None:
    # tools + system PIN + system FOLD + long history + last-msg non-DROP
    # => 5 candidates (BP-T,BP-S,BP-R,BP-mid,BP-X). R7 keeps the four highest
    # priority (pin-tools > pin-system > ref-pool > rolling-mid) and drops the
    # latest-turn anchor (BP-X).
    plan = AnthropicAdapter().plan_marks(_ir(n_msgs=_MID_ANCHOR_STRIDE + 1))
    names = [s.name for s in plan.slots]
    assert len(names) == 4
    assert "BP-X" not in names
    assert set(names) == {"BP-T", "BP-S", "BP-R", "BP-mid"}


def test_mid_anchor_only_for_long_sessions() -> None:
    short = AnthropicAdapter().plan_marks(_ir(n_msgs=3))
    assert "BP-mid" not in [s.name for s in short.slots]

    n = _MID_ANCHOR_STRIDE + 5
    long = AnthropicAdapter().plan_marks(_ir(n_msgs=n))
    mid = [s for s in long.slots if s.name == "BP-mid"]
    assert mid and mid[0].message_index == n - _MID_ANCHOR_STRIDE


def test_short_session_stays_within_cap() -> None:
    plan = AnthropicAdapter().plan_marks(_ir(n_msgs=2))
    assert len(plan.slots) <= 4
