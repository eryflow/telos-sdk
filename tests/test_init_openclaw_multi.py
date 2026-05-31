"""Multi-provider OpenClawInstaller tests (Phase 2.5).

Exercises the "patch a subset" / "patch all" / "patch incrementally" flows
introduced by the interactive checklist refactor. Selection is fed via the
``providers_to_patch`` kwarg so the tests are deterministic without a TTY.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from telos.init.openclaw import OpenClawInstaller


def _write(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _multi_provider_cfg() -> dict:
    return {
        "agents": {
            "defaults": {
                "model": {"primary": "deepseek/deepseek-chat"},
            },
        },
        "models": {
            "providers": {
                "deepseek": {
                    "baseUrl": "https://api.deepseek.com",
                    "api": "openai-completions",
                },
                "openrouter": {
                    "baseUrl": "https://openrouter.ai/api/v1",
                    "api": "openai-completions",
                },
                "anthropic": {
                    "baseUrl": "https://api.anthropic.com",
                    "api": "anthropic-messages",
                },
            },
        },
    }


def _setup(tmp_path: Path, providers_to_patch=None):
    config_path = tmp_path / "openclaw.json"
    _write(config_path, _multi_provider_cfg())
    state_path = tmp_path / "state.json"
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    inst = OpenClawInstaller(
        proxy_url="http://127.0.0.1:7171",
        config_path=config_path,
        state_path=state_path,
        providers_to_patch=providers_to_patch,
    )
    return inst, config_path, state_path


def _teardown():
    os.environ.pop("TELOS_HOME", None)


def test_install_patches_subset(tmp_path: Path) -> None:
    inst, config_path, state_path = _setup(
        tmp_path, providers_to_patch=["deepseek", "openrouter"],
    )
    try:
        inst.install()
        data = json.loads(config_path.read_text())
        # deepseek + openrouter patched.
        assert (data["models"]["providers"]["deepseek"]["baseUrl"]
                == "http://127.0.0.1:7171/_h/openclaw/upstreams/deepseek")
        assert (data["models"]["providers"]["openrouter"]["baseUrl"]
                == "http://127.0.0.1:7171/_h/openclaw/upstreams/openrouter")
        # anthropic untouched.
        assert (data["models"]["providers"]["anthropic"]["baseUrl"]
                == "https://api.anthropic.com")
        # State has 2 records.
        state = json.loads(state_path.read_text())
        ids = sorted(s["provider_id"] for s in state["patched"])
        assert ids == ["deepseek", "openrouter"]
    finally:
        _teardown()
def test_incremental_install_merges_state(tmp_path: Path) -> None:
    """First install picks deepseek. Second install picks anthropic. State
    must record BOTH (additive)."""
    inst1, config_path, state_path = _setup(
        tmp_path, providers_to_patch=["deepseek"],
    )
    try:
        inst1.install()
        # Now run a separate installer that picks anthropic.
        inst2 = OpenClawInstaller(
            proxy_url="http://127.0.0.1:7171",
            config_path=config_path,
            state_path=state_path,
            providers_to_patch=["anthropic"],
        )
        inst2.install()
        data = json.loads(config_path.read_text())
        # Both patched.
        assert (data["models"]["providers"]["deepseek"]["baseUrl"]
                == "http://127.0.0.1:7171/_h/openclaw/upstreams/deepseek")
        assert (data["models"]["providers"]["anthropic"]["baseUrl"]
                == "http://127.0.0.1:7171/_h/openclaw/upstreams/anthropic")
        # State has both records.
        state = json.loads(state_path.read_text())
        ids = sorted(s["provider_id"] for s in state["patched"])
        assert ids == ["anthropic", "deepseek"]
    finally:
        _teardown()


def test_uninstall_restores_all_patched(tmp_path: Path) -> None:
    inst, config_path, state_path = _setup(
        tmp_path, providers_to_patch=["deepseek", "openrouter"],
    )
    try:
        inst.install()
        r = inst.uninstall()
        assert config_path in r.changed_files
        data = json.loads(config_path.read_text())
        assert (data["models"]["providers"]["deepseek"]["baseUrl"]
                == "https://api.deepseek.com")
        assert (data["models"]["providers"]["openrouter"]["baseUrl"]
                == "https://openrouter.ai/api/v1")
        assert not state_path.exists()
    finally:
        _teardown()


def test_uninstall_skips_partially_modified_routes(tmp_path: Path) -> None:
    """If after install the user manually changed ONE provider's baseUrl,
    uninstall restores the others and keeps the modified one + its state."""
    inst, config_path, state_path = _setup(
        tmp_path, providers_to_patch=["deepseek", "openrouter"],
    )
    try:
        inst.install()
        data = json.loads(config_path.read_text())
        data["models"]["providers"]["deepseek"]["baseUrl"] = "https://manual.example"
        config_path.write_text(json.dumps(data, indent=2))

        r = inst.uninstall()
        data2 = json.loads(config_path.read_text())
        # openrouter restored.
        assert (data2["models"]["providers"]["openrouter"]["baseUrl"]
                == "https://openrouter.ai/api/v1")
        # deepseek kept as user set it.
        assert (data2["models"]["providers"]["deepseek"]["baseUrl"]
                == "https://manual.example")
        # State file still exists with the deepseek record kept.
        state = json.loads(state_path.read_text())
        ids = [s["provider_id"] for s in state["patched"]]
        assert ids == ["deepseek"]
        assert any("not the route we set" in n for n in r.notes)
    finally:
        _teardown()
def test_state_v1_backward_compat(tmp_path: Path) -> None:
    """An existing v1 single-provider state file is read as a 1-element list."""
    config_path = tmp_path / "openclaw.json"
    _write(config_path, _multi_provider_cfg())
    # Pre-patch deepseek manually so it matches the state.
    data = json.loads(config_path.read_text())
    data["models"]["providers"]["deepseek"]["baseUrl"] = (
        "http://127.0.0.1:7171/_h/openclaw/upstreams/deepseek"
    )
    config_path.write_text(json.dumps(data, indent=2))

    state_path = tmp_path / "state.json"
    # v1 shape: a flat object.
    state_path.write_text(json.dumps({
        "version": 1,
        "provider_id": "deepseek",
        "previous_base_url": "https://api.deepseek.com",
        "gateway_route_url": "http://127.0.0.1:7171/_h/openclaw/upstreams/deepseek",
    }))

    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    try:
        inst = OpenClawInstaller(
            config_path=config_path,
            state_path=state_path,
        )
        # uninstall should read v1, restore deepseek, and delete state.
        r = inst.uninstall()
        assert config_path in r.changed_files
        data2 = json.loads(config_path.read_text())
        assert (data2["models"]["providers"]["deepseek"]["baseUrl"]
                == "https://api.deepseek.com")
        assert not state_path.exists()
    finally:
        _teardown()


def test_repatch_after_daemon_port_change_preserves_original(tmp_path: Path) -> None:
    """If the daemon moves to a new port between installs, re-install must
    update the patched URL but KEEP the originally-recorded baseUrl in state
    so uninstall still restores to the user's real upstream (not the
    intermediate telos route).
    """
    config_path = tmp_path / "openclaw.json"
    _write(config_path, _multi_provider_cfg())
    state_path = tmp_path / "state.json"
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    try:
        # First install with daemon on 7171.
        inst_a = OpenClawInstaller(
            proxy_url="http://127.0.0.1:7171",
            config_path=config_path,
            state_path=state_path,
            providers_to_patch=["deepseek"],
        )
        inst_a.install()
        # Sanity: state remembers the original.
        state = json.loads(state_path.read_text())
        rec = next(r for r in state["patched"]
                    if r["provider_id"] == "deepseek")
        assert rec["previous_base_url"] == "https://api.deepseek.com"
        # openclaw.json now has the 7171 route URL.
        data1 = json.loads(config_path.read_text())
        assert (data1["models"]["providers"]["deepseek"]["baseUrl"]
                == "http://127.0.0.1:7171/_h/openclaw/upstreams/deepseek")

        # Daemon moves to 7392; same install runs again.
        inst_b = OpenClawInstaller(
            proxy_url="http://127.0.0.1:7392",
            config_path=config_path,
            state_path=state_path,
            providers_to_patch=["deepseek"],
        )
        r = inst_b.install()
        assert config_path in r.changed_files
        # openclaw.json now points at 7392 (re-aligned).
        data2 = json.loads(config_path.read_text())
        assert (data2["models"]["providers"]["deepseek"]["baseUrl"]
                == "http://127.0.0.1:7392/_h/openclaw/upstreams/deepseek")
        # State STILL records the original upstream (not the 7171 stale route).
        state2 = json.loads(state_path.read_text())
        rec2 = next(r for r in state2["patched"]
                     if r["provider_id"] == "deepseek")
        assert rec2["previous_base_url"] == "https://api.deepseek.com"
        assert rec2["gateway_route_url"] == "http://127.0.0.1:7392/_h/openclaw/upstreams/deepseek"

        # And uninstall (with the 7392 installer) restores to the real upstream.
        inst_b.uninstall()
        data3 = json.loads(config_path.read_text())
        assert (data3["models"]["providers"]["deepseek"]["baseUrl"]
                == "https://api.deepseek.com")
    finally:
        _teardown()


def test_repatch_stale_route_without_state_is_refused(tmp_path: Path) -> None:
    """If baseUrl looks like a stale telos route but no state file exists,
    refuse to patch — patching would lock the stale URL in as 'original' and
    uninstall could never restore the real upstream."""
    config_path = tmp_path / "openclaw.json"
    cfg = _multi_provider_cfg()
    cfg["models"]["providers"]["deepseek"]["baseUrl"] = (
        "http://127.0.0.1:7392/upstreams/deepseek"  # stale, no state
    )
    _write(config_path, cfg)
    state_path = tmp_path / "state.json"  # does not exist
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    try:
        inst = OpenClawInstaller(
            proxy_url="http://127.0.0.1:7171",
            config_path=config_path,
            state_path=state_path,
            providers_to_patch=["deepseek"],
        )
        r = inst.install()
        assert not r.changed_files
        assert any("stale telos route" in n for n in r.notes)
        # openclaw.json is unchanged.
        data = json.loads(config_path.read_text())
        assert (data["models"]["providers"]["deepseek"]["baseUrl"]
                == "http://127.0.0.1:7392/upstreams/deepseek")
    finally:
        _teardown()
