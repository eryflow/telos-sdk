"""``python -m telos.init`` / ``telos init`` entry point.

Default (no arguments) flow —— this is the headline feature:

1. Detect which harness CLIs are installed locally (claude-code / codex /
   openclaw / hermes);
2. Inject config pointing at the gateway for each detected harness;
3. Start the gateway in the background;
4. Print the gateway and dashboard addresses.

Single-point operations are also supported: ``--harness <name>`` and
``--status``. To undo the injection, use the top-level ``telos uninstall``
command (which lives in :mod:`telos.cli`).
"""

from __future__ import annotations

import argparse
import difflib
import shutil
import subprocess
import sys

from telos.config import (
    config_path,
    disable_harness_trace,
    enable_harness_trace,
    load_config,
    telos_home,
)
from telos.harnesses import HARNESS_NAMES, detect_installed
from telos.init import INSTALLERS
from telos.init.base import InstallResult


def _render(result: InstallResult) -> str:
    lines = [f"[{result.agent}] {result.action}"]
    if result.changed_files:
        lines.append("  changed:")
        for p in result.changed_files:
            lines.append(f"    - {p}")
    if result.backups:
        lines.append("  backups:")
        for p in result.backups:
            lines.append(f"    - {p}")
    for note in result.notes:
        lines.append(f"  ▸ {note}")
    return "\n".join(lines)


def _make_installer(
    name: str,
    gateway_url: str,
    *,
    dsh_profile: str = "web",
    dsh_executable: str | None = None,
    replace_telemetry_backend: bool = False,
):
    factory = INSTALLERS[name]
    if name == "deepseek-harness":
        return factory(
            proxy_url=gateway_url,
            profile=dsh_profile,
            dsh_executable=(
                dsh_executable
                or load_config().harness_executables.get(name)
                or "dsh"
            ),
            replace_telemetry_backend=replace_telemetry_backend,
        )
    return factory(proxy_url=gateway_url)


def _harness_arg(value: str) -> str:
    """argparse ``type`` for ``--harness``: validate against :data:`INSTALLERS`,
    with a "did you mean" hint instead of a bare ``invalid choice``.

    Used in place of ``choices=`` so a near miss like ``claude`` (or ``claude code``,
    which the shell splits into two args) yields ``Did you mean 'claude-code'?``
    rather than an opaque error.
    """
    if value in INSTALLERS:
        return value
    suggestion = difflib.get_close_matches(value, INSTALLERS.keys(), n=1)
    hint = f" Did you mean {suggestion[0]!r}?" if suggestion else ""
    raise argparse.ArgumentTypeError(
        f"unknown harness {value!r}.{hint} Options: {', '.join(sorted(INSTALLERS))}"
    )


def _is_injected(
    name: str, gateway_url: str = "", *, dsh_profile: str = "web"
) -> bool:
    """Whether telos currently has its gateway redirect injected for ``name``.

    ``gateway_url`` must be the address the injected configs are expected to
    point at: openclaw/hermes match their route URL exactly, so an empty/wrong
    host would misreport an injected harness as "not connected". Pass the
    running daemon's URL (else the config default) — see ``_resolve_gateway_url``.

    On error (e.g. a malformed config file) returns ``True`` so the uninstall step
    still runs and surfaces the underlying error rather than silently skipping it.
    """
    try:
        return _make_installer(
            name, gateway_url, dsh_profile=dsh_profile
        ).status().already_installed
    except Exception:  # noqa: BLE001 - a broken config shouldn't hide the harness
        return True


def _run_one(
    name: str,
    gateway_url: str,
    *,
    uninstall: bool,
    status: bool,
    dsh_profile: str = "web",
    dsh_executable: str | None = None,
    replace_telemetry_backend: bool = False,
) -> tuple[int, InstallResult | None]:
    try:
        installer = _make_installer(
            name,
            gateway_url,
            dsh_profile=dsh_profile,
            dsh_executable=dsh_executable,
            replace_telemetry_backend=replace_telemetry_backend,
        )
        if status:
            result = installer.status()
        elif uninstall:
            result = installer.uninstall()
        else:
            result = installer.install()
    except Exception as e:  # noqa: BLE001
        print(f"[{name}] error: {e}", file=sys.stderr)
        return 1, None
    print(_render(result))
    return 0, result


def _start_gateway(*, config_changed: bool) -> None:
    """Start the gateway in the background after injection completes, and print the address.

    If the gateway is already running AND an installer mutated ``~/.telos/config.json``
    (e.g. registered a new upstream slug), restart it so the new config takes effect —
    otherwise the gateway keeps serving with its boot-time in-memory upstreams table
    and 404s on the freshly-added slug.
    """
    from telos.gateway import daemon

    existing = daemon.read_state()
    try:
        if existing is not None and config_changed:
            state = daemon.restart()
            print()
            print(f"✓ gateway restarted to pick up the new ~/.telos/config.json → {state.base_url()}  (mode={state.mode})")
        else:
            state = daemon.start_detached()
            print()
            print(f"✓ gateway running → {state.base_url()}  (mode={state.mode})")
    except RuntimeError as e:
        print(f"warning: gateway failed to start: {e}", file=sys.stderr)
        print("        you can run it manually later: telos gateway start")
        return
    print(f"  dashboard     → {state.dashboard_url()}")
    print("  open the dashboard: telos dashboard")


def _resolve_gateway_url(args_gateway_url: str | None, cfg) -> tuple[str, str]:
    """Decide which URL the installers should patch user configs to point at.

    Priority:
      1. Explicit ``--gateway-url`` from the CLI (user override).
      2. A currently-running daemon's URL (so installer-patched URLs match the
         port the daemon is actually listening on, even if ``~/.telos/config.json``
         records a different default).
      3. ``cfg.gateway.base_url()`` (config default, typically 127.0.0.1:7171).

    Returns ``(url, source)`` — ``source`` is a short label used in startup
    logs so the user can see where the URL came from.
    """
    if args_gateway_url:
        return args_gateway_url, "--gateway-url"
    try:
        from telos.gateway import daemon
        state = daemon.read_state()
    except Exception:  # noqa: BLE001 - daemon module is best-effort here
        state = None
    if state is not None:
        return state.base_url(), "running daemon"
    return cfg.gateway.base_url(), "config default"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="telos init",
        description="Automatically detect harnesses, inject gateway config, start the gateway.",
    )
    # --harness is primary; --agent is a hidden alias for backward compatibility.
    parser.add_argument("--harness", "--agent", dest="harness", default=None,
                        type=_harness_arg, metavar="NAME",
                        help="operate only on the specified harness "
                             f"({', '.join(sorted(INSTALLERS))}); default: auto-detect all")
    parser.add_argument("--gateway-url", "--proxy-url", dest="gateway_url",
                        default=None,
                        help="gateway address (default: running daemon if any, else ~/.telos/config.json)")
    parser.add_argument("--status", action="store_true", help="view only, do not change files")
    parser.add_argument("--dsh-profile", default="web", metavar="NAME",
                        help="DeepSeek Harness profile to patch (default: web)")
    parser.add_argument("--dsh-executable", default=None, metavar="PATH",
                        help="DeepSeek Harness CLI path (use when another command named dsh is on PATH)")
    parser.add_argument("--replace-telemetry-backend", action="store_true",
                        help="allow DeepSeek Harness init to disable its existing telemetry backend")
    parser.add_argument("--no-gateway", action="store_true",
                        help="only inject config, do not auto-start the gateway")
    args = parser.parse_args(argv)

    cfg = load_config()
    gateway_url, source = _resolve_gateway_url(args.gateway_url, cfg)
    if source == "running daemon":
        print(f"using gateway URL {gateway_url} (from {source})")

    # ---- Determine the target harness list ----
    if args.harness:
        targets = [args.harness]
    elif args.status:
        # When unspecified, status applies to all known harnesses.
        targets = [n for n in HARNESS_NAMES if n in INSTALLERS]
    else:
        detected = detect_installed(cfg.harness_executables)
        targets = [s.name for s in detected if s.name in INSTALLERS]
        if not targets:
            print("No installed harness CLI detected.")
            print(f"telos supports: {', '.join(HARNESS_NAMES)}")
            print("Install one of them and re-run telos init, "
                  "or use telos init --harness <name> to specify one.")
            return 1
        print(f"Detected harnesses: {', '.join(targets)}\n")

    # ---- Execute one by one ----
    rc = 0
    telos_config_path = config_path()
    config_changed = False
    for name in targets:
        sub_rc, result = _run_one(
            name,
            gateway_url,
            uninstall=False,
            status=args.status,
            dsh_profile=args.dsh_profile,
            dsh_executable=args.dsh_executable,
            replace_telemetry_backend=args.replace_telemetry_backend,
        )
        rc |= sub_rc
        if result is not None and telos_config_path in result.changed_files:
            config_changed = True
        if result is not None and not args.status:
            model_span_source = {
                "codex": "gateway",
                "deepseek-harness": "adapter",
            }.get(name)
            _, trace_changed = enable_harness_trace(
                name, model_span_source=model_span_source
            )
            config_changed |= trace_changed
            print(f"  ▸ local Trace capture enabled for {name}")

    # ---- Start the gateway after a successful install ----
    if not args.status and not args.no_gateway and rc == 0:
        _start_gateway(config_changed=config_changed)

    # ---- cc-switch coexistence notice ----
    if not args.status:
        try:
            from telos.init import cc_switch
            if cc_switch.is_installed():
                print()
                print("ℹ cc-switch detected. telos chains its gateway in front of whichever "
                      "provider cc-switch has active.")
                print("  After switching providers in cc-switch, run `telos ccswitch sync` "
                      "to re-chain telos in front of the new choice.")
        except Exception:  # noqa: BLE001 — never let the notice break init
            pass

    return rc


def _stop_gateway() -> None:
    """Stop the background gateway daemon if it is running."""
    try:
        from telos.gateway import daemon
        stopped = daemon.stop()
    except Exception as e:  # noqa: BLE001 - daemon module is best-effort here
        print(f"warning: failed to stop the gateway: {e}", file=sys.stderr)
        return
    if stopped:
        print("✓ gateway daemon stopped")
    else:
        print("  gateway daemon was not running")


def _remove_telos_home() -> None:
    """Delete the ``~/.telos`` directory (local config + state + logs)."""
    home = telos_home()
    if not home.exists():
        print(f"  {home} does not exist, nothing to remove")
        return
    try:
        shutil.rmtree(home)
    except Exception as e:  # noqa: BLE001
        print(f"warning: failed to remove {home}: {e}", file=sys.stderr)
        return
    print(f"✓ removed {home}")


def _pip_uninstall_self() -> None:
    """Uninstall the installed ``telos-sdk`` distribution via pip.

    The current process keeps running off already-imported modules, so this is
    safe to call from within ``telos uninstall`` — pip only removes the on-disk
    files.
    """
    cmd = [sys.executable, "-m", "pip", "uninstall", "-y", "telos-sdk"]
    print(f"$ {' '.join(cmd)}")
    try:
        rc = subprocess.call(cmd)
    except Exception as e:  # noqa: BLE001
        print(f"warning: failed to run pip uninstall: {e}", file=sys.stderr)
        return
    if rc == 0:
        print("✓ telos-sdk uninstalled")
    else:
        print(f"warning: pip uninstall exited with code {rc}", file=sys.stderr)


def uninstall_main(argv: list[str] | None = None) -> int:
    """``telos uninstall`` entry point — undo the gateway config injection.

    By default it reverts every harness telos has actually injected (harnesses
    that were never connected are skipped), restoring the pre-``telos init`` state.
    Pass ``--harness <name>`` to scope to one.

    With ``--purge`` it goes further and fully removes telos: after reverting the
    harness configs it stops the gateway daemon, deletes ``~/.telos`` and
    uninstalls the ``telos-sdk`` package itself.
    """
    parser = argparse.ArgumentParser(
        prog="telos uninstall",
        description="Undo the gateway config injection for each detected harness.",
    )
    parser.add_argument("--harness", "--agent", dest="harness", default=None,
                        type=_harness_arg, metavar="NAME",
                        help="operate only on the specified harness "
                             f"({', '.join(sorted(INSTALLERS))}); default: every injected harness")
    parser.add_argument("--purge", action="store_true",
                        help="fully remove telos: also stop the gateway, delete ~/.telos and uninstall the telos-sdk package")
    parser.add_argument("--dsh-profile", default="web", metavar="NAME",
                        help="DeepSeek Harness profile to unpatch (default: web)")
    parser.add_argument("--dsh-executable", default=None, metavar="PATH",
                        help="DeepSeek Harness CLI path (use when another command named dsh is on PATH)")
    parser.add_argument("-y", "--yes", dest="assume_yes", action="store_true",
                        help="skip the confirmation prompt for --purge")
    args = parser.parse_args(argv)

    if args.purge and args.harness:
        print("--purge applies to the whole installation; ignoring --harness.",
              file=sys.stderr)
        args.harness = None

    if args.harness:
        requested = [args.harness]
    else:
        requested = [n for n in HARNESS_NAMES if n in INSTALLERS]

    # --purge is destructive (deletes ~/.telos and uninstalls the package).
    # Confirm before touching anything.
    if args.purge:
        from telos.cli_menu import confirm
        if not args.assume_yes and not confirm(
            "This will revert all harness configs, stop the gateway, delete "
            f"{telos_home()} and uninstall telos-sdk. Continue?",
            default=False,
        ):
            print("aborted.")
            return 1

    # Only revert harnesses telos has actually injected — uninstalling a harness
    # that was never connected is a confusing no-op. 'generic' holds no state (it
    # just prints advice), so it always runs when explicitly requested.
    # Resolve the gateway URL the injected configs should point at so the
    # exact-match installers (openclaw/hermes) recognize their own routes.
    inject_url, _ = _resolve_gateway_url(None, load_config())
    targets: list[str] = []
    skipped: list[str] = []
    for name in requested:
        if name == "generic" or _is_injected(
            name, inject_url, dsh_profile=args.dsh_profile
        ):
            targets.append(name)
        else:
            skipped.append(name)

    for name in skipped:
        print(f"  ▸ skip {name}: not connected to telos (no injection found)")
        _, trace_changed = disable_harness_trace(name)
        if trace_changed:
            print(f"  ▸ local Trace disabled for {name}; history retained")

    if not targets and not args.purge:
        if args.harness:
            print(f"{args.harness} is not currently connected to telos; nothing to undo.")
        else:
            print("No telos-injected harness found; nothing to undo.")
        return 0

    rc = 0
    reverted_any = False
    for name in targets:
        # gateway URL is irrelevant for uninstall; pass an empty placeholder.
        sub_rc, result = _run_one(
            name,
            "",
            uninstall=True,
            status=False,
            dsh_profile=args.dsh_profile,
            dsh_executable=args.dsh_executable,
        )
        rc |= sub_rc
        if result is not None and result.changed_files:
            reverted_any = True
        if result is not None:
            _, trace_changed = disable_harness_trace(name)
            if trace_changed:
                print(f"  ▸ local Trace disabled for {name}; history retained")

    if reverted_any:
        print()
        print(
            "⚠ Restart any running harness session (e.g. Claude Code) for this to "
            "take effect — the gateway redirect is read once at startup, so a process "
            "started before uninstall stays connected until you relaunch it."
        )

    if args.purge:
        print()
        _stop_gateway()
        _remove_telos_home()
        _pip_uninstall_self()

    return rc


if __name__ == "__main__":
    sys.exit(main())
