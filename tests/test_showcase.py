"""Offline smoke tests for ``telos showcase`` — every scene must run with no network."""

from __future__ import annotations

import json

from telos.scripts import showcase as sc


def test_demo_corpus_shape() -> None:
    turns = sc.build_demo_corpus()
    assert len(turns) >= 10  # a multi-turn session, long enough to amortize
    for i, turn in enumerate(turns, start=1):
        assert turn["call_index"] == i
        req = turn["request"]
        assert req["model"] == sc.DEMO_MODEL
        assert req["tools"] and req["system"] and req["messages"]
    # turns accumulate: each request carries more messages than the last
    sizes = [len(t["request"]["messages"]) for t in turns]
    assert sizes == sorted(sizes) and sizes[0] < sizes[-1]


def test_synthetic_sender_models_prefix_caching() -> None:
    # an append-only session: turn 2's prompt contains turn 1's prompt
    small = {"system": "s", "tools": [], "messages": [{"role": "user", "content": "a"}]}
    big = {"system": "s" * 8000, "tools": [],
           "messages": [{"role": "user", "content": "a"}] * 6}
    # passthrough never reports cache hits
    none = sc.synthetic_sender("none")
    assert none(small)["cache_read_input_tokens"] == 0
    assert none(big)["cache_read_input_tokens"] == 0
    # TELOS: turn 1 cold, turn 2 reuses the whole prior prefix as cache_read
    telos = sc.synthetic_sender("telos")
    t1 = telos(small)
    t2 = telos(big)
    assert t1["cache_read_input_tokens"] == 0
    assert t2["cache_read_input_tokens"] > 0
    # the demo session should show a high steady-state cache hit rate
    by_mode = sc._aggregate_by_mode(
        sc._run_replays(sc.build_demo_corpus(), None))
    telos_agg = by_mode["telos"]
    prompt = telos_agg.raw_input + telos_agg.cache_read + telos_agg.cache_write
    assert telos_agg.cache_read / prompt >= 0.70


def test_recorded_sender_falls_back_to_synthetic() -> None:
    # no recordings → sender still returns a usable usage dict
    send = sc.recorded_sender("both", None)
    usage = send({"model": sc.DEMO_MODEL, "messages": []})
    assert "cache_read_input_tokens" in usage


def test_recorded_sender_replays_in_order() -> None:
    responses = {"both": [{"input_tokens": 11}, {"input_tokens": 22}]}
    send = sc.recorded_sender("both", responses)
    assert send({})["input_tokens"] == 11
    assert send({})["input_tokens"] == 22


def test_scene_replay_produces_four_modes() -> None:
    records = sc._run_replays(sc.build_demo_corpus(), sc.load_responses())
    modes = {r["mode"] for r in records}
    assert modes == set(sc.REPLAY_MODES)
    # every record carries the compare_group so replay/showcase analysis can group them
    assert all(r["compare_group"] == sc.COMPARE_GROUP for r in records)
    by_mode = sc._aggregate_by_mode(records)
    # TELOS modes resolve a cacheable prefix; passthrough does not
    assert by_mode["both"].cache_read > by_mode["none"].cache_read


def test_telos_cuts_token_cost_by_at_least_70pct() -> None:
    """The headline claim: TELOS saves 70%+ on a realistic agent session."""
    by_mode = sc._aggregate_by_mode(
        sc._run_replays(sc.build_demo_corpus(), sc.load_responses()))
    base = by_mode["none"].cost_usd
    assert base > 0
    for mode in ("telos", "both"):
        cut = (base - by_mode[mode].cost_usd) / base
        assert cut >= 0.70, f"{mode}: only {cut:.0%} cost cut"


def test_cast_output_is_valid_asciinema_v2(tmp_path) -> None:
    cast = tmp_path / "demo.cast"
    p = sc.Printer(cast_path=str(cast))
    p.out("hello")
    p.out("world")
    p.close()
    lines = cast.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["version"] == 2
    for line in lines[1:]:
        evt = json.loads(line)
        assert len(evt) == 3 and evt[1] == "o"
