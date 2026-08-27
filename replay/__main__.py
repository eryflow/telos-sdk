"""``python -m telos.replay`` / ``telos replay`` entry point.

Replays a real session from tracing LLM spans once for each of several modes. Results
are appended to usage_log with comparison metadata (compare_group = the original
session id) so replay/showcase tooling can analyze modes side by side.

Usage::

    telos replay --list                       # list sessions with replayable LLM spans
    telos replay --session telos-ab12cd34      # run all 4 modes by default
    telos replay --session <id> --modes none,both
    telos replay --session <id> --cast         # record the dashboard changing
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from telos.cast import CastRecorder
from telos.config import telos_home
from telos.corpus import (DEFAULT_CORPUS_DIR, display_session, list_sessions,
                          load_session)
from telos.output_filter import MODE_LABELS, TelosMode, build_filter
from telos.replay import ReplayResult, anthropic_sender, replay_session
from telos.scripts.build_savings_dashboard import aggregate

_DEFAULT_USAGE_LOG = Path.home() / ".telos" / "usage.jsonl"
_DEFAULT_CAST = Path.home() / ".telos" / "replay-cast.cast"


def load_trace_session(db_path: Path, session_id: str) -> list[dict]:
    """Read raw model requests from LLM spans without opening SQLite for writes."""
    if not db_path.is_file():
        raise FileNotFoundError(f"tracing database not found: {db_path}")
    with sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True) as db:
        thread = db.execute(
            """SELECT id FROM threads WHERE id=? OR external_id=?
               ORDER BY last_updated_at_us DESC LIMIT 1""",
            (session_id, session_id),
        ).fetchone()
        if thread is None:
            raise FileNotFoundError(f"tracing session not found: {session_id}")
        rows = db.execute(
            """SELECT s.id,t.id,s.start_time_us,s.model,s.input_json
               FROM spans s JOIN traces t ON t.id=s.trace_id
               WHERE t.thread_id=? AND s.type='llm' AND s.input_json IS NOT NULL
               ORDER BY s.start_time_us,s.id""",
            (thread[0],),
        ).fetchall()
    turns = []
    for index, (span_id, trace_id, start_us, model, input_json) in enumerate(rows, 1):
        request = json.loads(input_json)
        if not isinstance(request, dict):
            continue
        if model and "model" not in request:
            request["model"] = model
        turns.append({
            "call_index": index,
            "ts": start_us / 1_000_000,
            "trace_id": trace_id,
            "span_id": span_id,
            "request": request,
        })
    return turns


def _list_trace_sessions(db_path: Path) -> list[dict]:
    if not db_path.is_file():
        return []
    with sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True) as db:
        rows = db.execute(
            """SELECT th.id,th.external_id,th.harness,COUNT(s.id),MAX(s.start_time_us)
               FROM threads th
               JOIN traces t ON t.thread_id=th.id
               JOIN spans s ON s.trace_id=t.id AND s.type='llm' AND s.input_json IS NOT NULL
               GROUP BY th.id,th.external_id,th.harness
               ORDER BY MAX(s.start_time_us) DESC"""
        ).fetchall()
    return [
        {"id": row[0], "session_id": row[1], "harness": row[2],
         "n_calls": row[3], "last_ts": row[4] / 1_000_000}
        for row in rows
    ]


def _print_sessions(tracing_db: Path, corpus_dir: Path | None) -> int:
    if corpus_dir is not None:
        infos = list_sessions(corpus_dir)
        source = f"legacy corpus {corpus_dir}"
        rows = [{"session_id": i.session_id, "n_calls": i.n_calls,
                 "last_ts": i.last_ts, "harness": "legacy"} for i in infos]
    else:
        rows = _list_trace_sessions(tracing_db)
        source = f"tracing database {tracing_db}"
    if not rows:
        print(f"no replayable LLM spans in {source}")
        return 0
    print(f"{source} —— {len(rows)} sessions:\n")
    print(f"  {'session':<40} {'calls':>6}  {'last_seen':<16}  harness")
    for row in rows:
        last = datetime.fromtimestamp(row["last_ts"]).strftime("%Y-%m-%d %H:%M") \
            if row["last_ts"] else "—"
        print(f"  {display_session(row['session_id']):<40} {row['n_calls']:>6}  "
              f"{last:<16}  {row['harness']}")
    print("\nreplay one:  telos replay --session <session>")
    return 0


def _show_session(session_id: str, turns: list[dict]) -> int:
    """Print a bounded, read-only view of any recorded wire protocol."""
    print(f"session {session_id} —— {len(turns)} recorded calls:\n")
    for seq, turn in enumerate(turns, 1):
        request = turn.get("request") or {}
        payload = request.get("messages", request.get("input"))
        protocol = "openai-responses" if "input" in request else "messages"
        items = len(payload) if isinstance(payload, list) else int(payload is not None)
        preview = json.dumps(payload, ensure_ascii=False, default=str)
        preview = " ".join(preview.split())
        if len(preview) > 160:
            preview = preview[:157] + "..."
        recorded = datetime.fromtimestamp(float(turn.get("ts") or 0)).strftime(
            "%Y-%m-%d %H:%M:%S") if turn.get("ts") else "—"
        print(
            f"  {seq:>3}. call_index={turn.get('call_index', '—')} · {recorded}"
            f" · {request.get('model', '—')} · {protocol}"
            f" · {items} input item(s) · {len(request.get('tools') or [])} tools"
        )
        print(f"       {preview or '—'}")
    return 0


def _append_records(usage_log: Path, results: list[ReplayResult]) -> int:
    usage_log.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with usage_log.open("a", encoding="utf-8") as f:
        for r in results:
            for rec in r.records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
    return n


def _print_summary(results: list[ReplayResult]) -> None:
    print(f"\n{'mode':<10} {'turns':>7} {'raw_input':>12} "
          f"{'cache_read':>12} {'cache_write':>12}")
    print("  " + "-" * 56)
    for r in results:
        print(f"{r.mode:<10} {r.turns_ok:>7} {r.total_raw_input:>12,} "
              f"{r.total_cache_read:>12,} {r.total_cache_write:>12,}")
        if r.turns_failed:
            print(f"  ⚠ {r.turns_failed} turns failed the upstream call (skipped)")


# ---------------------------------------------------------------------------
# --cast: record the savings dashboard updating as replay runs
# ---------------------------------------------------------------------------

def _usd(v: float) -> str:
    if v <= 0:
        return "—"
    return f"${v:.4f}" if v < 0.01 else f"${v:.2f}"


def _bar(frac: float, width: int = 22) -> str:
    filled = max(0, min(width, round(frac * width)))
    return "█" * filled + "░" * (width - filled)


def _render_dashboard_frame(session_label: str, mode_order: list[str],
                            cur_mode: str, idx: int, total: int,
                            summary, *, done: bool = False) -> str:
    """Render one text frame of the live savings dashboard for the cast."""
    by_mode = summary.by_mode
    frac = (idx / total) if total else 1.0
    head = "replay complete" if done else f"▶ replaying {cur_mode}"
    lines = [
        "",
        "  ┌─ TELOS replay · dashboard cast " + "─" * 37 + "┐",
        "  │",
        f"  │   session   {session_label}",
        f"  │   {head:<18} turn {idx:>4} / {total:<4}  [{_bar(frac)}] {frac * 100:3.0f}%",
        "  │",
        f"  │   {'mode':<7}{'turns':>7}{'cache_read':>14}"
        f"{'token cost':>13}{'saved $':>11}",
        "  │   " + "─" * 52,
    ]
    for m in mode_order:
        agg = by_mode.get(m)
        if agg is None or agg.calls == 0:
            lines.append(f"  │   {m:<7}{'—':>7}{'—':>14}{'—':>13}{'—':>11}")
            continue
        mark = "  ◀" if (m == cur_mode and not done) else ""
        lines.append(
            f"  │   {m:<7}{agg.calls:>7}{agg.cache_read:>14,}"
            f"{_usd(agg.cost_usd):>13}{_usd(agg.combined_saved_usd):>11}{mark}")
    tot = summary.total
    lines += [
        "  │",
        f"  │   cumulative saved   {_usd(tot.combined_saved_usd):<10}"
        f" ·   {tot.calls} turns replayed",
        "  │",
        "  └" + "─" * 70 + "┘",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="telos.replay",
        description="Record → replay comparison: run the same session once for each "
                    "of several modes and append comparison-tagged usage records.",
    )
    ap.add_argument("--tracing-db", type=Path, default=None,
                    help="SQLite tracing database (default $TELOS_HOME/telos.db)")
    ap.add_argument("--corpus-dir", type=Path, default=None,
                    help=f"read the legacy corpus instead (usually {DEFAULT_CORPUS_DIR})")
    ap.add_argument("--list", action="store_true",
                    help="list sessions with replayable LLM spans and exit")
    ap.add_argument("--session", default=None,
                    help="the session id to replay (see --list)")
    ap.add_argument("--show", action="store_true",
                    help="inspect a session without sending model requests")
    ap.add_argument("--modes", default="none,telos,rtk,both",
                    help="comma-separated list of modes (default none,telos,rtk,both)")
    ap.add_argument("--usage-log", type=Path, default=_DEFAULT_USAGE_LOG,
                    help=f"replay results are appended to this jsonl (default {_DEFAULT_USAGE_LOG})")
    ap.add_argument("--compare-group", default=None,
                    help="comparison group key (defaults to the session id)")
    ap.add_argument("--upstream", default="https://api.anthropic.com",
                    help="upstream Anthropic endpoint")
    ap.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"),
                    help="Anthropic API key (defaults to reading ANTHROPIC_API_KEY)")
    ap.add_argument("--no-cache-isolation", action="store_true",
                    help="do not inject a unique system prefix per mode (injected by default, "
                         "to avoid cross-mode cache pollution)")
    ap.add_argument("--cast", nargs="?", const=str(_DEFAULT_CAST), default=None,
                    metavar="PATH",
                    help="record an asciinema cast of the savings dashboard updating "
                         f"as replay runs (default path: {_DEFAULT_CAST})")
    args = ap.parse_args(argv)
    tracing_db = args.tracing_db or telos_home() / "telos.db"

    if args.list:
        return _print_sessions(tracing_db, args.corpus_dir)

    if not args.session:
        print("--session <id> is required (or use --list to view available sessions)", file=sys.stderr)
        return 2

    try:
        turns = (
            load_session(args.corpus_dir, args.session)
            if args.corpus_dir is not None
            else load_trace_session(tracing_db, args.session)
        )
    except (FileNotFoundError, sqlite3.DatabaseError, json.JSONDecodeError) as e:
        print(str(e), file=sys.stderr)
        return 1
    if not turns:
        print(f"session {args.session} has no replayable turns", file=sys.stderr)
        return 1

    if args.show:
        return _show_session(args.session, turns)

    if any("input" in (turn.get("request") or {}) for turn in turns):
        print(
            "OpenAI Responses sessions cannot be re-executed yet; "
            "use --show for a read-only view",
            file=sys.stderr,
        )
        return 2

    if not args.api_key:
        print("missing API key: set ANTHROPIC_API_KEY or pass --api-key", file=sys.stderr)
        return 2

    modes = [TelosMode.from_label(m.strip()) for m in args.modes.split(",")
             if m.strip()]
    if not modes:
        print(f"--modes parsed to empty; valid values: {', '.join(MODE_LABELS)}", file=sys.stderr)
        return 2

    compare_group = args.compare_group or args.session
    sender = anthropic_sender(api_key=args.api_key, upstream=args.upstream)
    flt = build_filter()

    # --cast: set up an asciinema recorder of the dashboard changing.
    mode_order = [m.label for m in modes]
    session_label = display_session(args.session)
    recorder: CastRecorder | None = None
    frame_dt = 0.1
    if args.cast:
        total_frames = max(1, len(turns) * len(modes))
        frame_dt = min(0.4, max(0.04, 40.0 / total_frames))
        recorder = CastRecorder(args.cast, title=f"TELOS replay — {session_label}")

    print(f"replaying session {args.session} ({len(turns)} turns)"
          f" × {len(modes)} modes → {args.usage_log}")
    if recorder is not None:
        print(f"  · recording dashboard cast → {recorder.path}")
    t0 = time.time()
    results: list[ReplayResult] = []
    done_records: list[dict] = []
    for mode in modes:
        print(f"  · mode={mode.label} …", flush=True)
        on_turn = None
        if recorder is not None:
            def on_turn(result: ReplayResult, idx: int, total: int,
                        _mode: str = mode.label) -> None:
                summary = aggregate(done_records + result.records)
                recorder.frame(
                    _render_dashboard_frame(session_label, mode_order, _mode,
                                            idx, total, summary),
                    dt=frame_dt)
        res = replay_session(
            turns, mode,
            session_id=args.session,
            compare_group=compare_group,
            sender=sender,
            flt=flt,
            cache_isolation=not args.no_cache_isolation,
            on_turn=on_turn,
        )
        results.append(res)
        done_records.extend(res.records)

    if recorder is not None:
        final = _render_dashboard_frame(
            session_label, mode_order, mode_order[-1],
            len(turns), len(turns), aggregate(done_records), done=True)
        recorder.frame(final, dt=frame_dt)
        recorder.frame(final, dt=2.5)  # hold the final dashboard on screen
        recorder.close()

    n = _append_records(args.usage_log, results)
    _print_summary(results)
    print(f"\nwrote {n} records · took {time.time() - t0:.1f}s")
    print(f"view savings totals: telos dashboard --usage-log {args.usage_log}")
    print(f"  (compare_group = {compare_group})")
    if recorder is not None:
        print(f"dashboard cast → {recorder.path}")
        print(f"  play it back:  asciinema play {recorder.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
