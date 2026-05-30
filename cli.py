"""``telos`` command-line entry point (unified dispatch).

Subcommands:

- ``telos``               bare command → pick a harness and enter its CLI
- ``telos <harness>``     directly enter a harness (claude-code / codex / openclaw / hermes)
- ``telos init``          auto-detect harnesses → inject → start gateway in background → print dashboard
- ``telos uninstall``     undo the injection from ``telos init`` (per-harness with ``--harness``; ``--purge`` fully removes telos)
- ``telos gateway``       start / stop / view the gateway
- ``telos dashboard``     open the dashboard in a browser (``restart`` cycles the gateway serving it)
- ``telos mode``          switch the optimization mode (hot-updates the running gateway)
- ``telos alias``         set the harness the bare ``telos`` enters by default
- ``telos replay``        replay a recorded session across multiple modes for comparison
- ``telos proxy``         (hidden alias) run the gateway blocking in the foreground, equivalent to the old telos proxy
"""

from __future__ import annotations

import os
import sys
import webbrowser

from telos.harnesses import HARNESS_NAMES


def _resolve_version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("telos-sdk")
    except PackageNotFoundError:
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in ("-h", "--help"):
        _print_usage()
        return 0

    if argv and argv[0] in ("-V", "--version", "version"):
        print(f"telos {_resolve_version()}")
        return 0

    if not argv:
        return _cmd_bare()

    subcommand, rest = argv[0], argv[1:]

    if subcommand == "gateway":
        from telos.gateway.__main__ import main as gateway_main
        return gateway_main(rest)
    if subcommand == "proxy":
        # Hidden alias: keeps the old `telos proxy` foreground-blocking behavior, all old flags compatible.
        from telos.proxy.__main__ import main as proxy_main
        return proxy_main(rest)
    if subcommand == "init":
        from telos.init.__main__ import main as init_main
        return init_main(rest)
    if subcommand == "uninstall":
        from telos.init.__main__ import uninstall_main
        return uninstall_main(rest)
    if subcommand == "dashboard":
        return _cmd_dashboard(rest)
    if subcommand == "mode":
        return _cmd_mode(rest)
    if subcommand == "alias":
        return _cmd_alias(rest)
    if subcommand == "replay":
        from telos.replay.__main__ import main as replay_main
        return replay_main(rest)
    if subcommand == "showcase":
        from telos.scripts.showcase import main as showcase_main
        return showcase_main(rest)
    if subcommand in HARNESS_NAMES:
        return _cmd_launch_harness(subcommand)

    print(f"unknown subcommand: {subcommand}", file=sys.stderr)
    _print_usage()
    return 2


# ---------------------------------------------------------------------------
# gateway helpers
# ---------------------------------------------------------------------------

def _ensure_gateway(*, auto_start: bool = True):
    """Ensure the gateway is running; return its ``GatewayState`` (returns ``None`` if unavailable)."""
    from telos.cli_menu import confirm, is_interactive
    from telos.gateway import daemon

    state = daemon.read_state()
    if state is not None:
        return state
    if not auto_start:
        return None
    if is_interactive() and not confirm("gateway is not running, start it now?", default=True):
        return None
    try:
        state = daemon.start_detached()
        print(f"✓ gateway started → {state.base_url()}  (mode={state.mode})")
        return state
    except RuntimeError as e:
        print(f"error: failed to start gateway: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# telos (bare command)
# ---------------------------------------------------------------------------

def _cmd_bare() -> int:
    """Bare ``telos``: enter the favorite harness, or pop a menu to pick one."""
    from telos.cli_menu import select_from
    from telos.config import load_config
    from telos.harnesses import HARNESS_SPECS, detect_installed

    cfg = load_config()

    if cfg.favorite_harness and cfg.favorite_harness in HARNESS_SPECS:
        return _cmd_launch_harness(cfg.favorite_harness)

    installed = detect_installed(cfg.harness_executables)
    if not installed:
        print("No installed harness CLI detected.")
        print(f"telos supports: {', '.join(HARNESS_NAMES)}")
        print("Install one of them and run telos again, or use telos alias <harness> to specify one.")
        return 1

    options = [(s.name, f"{s.display_name}  ({s.name})") for s in installed]
    try:
        chosen = select_from(options, prompt="Select a harness to enter:")
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return _cmd_launch_harness(chosen)


def _cmd_launch_harness(name: str) -> int:
    """Resolve the harness executable, inject the gateway environment, and ``exec`` into its CLI."""
    from telos.config import load_config
    from telos.harnesses import executable_path, gateway_env, get_spec

    try:
        spec = get_spec(name)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    cfg = load_config()
    exe = executable_path(spec, cfg.harness_executables)
    if exe is None:
        from telos.harnesses import resolve_executable
        want = resolve_executable(spec, cfg.harness_executables)
        print(f"error: cannot find the executable {want!r} for {spec.display_name}.",
              file=sys.stderr)
        print(f"       Install it and retry, or specify the correct command name in "
              f"the harness_executables of ~/.telos/config.json.", file=sys.stderr)
        return 1

    state = _ensure_gateway()
    base_url = state.base_url() if state else cfg.gateway.base_url()

    child_env = os.environ.copy()
    child_env.update(gateway_env(spec, base_url))

    print(f"→ entering {spec.display_name} ({spec.env_var}={base_url})")
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvpe(exe, [exe], child_env)  # does not return
    except OSError as e:  # noqa: BLE001
        print(f"error: failed to launch {exe}: {e}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# telos dashboard
# ---------------------------------------------------------------------------

def _cmd_dashboard(rest: list[str]) -> int:
    """``telos dashboard [restart|reset]``.

    Bare: gateway running → open the live dashboard; otherwise build static HTML.
    ``restart``: restart the gateway that serves the dashboard, then reopen it.
    ``reset``:   clear the usage log → zero the dashboard (``--hard`` skips backup).
    """
    verb = rest[0] if rest and not rest[0].startswith("-") else None
    flags = rest[1:] if verb else rest
    no_open = "--no-open" in flags
    force_static = "--static" in flags

    if verb is not None and verb not in ("restart", "reset"):
        print(f"error: unknown dashboard verb {verb!r}; expected 'restart' or 'reset'",
              file=sys.stderr)
        return 2

    from telos.config import load_config
    from telos.gateway import control, daemon

    if verb == "restart":
        return _cmd_dashboard_restart(no_open=no_open)
    if verb == "reset":
        return _cmd_dashboard_reset(hard="--hard" in flags)

    state = daemon.read_state()
    if state is not None and not force_static:
        url = control.dashboard_url(state.host, state.port)
        print(f"dashboard → {url}")
        if not no_open:
            webbrowser.open(url)
        return 0

    # gateway not running: build static HTML.
    from telos.config import telos_home
    from telos.scripts.build_savings_dashboard import main as dash_main

    cfg = load_config()
    usage_log = cfg.gateway.resolved_usage_log()
    if not usage_log.exists():
        print(f"usage log does not exist: {usage_log}")
        print("Run a few requests with telos gateway start first, then view the dashboard.")
        return 1

    out = telos_home() / "savings.html"
    rc = dash_main(["--usage-log", str(usage_log), "--out", str(out)])
    if rc != 0:
        return rc
    print(f"dashboard → file://{out}")
    if not no_open:
        webbrowser.open(f"file://{out}")
    return 0


def _cmd_dashboard_restart(*, no_open: bool) -> int:
    """Restart the gateway process that serves the dashboard, then reopen it.

    The dashboard is an endpoint of the gateway server, so "restarting the
    dashboard" means cycling the gateway: it picks up the latest code, config,
    and usage log, and serves a fresh page.
    """
    from telos.config import load_config
    from telos.gateway import daemon

    cfg = load_config()
    running = daemon.read_state() is not None
    try:
        if running:
            state = daemon.restart(config=cfg)
            print(f"✓ gateway restarted → {state.base_url()}  (mode={state.mode})")
        else:
            state = daemon.start_detached(config=cfg)
            print(f"✓ gateway was not running — started → {state.base_url()}  "
                  f"(mode={state.mode})")
    except RuntimeError as e:
        print(f"error: failed to restart gateway: {e}", file=sys.stderr)
        return 1

    url = state.dashboard_url()
    print(f"dashboard → {url}")
    if not no_open:
        webbrowser.open(url)
    return 0


def _cmd_dashboard_reset(*, hard: bool) -> int:
    """``telos dashboard reset [--hard]`` — clear the usage log → zero the dashboard.

    Gateway running → hot-reset over the loopback control endpoint (no restart).
    Gateway stopped, or the control endpoint does not answer → rotate the
    usage-log file on disk directly.

    By default the old log is rotated to a timestamped ``.bak`` sibling so the
    data stays recoverable; ``--hard`` deletes it outright.
    """
    from telos.cli_menu import confirm, is_interactive
    from telos.config import load_config
    from telos.gateway import control, daemon

    if is_interactive() and not confirm(
        "clear the usage log and zero the dashboard?", default=False
    ):
        print("aborted.")
        return 0

    state = daemon.read_state()
    if state is not None:
        try:
            res = control.post_reset(state.host, state.port,
                                     keep_backup=not hard)
        except RuntimeError as e:
            # Gateway is registered but its control endpoint did not answer
            # (commonly: a gateway started before the reset route existed).
            # Fall back to rotating the log file directly.
            print(f"warning: {e}", file=sys.stderr)
            print("falling back to clearing the usage-log file directly…",
                  file=sys.stderr)
            return _reset_usage_log_file(load_config(), hard=hard,
                                         restart_hint=True)
        status = res.get("status", "reset")
        cleared = res.get("lines_cleared", 0)
        backup = res.get("backup")
        if status == "already empty":
            print("✓ usage log is already empty — nothing to reset.")
        else:
            print(f"✓ dashboard reset — {cleared} line(s) cleared "
                  f"(gateway hot-reset, no restart needed).")
        if backup:
            print(f"  previous log backed up → {backup}")
        return 0

    # gateway not running: rotate the usage-log file directly.
    return _reset_usage_log_file(load_config(), hard=hard, restart_hint=False)


def _reset_usage_log_file(cfg, *, hard: bool, restart_hint: bool) -> int:
    """Rotate (or delete) the usage-log file on disk → zero the dashboard."""
    import time

    usage_log = cfg.gateway.resolved_usage_log()
    if not usage_log.exists() or usage_log.stat().st_size == 0:
        print(f"✓ usage log is already empty — nothing to reset: {usage_log}")
        return 0

    lines = sum(1 for _ in usage_log.open())
    backup_path = None
    try:
        if hard:
            usage_log.unlink()
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            backup_path = usage_log.with_name(f"{usage_log.name}.{stamp}.bak")
            usage_log.replace(backup_path)
        usage_log.touch()
    except OSError as e:
        print(f"error: failed to reset usage log: {e}", file=sys.stderr)
        return 1

    print(f"✓ dashboard reset — {lines} line(s) cleared.")
    if backup_path is not None:
        print(f"  previous log backed up → {backup_path}")
    if restart_hint:
        print("  note: run 'telos gateway restart' so the gateway picks up "
              "the change and the new control endpoint.")
    return 0


# ---------------------------------------------------------------------------
# telos mode
# ---------------------------------------------------------------------------

_MODE_CHOICES = [
    ("none", "none   —— pure passthrough, no optimization"),
    ("telos", "telos  —— prefix caching only (telos-only)"),
    ("rtk", "rtk    —— tool-output filtering only"),
    ("both", "both   —— prefix caching + tool-output filtering (recommended)"),
]


def _cmd_mode(rest: list[str]) -> int:
    """Switch the default optimization mode: write config + hot-update the running gateway."""
    from telos.cli_menu import select_from
    from telos.config import load_config, update_config
    from telos.gateway import control, daemon
    from telos.output_filter import MODE_LABELS

    if rest:
        label = rest[0]
        if label not in MODE_LABELS:
            print(f"error: unknown mode {label!r}; options: {', '.join(MODE_LABELS)}",
                  file=sys.stderr)
            return 2
    else:
        cfg = load_config()
        default_index = next(
            (i for i, (v, _) in enumerate(_MODE_CHOICES) if v == cfg.mode), 1)
        try:
            label = select_from(_MODE_CHOICES, prompt="Select the optimization mode:",
                                 default_index=default_index)
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    update_config(mode=label)
    print(f"✓ default mode saved as {label} (written to ~/.telos/config.json)")

    state = daemon.read_state()
    if state is not None:
        try:
            confirmed = control.post_mode(state.host, state.port, label)
            print(f"✓ the running gateway was hot-updated to {confirmed} (no restart needed)")
        except RuntimeError as e:
            print(f"warning: failed to hot-update the gateway: {e}", file=sys.stderr)
            print("        the new mode will be used the next time the gateway starts.")
    else:
        print("gateway is not running; takes effect on next start.")
    return 0


# ---------------------------------------------------------------------------
# telos alias
# ---------------------------------------------------------------------------

def _cmd_alias(rest: list[str]) -> int:
    """Set the harness the bare ``telos`` enters by default."""
    from telos.config import update_config

    if not rest:
        from telos.config import load_config
        cfg = load_config()
        cur = cfg.favorite_harness or "(not set)"
        print(f"current default harness: {cur}")
        print(f"usage: telos alias <{'|'.join(HARNESS_NAMES)}>")
        return 0

    harness = rest[0]
    if harness not in HARNESS_NAMES:
        print(f"error: unknown harness {harness!r}; options: {', '.join(HARNESS_NAMES)}",
              file=sys.stderr)
        return 2
    update_config(favorite_harness=harness)
    print(f"✓ default harness set to {harness}; from now on just type telos to enter it.")
    return 0


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

def _print_usage() -> None:
    print(
        "usage: telos [<subcommand>] [...]\n"
        "\n"
        "Without a subcommand: select and enter a harness CLI.\n"
        "\n"
        "subcommands:\n"
        "  <harness>   directly enter a harness (claude-code / codex / openclaw / hermes)\n"
        "  init        auto-detect harnesses, inject config, start the gateway\n"
        "  uninstall   undo the injection from 'init' (all harnesses, or one via --harness; --purge fully removes telos)\n"
        "  gateway     start / stop / view the gateway (start|stop|status|restart)\n"
        "  dashboard   open the saved-token / saved-$ dashboard in a browser (dashboard restart restarts it; dashboard reset zeroes it)\n"
        "  mode        switch the optimization mode (none|telos|rtk|both), hot-updates the running gateway\n"
        "  alias       set the harness the bare telos enters by default\n"
        "  replay      replay a recorded session across multiple modes for a controlled A/B comparison\n"
        "  showcase    offline narrated demo + interactive playground (--interactive / --cast)\n"
        "\n"
        "options:\n"
        "  -V, --version   print the installed telos version and exit\n"
        "  -h, --help      show this help and exit\n"
        "\n"
        "examples:\n"
        "  telos init\n"
        "  telos uninstall\n"
        "  telos gateway start --port 7171\n"
        "  telos mode both\n"
        "  telos alias claude-code\n"
        "  telos                       # enter the favorite harness\n"
        "  telos dashboard\n"
        "  telos dashboard restart\n"
        "  telos dashboard reset\n"
    )


if __name__ == "__main__":
    sys.exit(main())
