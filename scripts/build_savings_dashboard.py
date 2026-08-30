#!/usr/bin/env python3
"""TELOS savings dashboard — aggregates usage_log into "how many tokens / dollars saved".

Input: one or more ``usage_log`` jsonl files (from the proxy or the SDK
transport; schema is documented in docs/User-guide.md §7.1).

Usage::

    telos dashboard --usage-log ~/.telos/usage.jsonl
    # or multiple:
    telos dashboard --usage-log a.jsonl --usage-log b.jsonl --out savings.html

Output: pure static HTML (inline SVG + CSS, zero JS), opens offline.
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from telos.registry import harness_display_name


# ---------------------------------------------------------------------------
# Pricing table (USD / 1M tokens, 2026 public pricing for Anthropic / OpenAI / DeepSeek)
# ---------------------------------------------------------------------------
#
# Anthropic prompt-caching billing rules (2026 public pricing, see
# https://platform.claude.com/docs/en/about-claude/pricing and
# https://platform.claude.com/docs/en/build-with-claude/prompt-caching):
#
#   cache_read price   = 0.10 × input price   (cache hit, 90% off)
#   cache_write (5m)   = 1.25 × input price   (short-TTL write premium +25%)
#   cache_write (1h)   = 2.00 × input price   (long-TTL write premium +100%)
#
# Therefore each model table records both ``cache_write_5m`` and ``cache_write_1h``.
# The legacy field ``cache_write`` is kept as an alias for the 5m price, used
# only when raw_usage has no ``cache_creation.ephemeral_*_input_tokens`` split.
#
# 2026 public Claude pricing:
#   Opus 4.7 / 4.6  : $5  / $25   per 1M tok (input / output)
#   Sonnet 4.6 / 4.5: $3  / $15
#   Haiku 4.5       : $1  / $5
#
# **Fix**: early versions carried over the old Opus 4-series quote of $15 / $75;
# 2026 pricing has dropped to $5 / $25. Haiku likewise moved from $0.80 / $4 to
# $1 / $5. Historical sonnet-4 / opus-4 quotes keep their old values (the pricing
# at the time, USD / 1M) purely so replaying historical logs is not re-priced;
# new calls on 4.6 / 4.7 use the new pricing.

_PRICING: dict[str, dict[str, float]] = {
    # Anthropic — 2026 new pricing
    "claude-opus-4-7":   {"input":  5.00, "cache_read": 0.50, "cache_write_5m":  6.25, "cache_write_1h": 10.00, "output": 25.00},
    "claude-opus-4-6":   {"input":  5.00, "cache_read": 0.50, "cache_write_5m":  6.25, "cache_write_1h": 10.00, "output": 25.00},
    # Opus 4 / 4.5: keep the 2024-2025 historical pricing for replaying old logs
    "claude-opus-4-5":   {"input": 15.00, "cache_read": 1.50, "cache_write_5m": 18.75, "cache_write_1h": 30.00, "output": 75.00},
    "claude-opus-4":     {"input": 15.00, "cache_read": 1.50, "cache_write_5m": 18.75, "cache_write_1h": 30.00, "output": 75.00},
    "claude-sonnet-4-6": {"input":  3.00, "cache_read": 0.30, "cache_write_5m":  3.75, "cache_write_1h":  6.00, "output": 15.00},
    "claude-sonnet-4-5": {"input":  3.00, "cache_read": 0.30, "cache_write_5m":  3.75, "cache_write_1h":  6.00, "output": 15.00},
    "claude-sonnet-4":   {"input":  3.00, "cache_read": 0.30, "cache_write_5m":  3.75, "cache_write_1h":  6.00, "output": 15.00},
    "claude-haiku-4-5":  {"input":  1.00, "cache_read": 0.10, "cache_write_5m":  1.25, "cache_write_1h":  2.00, "output":  5.00},
    "claude-haiku-4":    {"input":  1.00, "cache_read": 0.10, "cache_write_5m":  1.25, "cache_write_1h":  2.00, "output":  5.00},
    # OpenAI: prompt cache is 0.25× input, no cache_write (writes are free)
    "gpt-5":             {"input":  5.00, "cache_read": 1.25, "cache_write_5m":  0.00, "cache_write_1h":  0.00, "output": 15.00},
    "gpt-5.1":           {"input":  5.00, "cache_read": 1.25, "cache_write_5m":  0.00, "cache_write_1h":  0.00, "output": 15.00},
    # DeepSeek: cache hit and miss are billed separately; no 5m/1h distinction
    "deepseek-chat":     {"input":  0.27, "cache_read": 0.07, "cache_write_5m":  0.00, "cache_write_1h":  0.00, "output":  1.10},
    "deepseek-v3":       {"input":  0.27, "cache_read": 0.07, "cache_write_5m":  0.00, "cache_write_1h":  0.00, "output":  1.10},
    # Fallback: estimate at the Sonnet price as a "mid-range" tier
    "_default":          {"input":  3.00, "cache_read": 0.30, "cache_write_5m":  3.75, "cache_write_1h":  6.00, "output": 15.00},
}


def _price_for(model: str) -> dict[str, float]:
    """Fuzzy-match the ``model`` field to the pricing table. Prefix match only,
    longest prefix wins.

    In the returned dict, ``cache_write`` is an alias for the 5m-TTL price (a
    fallback for old call sites that do not pass a breakdown); it also keeps the
    separate ``cache_write_5m`` / ``cache_write_1h`` fields.
    """
    if not model:
        base = _PRICING["_default"]
    else:
        candidates = sorted(
            (k for k in _PRICING if k != "_default" and model.startswith(k)),
            key=len, reverse=True,
        )
        base = _PRICING[candidates[0]] if candidates else _PRICING["_default"]
    # Expose the cache_write alias (== 5m price) for old callers; don't pollute the original dict
    out = dict(base)
    out.setdefault("cache_write", base["cache_write_5m"])
    return out


def _split_cache_write(n: dict[str, int]) -> tuple[int, int]:
    """Split (cache_write_5m, cache_write_1h) out of normalized usage.

    Prefers the raw_usage.cache_creation.ephemeral_{5m,1h}_input_tokens split
    (Anthropic returns it in both SSE and JSON); when missing, counts everything
    as 5m (equivalent to historical behavior). Callers should also place
    raw_usage into the dict (under key ``_breakdown``).
    """
    bd = n.get("_breakdown") if isinstance(n, dict) else None
    if isinstance(bd, Mapping):
        w5 = int(bd.get("ephemeral_5m_input_tokens", 0) or 0)
        w1 = int(bd.get("ephemeral_1h_input_tokens", 0) or 0)
        if w5 + w1 > 0:
            return w5, w1
    return int(n.get("cache_write", 0) or 0), 0


def _cost_usd(model: str, n: dict[str, int]) -> dict[str, float]:
    """Cost breakdown (USD) for a single call under that model's pricing table.

    Total ``cache_write`` = 5m portion × cache_write_5m price + 1h portion ×
    cache_write_1h price; when the breakdown is missing, counts as 5m
    (conservatively underestimates the 1h portion).
    """
    p = _price_for(model)
    w5, w1 = _split_cache_write(n)
    return {
        "raw_input":   p["input"]          * n["raw_input"]   / 1_000_000,
        "cache_read":  p["cache_read"]     * n["cache_read"]  / 1_000_000,
        "cache_write": (p["cache_write_5m"] * w5 + p["cache_write_1h"] * w1) / 1_000_000,
        "output":      p["output"]         * n["output"]      / 1_000_000,
    }


def _counterfactual_cost_usd(model: str, n: dict[str, int]) -> float:
    """The reference price for "if TELOS / cache_control were entirely off".

    In this counterfactual, all prompt tokens (raw_input + cache_read +
    cache_write) are billed at the base input price — no cache_read discount,
    and no cache_write premium. The output price is unchanged.
    """
    p = _price_for(model)
    w5, w1 = _split_cache_write(n)
    cache_write_total = w5 + w1
    prompt_tokens = int(n.get("raw_input", 0)) + int(n.get("cache_read", 0)) + cache_write_total
    return (p["input"] * prompt_tokens + p["output"] * int(n.get("output", 0))) / 1_000_000


def _saved_usd_for_call(model: str, n: dict[str, int]) -> float:
    """The money this call actually saved (or overspent) by using TELOS / cache_control.

    Compared against "cache_control entirely off":
      saved = counterfactual_cost − actual_cost
            = cache_read × (input − cache_read_price)              # cache hit saves money
              + cache_write_5m × (input − cache_write_5m_price)    # short write premium -25%
              + cache_write_1h × (input − cache_write_1h_price)    # long write premium -100%

    For Anthropic the cache_write term is a *negative* contribution (writes cost
    more than the base price), but as long as the cache_read volume is large
    enough the total is still positive. This is an important correction over the
    early implementation: the old version only counted the cache_read discount,
    which overestimated "money saved".
    """
    p = _price_for(model)
    w5, w1 = _split_cache_write(n)
    saved_read  = (p["input"] - p["cache_read"])      * int(n.get("cache_read", 0))
    saved_w5    = (p["input"] - p["cache_write_5m"])  * w5
    saved_w1    = (p["input"] - p["cache_write_1h"])  * w1
    return (saved_read + saved_w5 + saved_w1) / 1_000_000


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

# Chars-per-token ratio for estimating tool_result tokens. **Only a fallback for
# old logs** — new logs carry original/filtered_tokens already computed against
# the real text at filter time in tool_output_reduction.
_CHARS_PER_TOKEN = 4


@dataclass
class _Agg:
    """Accumulates the 4 token buckets + dollars + call count.

    Added fields:
    - ``cache_write_5m`` / ``cache_write_1h``: the split cache_write volumes
    - ``counterfactual_usd``: counterfactual cost (what would be paid with TELOS off)
    - ``tool_orig_chars`` / ``tool_filtered_chars``: tool output char counts
      before/after RTK filtering; ``tool_orig_tokens`` / ``tool_filtered_tokens``
      are token estimates computed against the real text at filter time.
      ``tool_saved_usd`` is the money saved estimated from that (priced with
      cache-hit-rate weighting).
    """
    raw_input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    output: int = 0
    cost_usd: float = 0.0
    saved_usd: float = 0.0
    counterfactual_usd: float = 0.0
    calls: int = 0
    last_ts: float = 0.0
    # RTK tool output filtering
    tool_orig_chars: int = 0
    tool_filtered_chars: int = 0
    tool_orig_tokens: int = 0
    tool_filtered_tokens: int = 0
    tool_blocks_filtered: int = 0
    tool_blocks_seen: int = 0
    tool_rtk_calls: int = 0   # number of calls where RTK actually ran filtering (non-empty reduction)
    tool_saved_usd: float = 0.0

    @property
    def tool_saved_chars(self) -> int:
        return max(0, self.tool_orig_chars - self.tool_filtered_chars)

    @property
    def rtk_status(self) -> str:
        """RTK's state across this group of data — splits "saved nothing" into
        three fundamentally different cases:

        - ``disabled``: RTK never ran on any call (proxy mode does not include rtk).
        - ``idle``: RTK is on, but no tool_result was scanned (the conversation has
          not produced tool output yet).
        - ``nosave``: RTK ran and scanned tool output, but did not save any tokens.
        - ``active``: RTK actually saved tokens.
        """
        if self.tool_rtk_calls == 0:
            return "disabled"
        if self.tool_blocks_seen == 0:
            return "idle"
        if self.tool_saved_tokens == 0:
            return "nosave"
        return "active"

    @property
    def tool_saved_tokens(self) -> int:
        """Tokens saved by RTK filtering.

        Prefers the token estimate computed against the real text at filter time
        in the log (``original_tokens`` / ``filtered_tokens``, see
        output_filter/tokens.py); when old logs lack those two fields, falls back
        to the rough ``saved_chars / _CHARS_PER_TOKEN`` estimate.
        """
        logged = max(0, self.tool_orig_tokens - self.tool_filtered_tokens)
        if logged > 0:
            return logged
        return self.tool_saved_chars // _CHARS_PER_TOKEN

    def add(self, n: dict[str, int], cost: dict[str, float], saved: float,
            counterfactual: float, ts: float,
            tool_reduction: Mapping[str, Any] | None = None,
            tool_saved_usd: float = 0.0) -> None:
        self.raw_input += n["raw_input"]
        self.cache_read += n["cache_read"]
        self.cache_write += n["cache_write"]
        # n["_w5"] / n["_w1"] is the breakdown split by aggregate()
        self.cache_write_5m += int(n.get("_w5", 0))
        self.cache_write_1h += int(n.get("_w1", 0))
        self.output += n["output"]
        self.cost_usd += sum(cost.values())
        self.saved_usd += saved
        self.counterfactual_usd += counterfactual
        self.calls += 1
        if tool_reduction:
            self.tool_orig_chars += int(tool_reduction.get("original_chars", 0) or 0)
            self.tool_filtered_chars += int(tool_reduction.get("filtered_chars", 0) or 0)
            self.tool_orig_tokens += int(tool_reduction.get("original_tokens", 0) or 0)
            self.tool_filtered_tokens += int(tool_reduction.get("filtered_tokens", 0) or 0)
            self.tool_blocks_filtered += int(tool_reduction.get("blocks_filtered", 0) or 0)
            self.tool_blocks_seen += int(tool_reduction.get("blocks_seen", 0) or 0)
            self.tool_rtk_calls += 1
            self.tool_saved_usd += tool_saved_usd
        if ts > self.last_ts:
            self.last_ts = ts

    @property
    def combined_saved_usd(self) -> float:
        """TELOS prefix-cache savings + RTK tool-output filtering savings."""
        return self.saved_usd + self.tool_saved_usd


@dataclass
class Summary:
    total: _Agg = field(default_factory=_Agg)
    by_harness: dict[str, _Agg] = field(default_factory=lambda: defaultdict(_Agg))
    by_model: dict[str, _Agg] = field(default_factory=lambda: defaultdict(_Agg))
    by_session: dict[str, _Agg] = field(default_factory=lambda: defaultdict(_Agg))
    # Toggle dimension: mode ∈ {none, telos, rtk, both, passthrough, rtk-only}
    by_mode: dict[str, _Agg] = field(default_factory=lambda: defaultdict(_Agg))
    # Time series: accumulate cache_read / saved_usd / counterfactual / cost / calls into hour buckets
    timeline: dict[str, dict[str, float]] = field(
        default_factory=lambda: defaultdict(lambda: {
            "cache_read": 0.0,
            "saved_usd": 0.0,
            "counterfactual_usd": 0.0,
            "cost_usd": 0.0,
            "calls": 0.0,
        })
    )
    first_ts: float | None = None
    last_ts: float | None = None
    sessions_seen: set[str] = field(default_factory=set)


def _extract_breakdown(rec: Mapping[str, Any]) -> dict[str, int] | None:
    """Dig the 5m/1h split of cache_creation out of a record.

    Both the proxy and the SDK transport stuff the original ``usage`` field into
    ``raw_usage``; Anthropic returns ``cache_creation.ephemeral_{5m,1h}_input_tokens``
    there. Returns None when missing or not a dict (callers fall back to 5m).
    """
    raw = rec.get("raw_usage")
    if not isinstance(raw, Mapping):
        return None
    cc = raw.get("cache_creation")
    if not isinstance(cc, Mapping):
        return None
    return {
        "ephemeral_5m_input_tokens": int(cc.get("ephemeral_5m_input_tokens", 0) or 0),
        "ephemeral_1h_input_tokens": int(cc.get("ephemeral_1h_input_tokens", 0) or 0),
    }


def aggregate(records: Iterable[dict[str, Any]]) -> Summary:
    s = Summary()
    for rec in records:
        n = rec.get("normalized") or {}
        if not n:
            continue
        breakdown = _extract_breakdown(rec)
        n_dict: dict[str, Any] = {
            "raw_input": int(n.get("raw_input", 0) or 0),
            "cache_read": int(n.get("cache_read", 0) or 0),
            "cache_write": int(n.get("cache_write", 0) or 0),
            "output": int(n.get("output", 0) or 0),
        }
        if breakdown is not None:
            n_dict["_breakdown"] = breakdown
        # Put the 5m/1h split into the dict for _Agg.add to accumulate (convenient for later bar-chart classification)
        w5, w1 = _split_cache_write(n_dict)
        n_dict["_w5"] = w5
        n_dict["_w1"] = w1
        model = rec.get("model") or ""
        # "Breakdown by harness" groups by display name: hermes → "Claude Code".
        harness = harness_display_name(rec.get("harness") or "?")
        session = rec.get("session_id") or "(no-session)"
        mode = rec.get("mode") or "telos"
        ts = float(rec.get("ts") or 0.0)

        cost = _cost_usd(model, n_dict)
        saved = _saved_usd_for_call(model, n_dict)
        counterfactual = _counterfactual_cost_usd(model, n_dict)

        # RTK tool-output filtering savings estimate.
        #
        # Token count: prefers the estimate computed against the real text at
        # filter time in the log (original_tokens / filtered_tokens, see
        # output_filter/tokens.py); when old logs lack those fields, falls back to chars/4.
        #
        # Pricing: cache-hit-rate weighted. If these filtered-out tokens were not
        # filtered, they would enter the prompt — their marginal cost is between
        # the cache_read price (if the prefix cache hits) and the input price (on
        # a miss). Weight by this call's own hit rate h:
        #   eff_price = h × cache_read price + (1 − h) × input price
        # The higher TELOS's hit rate, the closer RTK's savings are to the
        # (cheaper) cache_read basis; with TELOS off (h≈0) it degrades to the full input price.
        tool_reduction = rec.get("tool_output_reduction")
        if not isinstance(tool_reduction, Mapping):
            tool_reduction = None
        tool_saved_usd = 0.0
        if tool_reduction:
            saved_tokens = int(tool_reduction.get("saved_tokens", 0) or 0)
            if saved_tokens <= 0:
                ot = int(tool_reduction.get("original_tokens", 0) or 0)
                ft = int(tool_reduction.get("filtered_tokens", 0) or 0)
                saved_tokens = max(0, ot - ft)
            if saved_tokens <= 0:
                saved_chars = max(0, int(tool_reduction.get("original_chars", 0) or 0)
                                  - int(tool_reduction.get("filtered_chars", 0) or 0))
                saved_tokens = saved_chars // _CHARS_PER_TOKEN
            p = _price_for(model)
            prompt_tokens = (n_dict["raw_input"] + n_dict["cache_read"]
                             + n_dict["cache_write"])
            hit = (n_dict["cache_read"] / prompt_tokens) if prompt_tokens else 0.0
            eff_price = hit * p["cache_read"] + (1.0 - hit) * p["input"]
            tool_saved_usd = eff_price * saved_tokens / 1_000_000

        def _add(agg: _Agg) -> None:
            agg.add(n_dict, cost, saved, counterfactual, ts,
                    tool_reduction=tool_reduction, tool_saved_usd=tool_saved_usd)

        _add(s.total)
        _add(s.by_harness[harness])
        _add(s.by_model[model or "(unknown)"])
        _add(s.by_session[session])
        _add(s.by_mode[mode])
        s.sessions_seen.add(session)

        if ts > 0:
            bucket = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:00")
            tb = s.timeline[bucket]
            tb["cache_read"] += n_dict["cache_read"]
            tb["saved_usd"] += saved
            tb["counterfactual_usd"] += counterfactual
            tb["cost_usd"] += sum(cost.values())
            tb["calls"] += 1
            if s.first_ts is None or ts < s.first_ts:
                s.first_ts = ts
            if s.last_ts is None or ts > s.last_ts:
                s.last_ts = ts
    return s


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _resolve_inputs(patterns: list[str]) -> list[Path]:
    """Supports glob wildcards. Returns a deduplicated, lexicographically sorted path list."""
    paths: list[Path] = []
    for p in patterns:
        expanded = glob.glob(p, recursive=True)
        if not expanded and Path(p).exists():
            expanded = [p]
        for e in expanded:
            paths.append(Path(e))
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in sorted(paths):
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

# Inline TELOS brand logo (see branding/logo.svg). The svg <style> block uses
# {} which would collide with f-string interpolation, so it lives as a plain
# constant and is injected via {LOGO_SVG}.
LOGO_SVG = """\
<svg class="brand-logo" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 460 140" role="img" aria-label="TELOS — Portable Agent Context">
  <title>TELOS</title>
  <style>
    .telos-wm    { fill: #EAF7F9; }
    .telos-tg    { fill: #82A6AB; }
    .telos-stone { stroke: #EAF7F9; }
    .telos-feet  { fill: #4FB3BF; }
  </style>
  <defs>
    <clipPath id="telos-body">
      <path d="M34 122 L34 45 Q34 15 74 15 Q114 15 114 45 L114 122 Z"/>
    </clipPath>
  </defs>
  <g class="telos-feet">
    <rect x="42" y="120" width="22" height="10" rx="5"/>
    <rect x="84" y="120" width="22" height="10" rx="5"/>
  </g>
  <g clip-path="url(#telos-body)">
    <rect x="30" y="10" width="88" height="58" fill="#7FD8E0"/>
    <rect x="30" y="68" width="88" height="27" fill="#4FB3BF"/>
    <rect x="30" y="95" width="88" height="35" fill="#2C5F66"/>
    <g stroke="#1F4A50" stroke-width="3" stroke-linecap="round" opacity="0.30">
      <line x1="49" y1="74" x2="93" y2="74"/>
      <line x1="49" y1="85" x2="84" y2="85"/>
    </g>
    <line x1="30" y1="95" x2="118" y2="95" stroke="#7FD8E0" stroke-width="2" opacity="0.55"/>
  </g>
  <g>
    <circle cx="60" cy="40" r="8.5" fill="#1F4A50"/>
    <circle cx="88" cy="40" r="8.5" fill="#1F4A50"/>
    <circle cx="62.6" cy="37.4" r="2.8" fill="#EAF7F9"/>
    <circle cx="90.6" cy="37.4" r="2.8" fill="#EAF7F9"/>
    <path d="M64 53 Q74 61 84 53" fill="none" stroke="#1F4A50" stroke-width="3.2" stroke-linecap="round"/>
  </g>
  <path class="telos-stone" d="M34 122 L34 45 Q34 15 74 15 Q114 15 114 45 L114 122 Z" fill="none" stroke-width="2.5"/>
  <text class="telos-wm" x="166" y="78" font-family="'Helvetica Neue', Arial, sans-serif" font-size="62" font-weight="700" letter-spacing="3">TELOS</text>
  <text class="telos-tg" x="168" y="104" font-family="'Helvetica Neue', Arial, sans-serif" font-size="12" font-weight="500" letter-spacing="3.4">PORTABLE AGENT CONTEXT</text>
</svg>"""

FAVICON_HREF = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 148 140'%3E"
    "%3Crect x='42' y='120' width='22' height='10' rx='5' fill='%234FB3BF'/%3E"
    "%3Crect x='84' y='120' width='22' height='10' rx='5' fill='%234FB3BF'/%3E"
    "%3CclipPath id='b'%3E%3Cpath d='M34 122 L34 45 Q34 15 74 15 Q114 15 114 45 L114 122 Z'/%3E%3C/clipPath%3E"
    "%3Cg clip-path='url(%23b)'%3E"
    "%3Crect x='30' y='10' width='88' height='58' fill='%237FD8E0'/%3E"
    "%3Crect x='30' y='68' width='88' height='27' fill='%234FB3BF'/%3E"
    "%3Crect x='30' y='95' width='88' height='35' fill='%232C5F66'/%3E"
    "%3Cline x1='30' y1='95' x2='118' y2='95' stroke='%237FD8E0' stroke-width='2' opacity='.55'/%3E"
    "%3C/g%3E"
    "%3Ccircle cx='60' cy='40' r='8.5' fill='%231F4A50'/%3E"
    "%3Ccircle cx='88' cy='40' r='8.5' fill='%231F4A50'/%3E"
    "%3Ccircle cx='62.6' cy='37.4' r='2.8' fill='%23EAF7F9'/%3E"
    "%3Ccircle cx='90.6' cy='37.4' r='2.8' fill='%23EAF7F9'/%3E"
    "%3Cpath d='M64 53 Q74 61 84 53' fill='none' stroke='%231F4A50' stroke-width='3.2' stroke-linecap='round'/%3E"
    "%3Cpath d='M34 122 L34 45 Q34 15 74 15 Q114 15 114 45 L114 122 Z' fill='none' stroke='%23EAF7F9' stroke-width='2.5'/%3E"
    "%3C/svg%3E"
)

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', sans-serif;
  margin: 0;
  padding: 0;
  background: radial-gradient(ellipse at top left, #1a2030 0%, #0a0d12 60%);
  color: #e6edf3;
  min-height: 100vh;
}
.wrap { max-width: 1200px; margin: 0 auto; padding: 32px 24px 64px; }

header { margin-bottom: 32px; }
.header-row {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 16px; flex-wrap: wrap;
}
header h1 { margin: 0 0 6px 0; font-size: 28px; font-weight: 700;
  letter-spacing: -0.01em; display: flex; align-items: center; gap: 12px;
  flex-wrap: wrap;
}
.brand-logo { height: 48px; width: auto; display: block; }
.title-lockup { display: inline-flex; align-items: center; gap: 12px; min-width: 0; }
.title-text { color: #adb6c2; font-size: 18px; font-weight: 500; white-space: nowrap; }

/* ---- header actions ---- */
.header-actions { display: flex; align-items: center; gap: 8px; }
.reset-btn, .nav-btn {
  flex: none; cursor: pointer; font: inherit; font-size: 12.5px;
  font-weight: 600; color: #adb6c2;
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 8px 14px; transition: all .15s ease; white-space: nowrap;
  text-decoration: none;
}
.nav-btn:hover { color: #79c0ff; border-color: #79c0ff66; }
.reset-btn:hover:not(:disabled) {
  color: #f85149; border-color: #f8514966; background: #f8514912;
}
.reset-btn:disabled { opacity: 0.55; cursor: progress; }
header h1 .grad {
  background: linear-gradient(120deg, #79c0ff 0%, #d2a8ff 100%);
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
}
header .sub { color: #7d8590; font-size: 13px; }

/* ---- mode badge ---- */
.mode-badge {
  -webkit-text-fill-color: initial;
  font-family: monospace; font-size: 12px; font-weight: 600;
  letter-spacing: 0.04em; padding: 4px 12px; border-radius: 999px;
  border: 1px solid transparent; text-transform: lowercase;
}

/* ---- hero stats ---- */
.hero {
  display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 24px;
}
.hero-card {
  background: linear-gradient(140deg, #1a2436 0%, #131822 100%);
  border: 1px solid #2a3346; border-radius: 14px;
  padding: 24px 28px; position: relative; overflow: hidden;
}
.hero-card.green::before, .hero-card.purple::before {
  content: ''; position: absolute; right: -40px; top: -40px;
  width: 200px; height: 200px; border-radius: 50%;
  filter: blur(60px); opacity: 0.3;
}
.hero-card.green::before  { background: #3fb950; }
.hero-card.purple::before { background: #d2a8ff; }

.hero-card .label {
  color: #8b949e; font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.08em; font-weight: 500; position: relative;
}
.hero-card .value {
  font-size: 44px; font-weight: 700; font-variant-numeric: tabular-nums;
  margin: 6px 0 4px 0; position: relative; letter-spacing: -0.02em;
}
.hero-card .sub {
  color: #8b949e; font-size: 13px; position: relative;
}
.hero-card.green .value  { color: #56d364; }
.hero-card.purple .value { color: #d2a8ff; }

/* ---- KPI strip ---- */
.kpis {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px; margin-bottom: 32px;
}
.kpi {
  background: #161b22; border: 1px solid #30363d; border-radius: 10px;
  padding: 14px 16px; transition: border-color .15s ease, transform .15s ease;
}
.kpi:hover { border-color: #3d4654; transform: translateY(-2px); }
.kpi .label { color: #7d8590; font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.06em; margin-bottom: 4px; }
.kpi .value { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em; }
.kpi .sub { font-size: 11px; color: #7d8590; margin-top: 2px; }

/* ---- card ---- */
.card {
  background: #0f141c; border: 1px solid #21262d; border-radius: 12px;
  padding: 22px 24px; margin-bottom: 18px;
  transition: border-color .15s ease;
}
.card:hover { border-color: #2d3340; }
.card h2 {
  margin: 0 0 14px 0; font-size: 14px; font-family: monospace;
  color: #7d8590; text-transform: uppercase; letter-spacing: 0.06em;
  font-weight: 500;
}

/* ---- segmented bar ---- */
.seg-bar {
  display: flex; height: 28px; border-radius: 6px; overflow: hidden;
  background: #161b22; margin: 8px 0 14px 0;
}
.seg-bar > span {
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 600; color: #0a0d12;
  font-variant-numeric: tabular-nums;
}
.seg-bar .raw_input   { background: #f0883e; }
.seg-bar .cache_read  { background: #3fb950; }
.seg-bar .cache_write { background: #d29922; }
.seg-bar .output      { background: #79c0ff; }
.seg-bar > span:empty { color: transparent; }

.seg-legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; }
.seg-legend > span { color: #8b949e; }
.seg-legend .sw { display: inline-block; width: 10px; height: 10px;
  border-radius: 2px; vertical-align: middle; margin-right: 5px; }

/* ---- table ---- */
.table-scroll {
  width: 100%; max-width: 100%; overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}
table {
  width: 100%; border-collapse: collapse; font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}
.breakdown-table { min-width: 720px; table-layout: fixed; }
th, td { padding: 8px 10px; border-bottom: 1px solid #21262d; text-align: right;
  vertical-align: middle; }
th { font-weight: 500; color: #7d8590; text-transform: uppercase;
  font-size: 10.5px; letter-spacing: 0.05em; }
th.left, td.left { text-align: left; }
th.left { width: 34%; }
td.left {
  font-family: monospace; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis;
}
tr:hover td { background: #131822; }

.bar-cell {
  position: relative; width: 150px;
  background: #161b22; border-radius: 3px; overflow: hidden;
}
.bar-cell .fill {
  position: absolute; left: 0; top: 0; bottom: 0;
  background: linear-gradient(90deg, #3fb950 0%, #56d364 100%);
  border-radius: 3px;
}
.bar-cell .label-overlay {
  position: relative; padding: 2px 8px; color: #e6edf3;
  font-size: 11px; font-weight: 500;
}

/* ---- timeline ---- */
.timeline { margin-top: 4px; }
.timeline svg { display: block; }

.muted { color: #7d8590; }
.gold  { color: #d29922; }
.green { color: #56d364; }
.blue  { color: #79c0ff; }
.lilac { color: #d2a8ff; }

.footer { margin-top: 40px; color: #4f5862; font-size: 11px; text-align: center; }

/* ---- welcome / empty state ------------------------------------------- */
@keyframes fade-up {
  from { opacity: 0; transform: translateY(14px); }
  to   { opacity: 1; transform: translateY(0); }
}
@keyframes orb-pulse {
  0%, 100% { transform: scale(1);    box-shadow: 0 0 0 0 #79c0ff55; }
  50%      { transform: scale(1.06); box-shadow: 0 0 0 22px #79c0ff00; }
}
@keyframes orb-spin { to { transform: rotate(360deg); } }
@keyframes dots {
  0%, 20%  { content: '·'; }
  40%      { content: '··'; }
  60%, 100%{ content: '···'; }
}
@keyframes sweep {
  0%   { transform: translateX(-100%); }
  100% { transform: translateX(100%); }
}

.welcome {
  max-width: 620px; margin: 7vh auto 0; text-align: center;
}
.welcome > * { animation: fade-up .6s ease both; }
.welcome > :nth-child(2) { animation-delay: .12s; }
.welcome > :nth-child(3) { animation-delay: .24s; }
.welcome > :nth-child(4) { animation-delay: .36s; }
.welcome > :nth-child(5) { animation-delay: .48s; }

.orb {
  width: 92px; height: 92px; margin: 0 auto 26px;
  border-radius: 50%; position: relative;
  background: radial-gradient(circle at 32% 30%, #add2ff 0%, #79c0ff 38%, #6f4bd8 100%);
  animation: orb-pulse 2.6s ease-in-out infinite;
}
.orb::before {
  content: ''; position: absolute; inset: -9px; border-radius: 50%;
  border: 1.5px solid transparent; border-top-color: #d2a8ff;
  border-right-color: #79c0ff66;
  animation: orb-spin 3.4s linear infinite;
}
.orb::after {
  content: 'T'; position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 40px; font-weight: 800; color: #0a0d12;
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
}

.welcome h1 {
  font-size: 30px; font-weight: 700; letter-spacing: -0.01em; margin: 0 0 10px;
}
.welcome h1 .grad {
  background: linear-gradient(120deg, #79c0ff 0%, #d2a8ff 100%);
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
}
.welcome .intro {
  color: #adb6c2; font-size: 14.5px; line-height: 1.65; margin: 0 0 26px;
}
.welcome .intro b { color: #e6edf3; font-weight: 600; }

.feature-row {
  display: flex; gap: 12px; justify-content: center; margin-bottom: 28px;
  flex-wrap: wrap;
}
.feature {
  flex: 1 1 150px; max-width: 180px;
  background: #0f141c; border: 1px solid #21262d; border-radius: 12px;
  padding: 16px 14px;
}
.feature .ico { font-size: 22px; }
.feature .ft  { font-size: 12.5px; font-weight: 600; color: #e6edf3;
  margin: 8px 0 3px; }
.feature .fd  { font-size: 11.5px; color: #7d8590; line-height: 1.45; }

.status-card {
  background: linear-gradient(140deg, #1a2436 0%, #131822 100%);
  border: 1px solid #2a3346; border-radius: 14px;
  padding: 20px 24px; position: relative; overflow: hidden;
}
.status-card::after {
  content: ''; position: absolute; left: 0; right: 0; bottom: 0; height: 2px;
  background: linear-gradient(90deg, transparent, #79c0ff, transparent);
  animation: sweep 1.8s ease-in-out infinite;
}
.status-card .status-title {
  font-size: 15px; font-weight: 600; color: #e6edf3;
  display: flex; align-items: center; justify-content: center; gap: 8px;
}
.status-card .status-title .pip {
  width: 8px; height: 8px; border-radius: 50%; background: #56d364;
  animation: orb-pulse 1.8s ease-in-out infinite;
}
.status-card .status-title .pip-idle {
  background: #d29922; animation: none;
}
.status-card .status-title .dots::after {
  content: '···'; animation: dots 1.4s steps(1) infinite;
  display: inline-block; width: 1.4em; text-align: left;
}
.status-card .status-body {
  color: #8b949e; font-size: 12.5px; margin-top: 8px; line-height: 1.6;
}
.status-card code {
  background: #0a0d12; border: 1px solid #21262d; border-radius: 4px;
  padding: 1px 6px; font-size: 11.5px; color: #adb6c2;
}
.welcome .hint { color: #4f5862; font-size: 11px; margin-top: 20px; }
"""


def _fmt_int(n: int | float) -> str:
    return f"{int(n):,}"


def _fmt_tokens(n: int) -> str:
    """7,453,210 → 7.45M / 1234 → 1.23K"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K"
    return f"{n:,}"


def _fmt_usd(x: float) -> str:
    if x >= 100:
        return f"${x:,.2f}"
    if x >= 1:
        return f"${x:.2f}"
    if x >= 0.01:
        return f"${x:.3f}"
    return f"${x:.4f}"


def _fmt_pct(x: float, decimals: int = 1) -> str:
    return f"{100 * x:.{decimals}f}%"


def _render_seg_bar(parts: list[tuple[str, int, str]]) -> str:
    """parts: [(class, n, hover_label), ...]"""
    total = max(sum(p[1] for p in parts), 1)
    segs = []
    for cls, n, label in parts:
        pct = 100 * n / total
        text = _fmt_tokens(n) if pct >= 5 else ""
        segs.append(
            f'<span class="{cls}" style="width:{pct:.2f}%" title="{html.escape(label)}: {n:,}">{text}</span>'
        )
    return '<div class="seg-bar">' + "".join(segs) + "</div>"


def _render_breakdown_table(label: str, data: dict[str, _Agg],
                             *, key_label: str, max_rows: int = 12) -> str:
    if not data:
        return ""
    rows_sorted = sorted(data.items(), key=lambda kv: -kv[1].cache_read)
    if len(rows_sorted) > max_rows:
        rows_sorted = rows_sorted[:max_rows]
    if not rows_sorted:
        return ""

    max_saved = max((a.saved_usd for _, a in rows_sorted), default=0.0)

    rows = []
    for key, a in rows_sorted:
        # Fix: the hit% denominator must include cache_write (Anthropic's
        # input_tokens only counts the part that missed and was not cache-written,
        # so "total prompt tokens" = raw + read + write)
        prompt_tokens = a.cache_read + a.raw_input + a.cache_write
        share = (a.cache_read / prompt_tokens) if prompt_tokens else 0.0
        bar_pct = (100 * a.saved_usd / max_saved) if max_saved > 0 else 0.0
        rows.append(
            f"<tr>"
            f'<td class="left">{html.escape(str(key))}</td>'
            f"<td>{_fmt_int(a.calls)}</td>"
            f"<td>{_fmt_tokens(a.raw_input)}</td>"
            f'<td class="green">{_fmt_tokens(a.cache_read)}</td>'
            f"<td>{_fmt_tokens(a.cache_write)}</td>"
            f"<td>{_fmt_pct(share)}</td>"
            f'<td class="bar-cell">'
            f'<span class="fill" style="width:{bar_pct:.1f}%"></span>'
            f'<span class="label-overlay">{_fmt_usd(a.saved_usd)}</span>'
            f"</td>"
            f"</tr>"
        )
    return f"""
<div class="card">
  <h2>{html.escape(label)}</h2>
  <div class="table-scroll">
  <table class="breakdown-table">
    <thead><tr>
      <th class="left">{html.escape(key_label)}</th>
      <th>calls</th>
      <th>raw_input</th>
      <th>cache_read</th>
      <th>cache_write</th>
      <th>hit%</th>
      <th class="left">saved $</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  </div>
</div>
"""


# mode label → display color / description
_MODE_META: dict[str, tuple[str, str]] = {
    "both":        ("#d2a8ff", "TELOS prefix cache + RTK tool filtering"),
    "telos":       ("#3fb950", "TELOS prefix cache only"),
    "rtk":         ("#f0883e", "RTK tool filtering only"),
    "rtk-only":    ("#f0883e", "RTK tool filtering only"),
    "none":        ("#7d8590", "pure passthrough, no optimization"),
    "passthrough": ("#7d8590", "pure passthrough / pipeline degraded"),
}


def _mode_color(mode: str) -> str:
    return _MODE_META.get(mode, ("#79c0ff", ""))[0]


def _dominant_mode(by_mode: dict[str, _Agg]) -> tuple[str, int]:
    """The mode that ran the most calls, plus how many *other* modes appear.

    Used for the header badge: a savings dashboard is almost always a single
    mode, but a usage_log can mix several — the badge shows the dominant one
    and a ``+N more`` hint when it does.
    """
    if not by_mode:
        return "telos", 0
    ranked = sorted(by_mode.items(), key=lambda kv: -kv[1].calls)
    return ranked[0][0], len(ranked) - 1


def _render_timeline_svg(timeline: dict[str, dict[str, float]]) -> str:
    if not timeline:
        return '<p class="muted">no timestamped data</p>'

    items = sorted(timeline.items())
    W = 1100
    H = 180
    pad_l, pad_r, pad_t, pad_b = 50, 14, 12, 24
    plot_w = W - pad_l - pad_r
    plot_h = H - pad_t - pad_b

    n = len(items)
    max_saved = max((it[1]["saved_usd"] for it in items), default=0.0) or 1.0
    max_cache = max((it[1]["cache_read"] for it in items), default=0.0) or 1.0

    # cache_read bars
    bar_w = plot_w / n * 0.7
    gap = plot_w / n - bar_w
    bars: list[str] = []
    for i, (_, v) in enumerate(items):
        h_px = plot_h * (v["cache_read"] / max_cache)
        x = pad_l + i * (plot_w / n) + gap / 2
        y = pad_t + plot_h - h_px
        bars.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" '
            f'height="{h_px:.2f}" fill="#3fb950" opacity="0.55" rx="2">'
            f'<title>{items[i][0]}\ncache_read: {int(v["cache_read"]):,}</title>'
            f'</rect>'
        )

    # saved_usd line
    pts = []
    for i, (_, v) in enumerate(items):
        x = pad_l + i * (plot_w / n) + (plot_w / n) / 2
        y = pad_t + plot_h - plot_h * (v["saved_usd"] / max_saved)
        pts.append((x, y))
    line = "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in pts)

    # y-axis labels (left = cache_read, right = saved)
    y_labels = []
    for frac, label in [(1.0, max_cache), (0.5, max_cache / 2), (0.0, 0)]:
        y = pad_t + plot_h * (1 - frac)
        y_labels.append(
            f'<text x="{pad_l - 6:.1f}" y="{y + 3:.1f}" text-anchor="end" '
            f'font-size="9" fill="#7d8590">{_fmt_tokens(int(label))}</text>'
        )
        y_labels.append(
            f'<line x1="{pad_l:.1f}" y1="{y:.1f}" x2="{W - pad_r:.1f}" y2="{y:.1f}" '
            f'stroke="#21262d" stroke-width="1"/>'
        )

    # x-axis labels (3 evenly-spaced)
    x_labels = []
    for i in (0, n // 2, n - 1) if n >= 3 else range(n):
        x = pad_l + i * (plot_w / n) + (plot_w / n) / 2
        x_labels.append(
            f'<text x="{x:.1f}" y="{H - 6:.1f}" text-anchor="middle" '
            f'font-size="9" fill="#7d8590">{html.escape(items[i][0][-13:])}</text>'
        )

    # markers on the line
    dots = "".join(
        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="#d2a8ff">'
        f'<title>{items[i][0]}\nsaved: {_fmt_usd(items[i][1]["saved_usd"])}</title>'
        f'</circle>'
        for i, (x, y) in enumerate(pts)
    )

    return f"""
<svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMid meet">
  {''.join(y_labels)}
  {''.join(bars)}
  <path d="{line}" stroke="#d2a8ff" stroke-width="1.8" fill="none"/>
  {dots}
  {''.join(x_labels)}
  <text x="{pad_l}" y="{pad_t - 2}" font-size="9" fill="#3fb950" font-family="monospace">
    cache_read (bars)
  </text>
  <text x="{pad_l + 130}" y="{pad_t - 2}" font-size="9" fill="#d2a8ff" font-family="monospace">
    saved $ (line)
  </text>
</svg>
"""


def render_dashboard(
    summary: Summary,
    sources: list[Path],
    *,
    refresh_seconds: int | None = None,
) -> str:
    total = summary.total
    # Fix: the hit_rate denominator must include cache_write (Anthropic's
    # input_tokens field excludes cached_creation / cached_read, so
    # "total prompt tokens" = the sum of all three)
    prompt_tokens_total = total.raw_input + total.cache_read + total.cache_write
    hit_rate = total.cache_read / prompt_tokens_total if prompt_tokens_total else 0.0
    # Counterfactual cost is the denominator for "% saved" only — it is the
    # estimated total price with cache_control turned off, accumulated per call
    # from _counterfactual_cost_usd. It is no longer surfaced as its own view.
    counterfactual_cost = total.counterfactual_usd or (total.cost_usd + total.saved_usd)
    saved_share = total.saved_usd / counterfactual_cost if counterfactual_cost else 0.0

    # Current optimization mode — shown as a header badge instead of a per-mode
    # breakdown table.
    mode, extra_modes = _dominant_mode(summary.by_mode)
    mode_color = _mode_color(mode)
    mode_desc = _MODE_META.get(mode, ("", ""))[1]
    mode_extra = (f'<span class="muted" style="font-size:11px;margin-left:6px">'
                  f'+{extra_modes} more</span>') if extra_modes else ""
    mode_badge = (
        f'<span class="mode-badge" title="{html.escape(mode_desc)}" '
        f'style="color:{mode_color};border-color:{mode_color}66;'
        f'background:{mode_color}1a">{html.escape(mode)}</span>{mode_extra}'
    )

    if summary.first_ts and summary.last_ts:
        span = summary.last_ts - summary.first_ts
        if span < 60:
            span_s = f"{int(span)} s"
        elif span < 3600:
            span_s = f"{span / 60:.1f} min"
        elif span < 86400:
            span_s = f"{span / 3600:.1f} h"
        else:
            span_s = f"{span / 86400:.1f} d"
    else:
        span_s = "—"

    n_sessions = len(summary.sessions_seen)
    n_calls = total.calls

    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sources_html = "<br>".join(f"<code>{html.escape(str(s))}</code>" for s in sources)

    # —— token mix (actual values) ——
    seg_bar_with = _render_seg_bar([
        ("raw_input",   total.raw_input,   "raw_input"),
        ("cache_read",  total.cache_read,  "cache_read"),
        ("cache_write", total.cache_write, "cache_write"),
        ("output",      total.output,      "output"),
    ])

    by_harness = _render_breakdown_table(
        "Breakdown by harness", summary.by_harness, key_label="harness"
    )
    by_model = _render_breakdown_table(
        "Breakdown by model", summary.by_model, key_label="model"
    )
    by_session = _render_breakdown_table(
        "Top sessions by saved $", summary.by_session,
        key_label="session_id", max_rows=15
    )

    timeline_svg = _render_timeline_svg(summary.timeline)

    # 5m / 1h cache_write split (non-zero only when the
    # raw_usage.cache_creation.* split is available; otherwise everything counts as 5m fallback)
    w5_total = total.cache_write_5m
    w1_total = total.cache_write_1h

    refresh_tag = (
        f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
        if refresh_seconds and refresh_seconds > 0 else ""
    )
    refresh_note = (
        f' · auto-refresh {int(refresh_seconds)}s'
        if refresh_seconds and refresh_seconds > 0 else ""
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
{refresh_tag}
<title>TELOS · Token Savings</title>
<link rel="icon" type="image/svg+xml" href="{FAVICON_HREF}">
<style>{CSS}</style>
</head><body>
<div class="wrap">

<header>
  <div class="header-row">
    <div>
      <h1><span class="title-lockup">{LOGO_SVG}<span class="title-text">Token Savings</span></span>{mode_badge}</h1>
      <div class="sub">
        {n_calls:,} calls · {n_sessions:,} sessions · span {span_s}
        · generated {ts_now}{refresh_note}
      </div>
    </div>
    <div class="header-actions">
      <a class="nav-btn" href="/">Overview</a>
      <a class="nav-btn" href="/tasks">Long Tasks</a>
      <a class="nav-btn" href="/knowledge">Wiki</a>
      <a class="nav-btn" href="/traces">Traces</a>
      <a class="nav-btn" href="/developer">Developer</a>
      <button class="reset-btn" type="button" onclick="telosReset(this)"
              title="Clear the usage log and zero this dashboard">
        ⟲ Reset
      </button>
    </div>
  </div>
</header>

<section class="hero">
  <div class="hero-card green">
    <div class="label">tokens saved (cache hits)</div>
    <div class="value">{_fmt_tokens(total.cache_read)}</div>
    <div class="sub">
      cache hits are <b class="green">{_fmt_pct(hit_rate)}</b> of total prompt tokens
      ·  absolute <code>{_fmt_int(total.cache_read)}</code> tokens
    </div>
  </div>
  <div class="hero-card purple">
    <div class="label">total cost saved (estimated)</div>
    <div class="value">{_fmt_usd(total.saved_usd)}</div>
    <div class="sub">
      TELOS prefix cache · <b class="lilac">{_fmt_pct(saved_share)}</b>
      off counterfactual cost
    </div>
  </div>
</section>

<div class="kpis">
  <div class="kpi"><div class="label">total calls</div>
    <div class="value">{_fmt_int(total.calls)}</div></div>
  <div class="kpi"><div class="label">unique sessions</div>
    <div class="value">{_fmt_int(n_sessions)}</div></div>
  <div class="kpi"><div class="label">raw input</div>
    <div class="value gold">{_fmt_tokens(total.raw_input)}</div>
    <div class="sub">{_fmt_int(total.raw_input)}</div></div>
  <div class="kpi"><div class="label">cache read</div>
    <div class="value green">{_fmt_tokens(total.cache_read)}</div>
    <div class="sub">{_fmt_int(total.cache_read)}</div></div>
  <div class="kpi"><div class="label">cache write</div>
    <div class="value gold">{_fmt_tokens(total.cache_write)}</div>
    <div class="sub">5m {_fmt_tokens(w5_total)} · 1h {_fmt_tokens(w1_total)}</div></div>
  <div class="kpi"><div class="label">output</div>
    <div class="value blue">{_fmt_tokens(total.output)}</div>
    <div class="sub">{_fmt_int(total.output)}</div></div>
</div>

<div class="card">
  <h2>Token mix</h2>
  {seg_bar_with}
  <div class="seg-legend">
    <span><span class="sw" style="background:#f0883e"></span>raw_input · {_fmt_tokens(total.raw_input)}</span>
    <span><span class="sw" style="background:#3fb950"></span>cache_read · {_fmt_tokens(total.cache_read)}</span>
    <span><span class="sw" style="background:#d29922"></span>cache_write · {_fmt_tokens(total.cache_write)}</span>
    <span><span class="sw" style="background:#79c0ff"></span>output · {_fmt_tokens(total.output)}</span>
  </div>
</div>

<div class="card timeline">
  <h2>Activity over time (aggregated by hour)</h2>
  {timeline_svg}
</div>

{by_harness}
{by_model}
{by_session}

<div class="footer">
  Data sources · {sources_html}<br>
  Pricing table = Anthropic / OpenAI / DeepSeek 2026 public pricing (USD / 1M tokens); unrecognized models are estimated at the Sonnet price tier.
  <br>cache_write split by raw_usage.cache_creation.ephemeral_{{5m,1h}}_input_tokens; when missing, all counted at the 5m price (conservative).
</div>

</div>
<script>
function telosReset(btn) {{
  if (!confirm('Clear the usage log and zero this dashboard?\\n\\n'
      + 'The current log is rotated to a timestamped .bak file, '
      + 'so the data stays recoverable.')) return;
  var label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Resetting…';
  fetch('/__telos/control/reset', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: '{{}}'
  }})
  .then(function (r) {{ return r.json().then(function (j) {{
      if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
      return j;
  }}); }})
  .then(function () {{ location.reload(); }})
  .catch(function (e) {{
    btn.disabled = false; btn.textContent = label;
    alert('Reset failed: ' + e.message);
  }});
}}
</script>
</body></html>
"""



def render_from_usage_log(
    path: Path | None,
    *,
    refresh_seconds: int | None = None,
) -> str:
    """Live-server helper: read from a single usage_log file → render.

    Specifically for the proxy's embedded endpoint: always returns a displayable
    HTML page, even when the log is missing / empty. When missing or empty it
    still carries the ``refresh_tag``, and the browser waits for the next round itself.
    """
    if path is None or not path.exists():
        return _render_empty(
            "No usage_log configured.",
            "Restart the proxy with --usage-log <path> to enable.",
            refresh_seconds=refresh_seconds,
        )
    records = list(_read_jsonl(path))
    if not records:
        return _render_empty(
            "Waiting for first request…",
            f"Watching <code>{html.escape(str(path))}</code>. "
            "Send a request through the proxy and this page will populate.",
            refresh_seconds=refresh_seconds,
        )
    summary = aggregate(records)
    return render_dashboard(summary, [path], refresh_seconds=refresh_seconds)


def _render_empty(title: str, body: str, *,
                   refresh_seconds: int | None) -> str:
    """Welcome / empty-state HTML — a friendly intro shown before any data
    has arrived. Keeps auto-refresh so the page swaps itself for the real
    dashboard as soon as the first request flows through the proxy."""
    refresh_tag = (
        f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
        if refresh_seconds and refresh_seconds > 0 else ""
    )
    refresh_hint = (
        f"This page refreshes itself every {int(refresh_seconds)}s — "
        "leave it open and it will fill in on its own."
        if refresh_seconds and refresh_seconds > 0 else
        "Reload this page once traffic has flowed through the proxy."
    )
    # `title` is a short status line; `body` may contain inline HTML (e.g. <code>).
    # A trailing ellipsis means "in progress" → swap it for animated dots.
    waiting = title.rstrip().endswith(("…", "..."))
    title_text = title.rstrip().rstrip("…").rstrip(".").rstrip() if waiting else title
    dots = '<span class="dots"></span>' if waiting else ''
    pip_cls = "pip" if waiting else "pip pip-idle"
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
{refresh_tag}
<title>TELOS · Token Savings</title>
<link rel="icon" type="image/svg+xml" href="{FAVICON_HREF}">
<style>{CSS}</style>
</head><body><div class="wrap">
<div class="welcome">

  <div class="orb" role="img" aria-label="TELOS"></div>

  <h1>Welcome to <span class="grad">TELOS</span></h1>

  <p class="intro">
    I sit in front of your LLM proxy and quietly <b>reshape every prompt</b>
    so the model re-reads as little as possible. This dashboard is where
    I'll show you <b>how many tokens — and how much money — that saves</b>.
  </p>

  <div class="feature-row">
    <div class="feature">
      <div class="ico">🧩</div>
      <div class="ft">Prompt caching</div>
      <div class="fd">Stable prefixes get reused instead of re-sent.</div>
    </div>
    <div class="feature">
      <div class="ico">✂️</div>
      <div class="ft">Tool trimming</div>
      <div class="fd">Idle tool schemas are folded out of the wire.</div>
    </div>
    <div class="feature">
      <div class="ico">📊</div>
      <div class="ft">Live savings</div>
      <div class="fd">Every call tallied into real-dollar totals.</div>
    </div>
  </div>

  <div class="status-card">
    <div class="status-title">
      <span class="{pip_cls}"></span>{html.escape(title_text)}{dots}
    </div>
    <div class="status-body">{body}</div>
  </div>

  <p class="hint"><a class="nav-btn" href="/">Open Overview</a></p>
  <p class="hint">{refresh_hint}</p>

</div></div></body></html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="telos.scripts.build_savings_dashboard",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--usage-log", action="append", required=True,
                    help="usage_log jsonl path or glob, repeatable")
    ap.add_argument("--out", default="telos_savings.html",
                    help="output HTML path (default: ./telos_savings.html)")
    args = ap.parse_args(argv)

    sources = _resolve_inputs(args.usage_log)
    if not sources:
        raise SystemExit("no usage_log files matched")

    records: list[dict[str, Any]] = []
    for src in sources:
        records.extend(_read_jsonl(src))

    if not records:
        raise SystemExit(f"all {len(sources)} usage_log file(s) were empty / malformed")

    summary = aggregate(records)
    html_doc = render_dashboard(summary, sources)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_doc, encoding="utf-8")
    print(f"[savings-dashboard] wrote {out}")
    print(f"  {len(records):,} records · {summary.total.calls:,} calls "
          f"· {len(summary.sessions_seen):,} sessions")
    print(f"  saved: {_fmt_tokens(summary.total.cache_read)} cache-read tokens "
          f"· TELOS {_fmt_usd(summary.total.saved_usd)}"
          f" + RTK {_fmt_usd(summary.total.tool_saved_usd)}"
          f" = {_fmt_usd(summary.total.combined_saved_usd)}")
    print(f"  open with:  open {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
