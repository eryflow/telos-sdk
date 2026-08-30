from __future__ import annotations

from telos.scripts.build_context_control import render_context_control


def test_context_control_is_goal_first_and_uses_public_routes() -> None:
    html = render_context_control("write-token")
    assert "Context Control Plane" in html
    assert all(label in html for label in (
        "Conversations", "Long Tasks", "Knowledge", "Evaluations",
    ))
    assert "Current progress and next step" not in html  # runtime Pack content, not fake demo data
    assert "history.pushState" in html
    assert "setInterval(load,5000)" in html
    assert 'id="taskComposer"' in html
    assert all(runtime in html for runtime in ("codex", "kimi-code", "deepseek-harness"))
    assert 'name="taskPlugin"' in html
    assert 'id="createLongTask"' in html
    assert "普通 TaskRun，不会自动成为 Long Task" in html
    assert "write('/tasks'" in html
    assert all(label in html for label in (
        "Overview", "State", "Executions", "Skills", "Agent", "Evidence", "Evolution",
    ))
    assert "api('/traces?limit=50')" in html
    assert "id=\"liveState\"" in html
    assert "fetch('/api/v1'+path" in html
    assert 'href="/traces"' in html
    assert '/traces?attempt_id=${encodeURIComponent(t.attempt_id)}' in html
    assert 'class="kpis"' in html
    assert 'class="app"' in html
    assert "write-token" in html
    assert "Raw JSON" not in html
