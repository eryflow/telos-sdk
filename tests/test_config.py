"""``telos.config`` tests: round-trip / defaults / bad JSON / unknown-key preservation."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import telos.config as cfgmod


def _tmp_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="telos-cfg-"))


def _with_home(home: Path):
    os.environ["TELOS_HOME"] = str(home)


def test_load_missing_returns_defaults() -> None:
    _with_home(_tmp_home())
    c = cfgmod.load_config()
    assert c.mode == "telos"
    assert c.gateway.port == 7171
    assert c.favorite_harness is None
    assert c.upstreams["openai"].url == "https://api.openai.com"
    assert c.upstreams["openai"].engine == "openai"
    print("✓ test_load_missing_returns_defaults")


def test_save_load_round_trip() -> None:
    _with_home(_tmp_home())
    c = cfgmod.load_config()
    c.mode = "both"
    c.gateway.port = 9999
    c.favorite_harness = "codex"
    c.harness_executables = {"openclaw": "openclaw-beta"}
    cfgmod.save_config(c)

    c2 = cfgmod.load_config()
    assert c2.mode == "both"
    assert c2.gateway.port == 9999
    assert c2.favorite_harness == "codex"
    assert c2.harness_executables["openclaw"] == "openclaw-beta"
    print("✓ test_save_load_round_trip")


def test_update_config() -> None:
    _with_home(_tmp_home())
    cfgmod.update_config(mode="rtk", gateway_port=8080)
    c = cfgmod.load_config()
    assert c.mode == "rtk"
    assert c.gateway.port == 8080
    print("✓ test_update_config")


def test_unknown_keys_preserved() -> None:
    home = _tmp_home()
    _with_home(home)
    path = home / "config.json"
    path.write_text(json.dumps({"mode": "both", "future_field": {"x": 1}}))
    c = cfgmod.load_config()
    cfgmod.save_config(c)
    reloaded = json.loads(path.read_text())
    assert reloaded["future_field"] == {"x": 1}
    print("✓ test_unknown_keys_preserved")


def test_bad_json_raises() -> None:
    home = _tmp_home()
    _with_home(home)
    (home / "config.json").write_text("{not json")
    try:
        cfgmod.load_config()
    except RuntimeError as e:
        assert "JSON" in str(e)
        print("✓ test_bad_json_raises")
        return
    raise AssertionError("expected RuntimeError")


def main() -> None:
    test_load_missing_returns_defaults()
    test_save_load_round_trip()
    test_update_config()
    test_unknown_keys_preserved()
    test_bad_json_raises()
    print("\nall config tests passed.")


if __name__ == "__main__":
    main()
