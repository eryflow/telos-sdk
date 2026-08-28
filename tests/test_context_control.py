from __future__ import annotations

from telos.scripts.build_context_control import render_context_control


def test_context_control_is_goal_first_and_keeps_selected_view() -> None:
    html = render_context_control("write-token")
    assert "Context Control Plane" in html
    assert all(label in html for label in ("Context", "Runs", "Evolution", "Evidence"))
    assert "Current progress and next step" not in html  # runtime Pack content, not fake demo data
    assert "localStorage.setItem('telos.control.view'" in html
    assert "setInterval(load,5000)" in html
    assert "location.hash.slice(1)||localStorage.getItem('telos.control.view')" in html
    assert "write-token" in html
    assert "Raw JSON" not in html
