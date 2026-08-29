#!/usr/bin/env python3
"""TELOS developer page — live view of session structure / pin·fold·drop region
changes / cache fields returned by the API / tool-call statistics.

Unlike ``build_savings_dashboard`` (the user-facing savings dashboard): this page
is for developers, its content is the **current in-memory state**, and every GET
re-renders from ``_SessionInspector``.

Page elements:
- Overview: a list of all sessions, sorted by most recent activity; click to enter details
- Details:
  * (raw_input, cache_read, cache_write, output, plan slots) of the last N calls
  * Current IR layout: block counts + char counts for the three segments
    (tools / system / messages) × three bands (pin/fold/drop), with
    "previous round → this round" arrows
  * The band sequence of each message (role + blocks listed by mi)
  * Tool-call statistics: invocation count / total argument bytes / total result
    bytes / largest result block
  * Raw cache fields returned by the last API call (cache_creation.ephemeral_*,
    cache_read_input_tokens, etc.)

Depends on the in-memory view provided by ``telos.proxy.server._SessionInspector``.
Fully self-contained HTML, reusing the savings dashboard's CSS color palette.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, TYPE_CHECKING

from telos.registry import harness_display_name

if TYPE_CHECKING:
    from telos.proxy.inspector import SessionInspector
    from telos.proxy.server import _SessionRegistry


class _RegistryLike(Protocol):
    """The dashboard only needs ``__len__``; using a Protocol avoids a hard
    dependency on server.py (server.py imports aiohttp, and unit tests should
    not be dragged into that)."""
    def __len__(self) -> int: ...


# Compatibility with old type annotations
_SessionInspector = "SessionInspector"
_SessionRegistry = _RegistryLike


_BANDS = ("pin", "fold", "drop")
_BAND_COLOR = {
    "pin":  "#d29922",
    "fold": "#58a6ff",
    "drop": "#7d8590",
}


CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', sans-serif;
  margin: 0; padding: 0;
  background: radial-gradient(ellipse at top left, #1a2030 0%, #0a0d12 60%);
  color: #e6edf3; min-height: 100vh;
}
.wrap { max-width: 1280px; margin: 0 auto; padding: 24px; }
header { margin-bottom: 22px; }
header h1 { margin: 0 0 4px 0; font-size: 22px; font-weight: 700;
  background: linear-gradient(120deg, #79c0ff 0%, #d2a8ff 100%);
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
}
header .sub { color: #7d8590; font-size: 12px; }
a { color: #79c0ff; text-decoration: none; }
a:hover { text-decoration: underline; }
.card {
  background: #0f141c; border: 1px solid #21262d; border-radius: 10px;
  padding: 16px 20px; margin-bottom: 14px;
}
.card h2 { margin: 0 0 12px 0; font-size: 13px; color: #7d8590;
  text-transform: uppercase; letter-spacing: 0.06em; font-weight: 500;
  font-family: monospace; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px; margin-bottom: 18px; }
.kpi { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 10px 14px; }
.kpi .label { color: #7d8590; font-size: 10.5px; text-transform: uppercase;
  letter-spacing: 0.05em; margin-bottom: 3px; }
.kpi .value { font-size: 19px; font-weight: 600; font-variant-numeric: tabular-nums; }
.kpi .sub { color: #7d8590; font-size: 11px; margin-top: 1px; }

table { width: 100%; border-collapse: collapse; font-size: 12px;
  font-variant-numeric: tabular-nums; }
th, td { padding: 6px 8px; border-bottom: 1px solid #21262d; text-align: right;
  vertical-align: middle; }
th { font-weight: 500; color: #7d8590; text-transform: uppercase;
  font-size: 10px; letter-spacing: 0.05em; }
th.left, td.left { text-align: left; }
td.left { font-family: monospace; }
tr:hover td { background: #131822; }

.pin   { color: #d29922; }
.fold  { color: #58a6ff; }
.drop  { color: #7d8590; }
.green { color: #56d364; }
.purple { color: #d2a8ff; }
.muted { color: #7d8590; }

.tabs { display: flex; gap: 0; background: #161b22; border-radius: 999px;
  padding: 4px; margin-bottom: 14px; border: 1px solid #30363d;
  width: fit-content; }
.tabs a { display: inline-block; padding: 6px 16px; border-radius: 999px;
  font-size: 12px; font-weight: 500; color: #8b949e; text-decoration: none; }
.tabs a.active { background: linear-gradient(120deg, #d2a8ff 0%, #79c0ff 100%);
  color: #0a0d12; }

.stack-bar { display: flex; height: 14px; border-radius: 3px; overflow: hidden;
  background: #161b22; margin: 4px 0 4px 0; }
.stack-bar > span { display: block; height: 100%; }
.stack-bar .pin   { background: #d29922; }
.stack-bar .fold  { background: #58a6ff; }
.stack-bar .drop  { background: #7d8590; }

.delta { font-family: monospace; font-size: 10.5px; }
.delta.up   { color: #f85149; }
.delta.down { color: #56d364; }

.msg-row { display: grid; grid-template-columns: 60px 70px 1fr; gap: 8px;
  padding: 4px 0; border-bottom: 1px dashed #21262d; font-size: 11.5px; }
.msg-row .role { font-family: monospace; color: #d2a8ff; }
.msg-row .idx { color: #7d8590; font-family: monospace; }
.msg-row .blocks { font-family: monospace; }
.blk-pill { display: inline-block; padding: 1px 6px; border-radius: 3px;
  font-size: 10px; margin-right: 4px; }

pre.raw { background: #0a0d12; border: 1px solid #21262d; border-radius: 6px;
  padding: 10px 12px; overflow-x: auto; font-size: 11px; color: #c9d1d9;
  margin: 0; max-height: 240px; }
.refresh-bar { color: #7d8590; font-size: 11px; margin-top: 4px; }
.raw-preview { font-family: monospace; font-size: 10.5px; color: #8b949e;
  white-space: pre-wrap; word-break: break-word; margin: 2px 0 0 0;
  padding: 4px 8px; background: #0a0d12; border-radius: 4px;
  border-left: 2px solid #21262d; max-height: 120px; overflow-y: auto; }
"""


def _fmt_int(n: int | float | None) -> str:
    if n is None: return "—"
    return f"{int(n):,}"


def _fmt_age(sec: float) -> str:
    if sec < 60: return f"{int(sec)}s ago"
    if sec < 3600: return f"{int(sec/60)}m ago"
    if sec < 86400: return f"{int(sec/3600)}h ago"
    return f"{int(sec/86400)}d ago"


def _fmt_chars(n: int | None) -> str:
    if n is None: return "—"
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000: return f"{n/1_000:.2f}K"
    return f"{n:,}"


def _delta_span(d: int) -> str:
    if d == 0:
        return '<span class="delta">±0</span>'
    cls = "up" if d > 0 else "down"
    sign = "+" if d > 0 else "−"
    return f'<span class="delta {cls}">{sign}{abs(int(d)):,}</span>'


def _render_segment_stack(segs: Mapping[str, Mapping[str, int]],
                            seg_name: str) -> str:
    """Render the pin·fold·drop stacked bar for a single segment (tools / system / messages)."""
    bands = segs.get(seg_name) or {}
    pin = int((bands.get("pin") or {}).get("chars", 0))
    fold = int((bands.get("fold") or {}).get("chars", 0))
    drop = int((bands.get("drop") or {}).get("chars", 0))
    total = pin + fold + drop
    if total <= 0:
        return f'<div class="muted">{html.escape(seg_name)} <b>0</b> chars</div>'
    p_pct = 100 * pin / total
    f_pct = 100 * fold / total
    d_pct = 100 * drop / total
    pin_blocks = int((bands.get("pin") or {}).get("blocks", 0))
    fold_blocks = int((bands.get("fold") or {}).get("blocks", 0))
    drop_blocks = int((bands.get("drop") or {}).get("blocks", 0))
    return (
        f'<div style="margin-bottom:8px">'
        f'<div style="font-size:11px;color:#7d8590;font-family:monospace;'
        f'margin-bottom:2px">{html.escape(seg_name)} '
        f'<b style="color:#e6edf3">{total:,}</b> chars · '
        f'<span class="pin">P {pin:,} ({pin_blocks}b)</span> · '
        f'<span class="fold">F {fold:,} ({fold_blocks}b)</span> · '
        f'<span class="drop">D {drop:,} ({drop_blocks}b)</span></div>'
        f'<div class="stack-bar">'
        f'<span class="pin"  style="width:{p_pct:.1f}%" title="PIN {pin:,}"></span>'
        f'<span class="fold" style="width:{f_pct:.1f}%" title="FOLD {fold:,}"></span>'
        f'<span class="drop" style="width:{d_pct:.1f}%" title="DROP {drop:,}"></span>'
        f'</div></div>'
    )


def _render_overview(inspector: "_SessionInspector",
                       registry: "_SessionRegistry") -> str:
    items = list(inspector.items())
    items.sort(key=lambda kv: -kv[1].last_seen)

    rows = []
    for sid, e in items:
        n_tools = len(e.tools_stat)
        tool_invocations = sum(s.invocations for s in e.tools_stat.values())
        age = datetime.now(timezone.utc).timestamp() - e.last_seen
        rows.append(
            f'<tr>'
            f'<td class="left"><a href="?session={html.escape(sid)}">'
            f'{html.escape(sid)}</a></td>'
            f'<td class="left">{html.escape(e.last_model or "")}</td>'
            f'<td class="left">{html.escape(harness_display_name(e.last_harness or ""))}</td>'
            f'<td>{len(e.calls)}</td>'
            f'<td>{tool_invocations}</td>'
            f'<td>{n_tools}</td>'
            f'<td>{_fmt_int(e.tool_result_chars_total)}</td>'
            f'<td>{_fmt_age(age)}</td>'
            f'</tr>'
        )
    body = "\n".join(rows) or (
        '<tr><td colspan="8" class="muted left">'
        'No sessions yet — make a /v1/messages request and it will appear here.</td></tr>'
    )
    return f"""
<div class="card">
  <h2>Sessions ({len(items)} live · {len(registry)} bridge states)</h2>
  <table>
    <thead><tr>
      <th class="left">session_id</th>
      <th class="left">model</th>
      <th class="left">harness</th>
      <th>calls</th>
      <th>tool calls</th>
      <th>distinct tools</th>
      <th>tool_result chars</th>
      <th>last seen</th>
    </tr></thead>
    <tbody>{body}</tbody>
  </table>
</div>
"""


def _render_session_detail(entry, registry: "_SessionRegistry",
                             *, tab: str) -> str:
    layout = entry.last_layout or {}
    segs = layout.get("segments") or {}
    cache_fields = {
        k: v for k, v in (entry.last_usage_raw or {}).items()
        if k in ("input_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens", "output_tokens",
                  "cache_creation")
    }
    cache_pre = json.dumps(cache_fields, indent=2, ensure_ascii=False)

    # ---- KPI strip ----
    norm = entry.last_usage_norm or {}
    kpis = f"""
<div class="kpis">
  <div class="kpi"><div class="label">model</div>
    <div class="value" style="font-size:13px">{html.escape(entry.last_model or "—")}</div></div>
  <div class="kpi"><div class="label">harness</div>
    <div class="value" style="font-size:13px">{html.escape(harness_display_name(entry.last_harness) or "—")}</div></div>
  <div class="kpi"><div class="label">calls seen</div>
    <div class="value">{len(entry.calls)}</div></div>
  <div class="kpi"><div class="label">plan slots</div>
    <div class="value" style="font-size:13px">{html.escape(" / ".join(entry.last_plan_slots) or "—")}</div></div>
  <div class="kpi"><div class="label">last raw_input</div>
    <div class="value gold">{_fmt_int(norm.get("raw_input"))}</div></div>
  <div class="kpi"><div class="label">last cache_read</div>
    <div class="value green">{_fmt_int(norm.get("cache_read"))}</div></div>
  <div class="kpi"><div class="label">last cache_write</div>
    <div class="value gold">{_fmt_int(norm.get("cache_write"))}</div></div>
  <div class="kpi"><div class="label">last output</div>
    <div class="value purple">{_fmt_int(norm.get("output"))}</div></div>
</div>
"""

    # ---- region stacks (3 segments) ----
    region_html = (
        '<div class="card"><h2>Prompt regions · pin·fold·drop chars per segment</h2>'
        + _render_segment_stack(segs, "tools")
        + _render_segment_stack(segs, "system")
        + _render_segment_stack(segs, "messages")
        + '</div>'
    )

    # ---- per-call trace ----
    call_rows = []
    for c in reversed(entry.calls):
        seg_chars = c.get("segment_chars") or {}
        seg_delta = c.get("segment_chars_delta") or {}
        usage = c.get("usage_norm") or {}
        cells = []
        for seg in ("tools", "system", "messages"):
            cur = int(seg_chars.get(seg, 0))
            d = int(seg_delta.get(seg, 0))
            cells.append(f"<td>{_fmt_chars(cur)} {_delta_span(d)}</td>")
        call_rows.append(
            f'<tr>'
            f'<td>#{c.get("call_index", "?")}</td>'
            f'<td>{c.get("latency_s", 0):.2f}s</td>'
            f'<td>{_fmt_int(usage.get("raw_input"))}</td>'
            f'<td class="green">{_fmt_int(usage.get("cache_read"))}</td>'
            f'<td class="gold">{_fmt_int(usage.get("cache_write"))}</td>'
            f'<td class="purple">{_fmt_int(usage.get("output"))}</td>'
            + "".join(cells)
            + f'<td class="left">{html.escape(" / ".join(c.get("plan_slots") or []) or "—")}</td>'
            f'<td>{c.get("n_tool_uses", 0)}</td>'
            f'<td>{c.get("n_tool_results", 0)}</td>'
            f'</tr>'
        )
    calls_html = f"""
<div class="card">
  <h2>Recent calls (latest first · max {len(entry.calls)})</h2>
  <table>
    <thead><tr>
      <th>#</th><th>lat</th>
      <th>raw_in</th><th>cache_read</th><th>cache_write</th><th>output</th>
      <th>tools chars · Δ</th><th>system chars · Δ</th><th>messages chars · Δ</th>
      <th class="left">plan slots</th>
      <th>uses</th><th>results</th>
    </tr></thead>
    <tbody>{''.join(call_rows) or '<tr><td colspan="12" class="muted left">no calls yet</td></tr>'}</tbody>
  </table>
</div>
"""

    # ---- per-message band view (latest IR) ----
    msg_rows = []
    for m in layout.get("messages") or []:
        blks = []
        for b in m.get("blocks") or []:
            band = b.get("band", "?")
            color = _BAND_COLOR.get(band, "#7d8590")
            tag = b.get("source_tag") or b.get("ref_slug") or ""
            blks.append(
                f'<span class="blk-pill" style="background:{color}33;color:{color}">'
                f'{html.escape(band[:1].upper())}·{html.escape(b.get("kind",""))} '
                f'<b>{int(b.get("chars",0)):,}c</b>'
                + (f' <span class="muted">{html.escape(tag)}</span>' if tag else '')
                + '</span>'
            )
        msg_rows.append(
            f'<div class="msg-row">'
            f'<div class="idx">msg[{m.get("index")}]</div>'
            f'<div class="role">{html.escape(m.get("role","?"))}</div>'
            f'<div class="blocks">{"".join(blks) or "<span class=muted>(empty)</span>"}</div>'
            f'</div>'
        )
    messages_html = f"""
<div class="card">
  <h2>Latest IR · per-message blocks (band · kind · chars)</h2>
  {''.join(msg_rows) or '<div class="muted">no messages</div>'}
</div>
"""

    # ---- tool stats ----
    tool_rows = []
    for s in sorted(entry.tools_stat.values(), key=lambda x: -x.invocations):
        avg_args = (s.args_chars_total / s.invocations) if s.invocations else 0
        avg_res = (s.result_chars_total / max(s.invocations, 1))
        tool_rows.append(
            f'<tr>'
            f'<td class="left">{html.escape(s.name)}</td>'
            f'<td>{s.invocations}</td>'
            f'<td>{_fmt_int(s.args_chars_total)}</td>'
            f'<td>{_fmt_int(int(avg_args))}</td>'
            f'<td>{_fmt_int(s.last_args_chars)}</td>'
            f'<td class="green">{_fmt_int(s.result_chars_total)}</td>'
            f'<td>{_fmt_int(int(avg_res))}</td>'
            f'<td>{_fmt_int(s.result_chars_max)}</td>'
            f'<td>{_fmt_int(s.last_result_chars)}</td>'
            f'</tr>'
        )
    tools_html = f"""
<div class="card">
  <h2>Tool calls in this session</h2>
  <table>
    <thead><tr>
      <th class="left">tool name</th>
      <th>invocations</th>
      <th>args chars total</th>
      <th>args avg</th>
      <th>args last</th>
      <th>result chars total</th>
      <th>result avg</th>
      <th>result max</th>
      <th>result last</th>
    </tr></thead>
    <tbody>{''.join(tool_rows) or '<tr><td colspan="9" class="muted left">no tool calls yet</td></tr>'}</tbody>
  </table>
</div>
"""

    # ---- raw API cache fields ----
    api_html = f"""
<div class="card">
  <h2>Last API usage · cache-related fields (raw)</h2>
  <pre class="raw">{html.escape(cache_pre or "{}")}</pre>
</div>
"""

    # ---- original (pre-TELOS) messages ----
    raw_messages_html = _render_raw_messages(entry)

    return kpis + region_html + raw_messages_html + calls_html + messages_html + tools_html + api_html


def _render_raw_messages(entry) -> str:
    """Render a summary of the original (pre-TELOS-rewrite) messages of the last call.

    Each message lists role + each content block's type / char count / text preview.
    Data comes from ``calls[-1]["raw_messages"]`` (the proxy layer backfills it on each call).
    """
    last = entry.calls[-1] if entry.calls else None
    raw_msgs = (last or {}).get("raw_messages") or []
    rows = []
    for i, m in enumerate(raw_msgs):
        blks = []
        for b in m.get("blocks") or []:
            btype = b.get("type", "?")
            chars = int(b.get("chars", 0))
            extra = b.get("extra") or ""
            preview = b.get("preview") or ""
            ell = "…" if b.get("truncated") else ""
            label = html.escape(str(btype))
            if extra:
                label += f' · <span class="muted">{html.escape(str(extra))}</span>'
            blks.append(
                f'<div style="margin:3px 0">'
                f'<span class="blk-pill" style="background:#58a6ff22;color:#79c0ff">'
                f'{label} <b>{chars:,}c</b></span>'
                f'<div class="raw-preview">{html.escape(preview)}{ell}</div>'
                f'</div>'
            )
        rows.append(
            f'<div class="msg-row">'
            f'<div class="idx">msg[{i}]</div>'
            f'<div class="role">{html.escape(m.get("role","?"))}</div>'
            f'<div class="blocks">{"".join(blks) or "<span class=muted>(empty)</span>"}</div>'
            f'</div>'
        )
    ci = (last or {}).get("call_index", "?")
    return f"""
<div class="card">
  <h2>Original messages · pre-TELOS raw request (call #{ci})</h2>
  {''.join(rows) or '<div class="muted">no raw messages captured yet</div>'}
</div>
"""


def render_developer(
    inspector: "_SessionInspector",
    registry: "_SessionRegistry",
    *,
    focus_session: str | None = None,
    refresh_seconds: int | None = None,
    tab: str = "overview",
) -> str:
    """Render the developer page HTML (self-contained).

    - focus_session not specified: render the overview (session list)
    - specified: render all the detail panels for that session
    """
    refresh_tag = (
        f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
        if refresh_seconds and refresh_seconds > 0 else ""
    )
    refresh_note = (
        f' · auto-refresh {int(refresh_seconds)}s'
        if refresh_seconds and refresh_seconds > 0 else ""
    )
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if focus_session:
        entry = inspector.get(focus_session)
        if entry is None:
            body = (
                f'<div class="card"><h2>session not found</h2>'
                f'<p class="muted">no inspector record for <code>'
                f'{html.escape(focus_session)}</code>. '
                f'<a href="?">back to overview</a></p></div>'
            )
            title = f'session · {focus_session}'
        else:
            body = _render_session_detail(entry, registry, tab=tab)
            title = f'session · {entry.session_id}'
        back = '<div class="refresh-bar"><a href="?">← back to overview</a></div>'
    else:
        body = _render_overview(inspector, registry)
        title = 'overview'
        back = (
            '<div class="refresh-bar">JSON view available at '
            '<a href="/developer.json">/developer.json</a></div>'
        )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
{refresh_tag}
<title>TELOS · developer · {html.escape(title)}</title>
<style>{CSS}</style>
</head><body>
<div class="wrap">
<nav class="tabs" aria-label="Dashboard views">
  <a href="/">Context</a>
  <a href="/savings">Savings</a>
  <a class="active" href="/developer">Developer</a>
  <a href="/traces">Evidence</a>
</nav>
<header>
  <h1>TELOS · Trace inspector</h1>
  <div class="sub">
    {len(inspector)} session(s) tracked · {len(registry)} bridge state(s) ·
    {html.escape(title)} · generated {ts_now}{refresh_note}
  </div>
</header>
{back}
{body}
</div></body></html>
"""
