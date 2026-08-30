"""``build_savings_dashboard`` unit tests: RTK token source, cache-weighted pricing, TELOS/RTK breakdown."""

from __future__ import annotations

from pathlib import Path

from telos.scripts.build_savings_dashboard import aggregate, render_dashboard


def _rec(*, model: str = "claude-opus-4-7", raw_input: int = 0,
         cache_read: int = 0, cache_write: int = 0, output: int = 0,
         tool: dict | None = None, mode: str = "both", session: str = "s",
         ts: float = 1.0) -> dict:
    r: dict = {
        "model": model, "harness": "claude-code", "session_id": session,
        "mode": mode, "ts": ts,
        "normalized": {
            "raw_input": raw_input, "cache_read": cache_read,
            "cache_write": cache_write, "output": output,
        },
    }
    if tool is not None:
        r["tool_output_reduction"] = tool
    return r


def test_rtk_tokens_prefer_logged_token_fields() -> None:
    """When tool_output_reduction carries token fields, tool_saved_tokens takes the logged value rather than chars/4."""
    tool = {
        "original_chars": 40000, "filtered_chars": 12000,  # saved_chars/4 = 7000
        "original_tokens": 9000, "filtered_tokens": 4000,  # saved_tokens   = 5000
        "saved_tokens": 5000, "blocks_filtered": 2,
    }
    summary = aggregate([_rec(raw_input=1000, tool=tool)])
    assert summary.total.tool_saved_tokens == 5000, summary.total.tool_saved_tokens
    print("✓ test_rtk_tokens_prefer_logged_token_fields")


def test_rtk_tokens_fall_back_to_chars_for_old_logs() -> None:
    """Old logs have only chars, no token fields → fall back to saved_chars // 4."""
    tool = {"original_chars": 40000, "filtered_chars": 12000, "blocks_filtered": 1}
    summary = aggregate([_rec(raw_input=1000, tool=tool)])
    assert summary.total.tool_saved_tokens == 28000 // 4
    print("✓ test_rtk_tokens_fall_back_to_chars_for_old_logs")


def test_combined_equals_telos_plus_rtk() -> None:
    """combined_saved_usd strictly equals the sum of the TELOS and RTK paths, with no double counting."""
    tool = {"original_tokens": 8000, "filtered_tokens": 2000, "saved_tokens": 6000,
            "original_chars": 32000, "filtered_chars": 8000}
    summary = aggregate([_rec(raw_input=5000, cache_read=50000, tool=tool)])
    t = summary.total
    assert abs(t.combined_saved_usd - (t.saved_usd + t.tool_saved_usd)) < 1e-12
    assert t.saved_usd > 0 and t.tool_saved_usd > 0
    print("✓ test_combined_equals_telos_plus_rtk")


def test_cache_hit_weights_down_rtk_savings() -> None:
    """For the same N tokens saved: a call with a high cache hit rate should have a lower RTK $ than zero-hit (cheaper marginal price)."""
    tool = {"original_tokens": 12000, "filtered_tokens": 2000, "saved_tokens": 10000}
    # A: all raw_input → hit=0 → eff_price = input price
    # B: all cache_read → hit=1 → eff_price = cache_read price (10x cheaper)
    summary = aggregate([
        _rec(raw_input=100000, tool=dict(tool), session="A"),
        _rec(cache_read=100000, tool=dict(tool), session="B"),
    ])
    a = summary.by_session["A"].tool_saved_usd
    b = summary.by_session["B"].tool_saved_usd
    assert a > b > 0, (a, b)
    # opus 4.7: input $5 / cache_read $0.5 → ratio ~10x
    assert abs(a / b - 10.0) < 0.5, a / b
    print("✓ test_cache_hit_weights_down_rtk_savings")


def test_render_shows_total_cost_saved() -> None:
    """The rendered output contains the total-cost-saved hero card and the TELOS KPI."""
    tool = {"original_tokens": 8000, "filtered_tokens": 2000, "saved_tokens": 6000}
    summary = aggregate([_rec(raw_input=5000, cache_read=50000, tool=tool)])
    html_doc = render_dashboard(summary, [Path("sample.jsonl")])
    assert "total cost saved" in html_doc
    print("✓ test_render_shows_total_cost_saved")


def test_rtk_status_distinguishes_disabled_from_zero_save() -> None:
    """rtk_status breaks down "$0": never enabled / enabled but idle / ran but saved nothing / actually saved."""
    # disabled: no tool_output_reduction at all
    s = aggregate([_rec(raw_input=1000)])
    assert s.total.rtk_status == "disabled"
    # disabled: tool_output_reduction is an empty dict (what the proxy writes when RTK is off is {})
    s = aggregate([_rec(raw_input=1000, tool={})])
    assert s.total.rtk_status == "disabled"
    # idle: RTK ran but did not scan any tool_result
    s = aggregate([_rec(raw_input=1000, tool={"blocks_seen": 0,
                                              "original_chars": 0})])
    assert s.total.rtk_status == "idle"
    # nosave: tool output was scanned but nothing was saved (original == filtered)
    s = aggregate([_rec(raw_input=1000, tool={"blocks_seen": 3, "saved_tokens": 0,
                                              "original_chars": 500,
                                              "filtered_chars": 500})])
    assert s.total.rtk_status == "nosave"
    # active: tokens were actually saved
    s = aggregate([_rec(raw_input=1000, tool={"blocks_seen": 3, "saved_tokens": 4000,
                                              "original_tokens": 9000,
                                              "filtered_tokens": 5000})])
    assert s.total.rtk_status == "active"
    print("✓ test_rtk_status_distinguishes_disabled_from_zero_save")


def test_render_shows_mode_badge() -> None:
    """The header carries a mode badge reflecting the dominant mode in the log."""
    summary = aggregate([_rec(raw_input=5000, cache_read=50000, mode="telos")])
    html_doc = render_dashboard(summary, [Path("sample.jsonl")])
    assert "mode-badge" in html_doc
    assert ">telos<" in html_doc
    print("✓ test_render_shows_mode_badge")


def test_render_uses_logo_title_and_scrollable_tables() -> None:
    """The dashboard header uses the TELOS logo and wide breakdown tables stay contained."""
    summary = aggregate([
        _rec(
            raw_input=5000,
            cache_read=50000,
            session="very-long-session-id-that-should-not-push-the-dashboard-past-the-edge",
        )
    ])
    html_doc = render_dashboard(summary, [Path("sample.jsonl")])
    assert 'class="brand-logo"' in html_doc
    assert 'class="title-lockup"' in html_doc
    assert "<title>TELOS · Token Savings</title>" in html_doc
    assert 'rel="icon"' in html_doc
    assert "Token Savings" in html_doc
    assert 'href="/">Overview</a>' in html_doc
    assert 'href="/tasks">Long Tasks</a>' in html_doc
    assert 'href="/traces">Traces</a>' in html_doc
    assert 'class="table-scroll"' in html_doc
    assert 'class="breakdown-table"' in html_doc
    print("✓ test_render_uses_logo_title_and_scrollable_tables")


def main() -> None:
    test_rtk_tokens_prefer_logged_token_fields()
    test_rtk_tokens_fall_back_to_chars_for_old_logs()
    test_combined_equals_telos_plus_rtk()
    test_cache_hit_weights_down_rtk_savings()
    test_render_shows_total_cost_saved()
    test_rtk_status_distinguishes_disabled_from_zero_save()
    test_render_shows_mode_badge()
    test_render_uses_logo_title_and_scrollable_tables()
    print("\nall savings_dashboard tests passed.")


if __name__ == "__main__":
    main()
