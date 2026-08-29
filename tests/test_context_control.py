from __future__ import annotations

from telos.scripts.build_context_control import render_context_control


def test_context_control_is_goal_first_and_uses_public_routes() -> None:
    html = render_context_control("write-token")
    assert "Context Control Plane" in html
    assert all(label in html for label in ("Overview", "Task runs", "Evolution", "Evidence"))
    assert "Current progress and next step" not in html  # runtime Pack content, not fake demo data
    assert "history.pushState" in html
    assert "setInterval(load,5000)" in html
    assert "Agent context overview" in html
    assert "api('/traces?limit=50')" in html
    assert "id=\"liveState\"" in html
    assert "fetch('/api/v1'+path" in html
    assert 'href="/traces"' in html
    assert 'class="kpis"' in html
    assert 'class="app"' in html
    assert "write-token" in html
    assert "Raw JSON" not in html
