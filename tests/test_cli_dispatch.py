"""``telos.cli`` dispatch tests: subcommand routing / error codes / alias persistence."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
from unittest.mock import patch

from telos import cli
from telos.init.__main__ import uninstall_main


def _iso_home() -> None:
    os.environ["TELOS_HOME"] = tempfile.mkdtemp(prefix="telos-cli-")


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


def test_help_omits_proxy() -> None:
    rc, out = _run(["--help"])
    assert rc == 0
    assert "telos gateway" in out
    # proxy is a hidden alias and does not appear in the subcommands list.
    assert "  proxy " not in out
    print("✓ test_help_omits_proxy")


def test_unknown_subcommand() -> None:
    rc, out = _run(["bogus"])
    assert rc == 2
    assert "unknown subcommand" in out
    print("✓ test_unknown_subcommand")


def test_alias_round_trip() -> None:
    _iso_home()
    rc, _ = _run(["alias", "codex"])
    assert rc == 0
    rc, out = _run(["alias"])
    assert rc == 0
    assert "codex" in out
    print("✓ test_alias_round_trip")


def test_alias_rejects_unknown() -> None:
    _iso_home()
    rc, out = _run(["alias", "nope"])
    assert rc == 2
    assert "unknown harness" in out
    print("✓ test_alias_rejects_unknown")


def test_mode_persists_without_gateway() -> None:
    _iso_home()
    rc, out = _run(["mode", "both"])
    assert rc == 0
    assert "both" in out
    import telos.config as cfgmod
    assert cfgmod.load_config().mode == "both"
    print("✓ test_mode_persists_without_gateway")


def test_mode_rejects_bad_label() -> None:
    _iso_home()
    rc, out = _run(["mode", "turbo"])
    assert rc == 2
    assert "unknown mode" in out
    print("✓ test_mode_rejects_bad_label")


def test_evolve_task_round_trip() -> None:
    _iso_home()
    rc, out = _run(["evolve", "--task", "代码缺陷修复"])
    assert rc == 0
    assert "evaluation policy: offline" in out
    assert "evaluator worker: not shipped yet" in out
    rc, out = _run(["evolve"])
    assert rc == 0
    assert "代码缺陷修复" in out
    import telos.config as cfgmod
    policy = cfgmod.load_config().evolution_tasks["代码缺陷修复"]
    assert policy["enabled"] is True
    assert policy["promotion"] == "manual"


def test_trace_hook_dispatches_supported_hooks() -> None:
    with patch("telos.codex_tracing.main", return_value=7) as hook:
        rc, _ = _run(["trace-hook", "codex"])
    assert rc == 7
    hook.assert_called_once_with([])

    with patch("telos.kimi_tracing.main", return_value=8) as hook:
        rc, _ = _run(["trace-hook", "kimi-code"])
    assert rc == 8
    hook.assert_called_once_with([])

    rc, out = _run(["trace-hook", "deepseek-harness"])
    assert rc == 2
    assert "usage: telos trace-hook <codex|kimi-code>" in out


def test_dashboard_rejects_bad_verb() -> None:
    _iso_home()
    rc, out = _run(["dashboard", "bogus"])
    assert rc == 2
    assert "unknown dashboard verb" in out
    print("✓ test_dashboard_rejects_bad_verb")


def test_status_without_gateway() -> None:
    _iso_home()
    rc, out = _run(["status"])
    assert rc == 0
    # The three sections are always present, even with no gateway running.
    assert "gateway" in out
    assert "harnesses (injection)" in out
    assert "traffic forwarding" in out
    assert "not running" in out
    print("✓ test_status_without_gateway")


def test_status_json_is_parseable() -> None:
    _iso_home()
    rc, out = _run(["status", "--json"])
    assert rc == 0
    snap = json.loads(out)
    assert set(snap) == {"gateway", "harnesses", "traffic"}
    assert snap["gateway"]["running"] is False
    assert isinstance(snap["traffic"]["upstreams"], list)
    print("✓ test_status_json_is_parseable")


def test_status_rejects_bad_option() -> None:
    _iso_home()
    rc, out = _run(["status", "--bogus"])
    assert rc == 2
    assert "unknown status option" in out
    print("✓ test_status_rejects_bad_option")


def _run_uninstall(argv: list[str]) -> tuple[int, str]:
    """Run uninstall_main capturing output; argparse errors surface as rc=2."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = uninstall_main(argv)
        except SystemExit as e:  # argparse error path
            rc = e.code if isinstance(e.code, int) else 1
    return rc, buf.getvalue()


@contextlib.contextmanager
def _isolated_claude_dir():
    """Point CLAUDE_CONFIG_DIR at a fresh empty dir, restoring it afterwards."""
    prev = os.environ.get("CLAUDE_CONFIG_DIR")
    d = os.path.join(tempfile.mkdtemp(prefix="telos-undo-"), ".claude")
    os.makedirs(d)
    os.environ["CLAUDE_CONFIG_DIR"] = d
    try:
        yield d
    finally:
        if prev is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = prev


def test_uninstall_unknown_harness_suggests() -> None:
    # 'claude' (or the shell-split 'claude code') should hint at 'claude-code'.
    rc, out = _run_uninstall(["--harness", "claude"])
    assert rc == 2
    assert "Did you mean 'claude-code'" in out
    print("✓ test_uninstall_unknown_harness_suggests")


def test_uninstall_skips_uninjected_harness() -> None:
    _iso_home()
    with _isolated_claude_dir():
        rc, out = _run_uninstall(["--harness", "claude-code"])
    assert rc == 0
    assert "not currently connected" in out
    assert "Restart any running harness" not in out  # nothing was reverted
    print("✓ test_uninstall_skips_uninjected_harness")


def test_uninstall_disables_stale_trace_registration() -> None:
    _iso_home()
    import telos.config as cfgmod
    cfgmod.enable_harness_trace("claude-code")
    with _isolated_claude_dir():
        rc, out = _run_uninstall(["--harness", "claude-code"])
    assert rc == 0
    assert "local Trace disabled" in out
    assert cfgmod.load_config().trace_harnesses["claude-code"]["enabled"] is False


def test_uninstall_reverts_injected_harness() -> None:
    _iso_home()
    with _isolated_claude_dir() as d:
        sp = os.path.join(d, "settings.json")
        with open(sp, "w") as f:
            json.dump({"env": {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:7171/_h/claude-code",
                "__telos_installed": True,
            }}, f)
        rc, out = _run_uninstall(["--harness", "claude-code"])
        data = json.loads(open(sp).read())
    assert rc == 0
    assert "Restart any running harness" in out  # reminder shown after a real revert
    assert "ANTHROPIC_BASE_URL" not in data.get("env", {})
    print("✓ test_uninstall_reverts_injected_harness")


def main() -> None:
    test_help_omits_proxy()
    test_unknown_subcommand()
    test_alias_round_trip()
    test_alias_rejects_unknown()
    test_mode_persists_without_gateway()
    test_mode_rejects_bad_label()
    test_evolve_task_round_trip()
    test_report_uses_registered_token_without_printing_it()
    test_dashboard_rejects_bad_verb()
    test_status_without_gateway()
    test_status_json_is_parseable()
    test_status_rejects_bad_option()
    test_uninstall_unknown_harness_suggests()
    test_uninstall_skips_uninjected_harness()
    test_uninstall_disables_stale_trace_registration()
    test_uninstall_reverts_injected_harness()
    print("\nall cli dispatch tests passed.")


if __name__ == "__main__":
    main()
