from telos.scripts.build_trace_explorer import render_trace_explorer


def test_trace_explorer_is_self_contained_and_uses_requested_api() -> None:
    html = render_trace_explorer(api_base="/custom/api")
    assert "轨迹观测" in html
    assert "评估中心" in html

    assert "TELOS Traces" in html
    assert 'const API="/custom/api"' in html
    assert "type=\"module\"" in html
    assert "Span tree" not in html  # The page renders the tree from API data.
    assert "innerHTML" not in html
    assert "textContent" in html
    assert 'id="project"' in html
    assert 'id="model"' in html
    assert 'type="datetime-local"' in html
    assert 'id="load-more"' in html
    assert "Thread timeline" in html
    assert "cost_usd_micros" in html
    assert "selectedInspectorTab=k" in html
    assert "show(selectedInspectorTab" in html
