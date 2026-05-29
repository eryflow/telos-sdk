"""``python -m telos.init`` / ``telos init`` entry point.

Default (no arguments) flow —— this is the headline feature:

1. Detect which harness CLIs are installed locally (claude-code / codex /
   openclaw / hermes);
2. Inject config pointing at the gateway for each detected harness;
3. Start the gateway in the background;
4. Print the gateway and dashboard addresses.

Single-point operations are also supported: ``--harness <name>``,
``--uninstall``, ``--status``.
"""

from __future__ import annotations

import argparse
import sys

from telos.config import config_path, load_config
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


def _make_installer(name: str, gateway_url: str):
    factory = INSTALLERS[name]
    return factory(proxy_url=gateway_url)


def _run_one(name: str, gateway_url: str, *, uninstall: bool, status: bool) -> tuple[int, InstallResult | None]:
    try:
        installer = _make_installer(name, gateway_url)
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
                        choices=sorted(INSTALLERS.keys()),
                        help="operate only on the specified harness (default: auto-detect all)")
    parser.add_argument("--gateway-url", "--proxy-url", dest="gateway_url",
                        default=None,
                        help="gateway address (default: running daemon if any, else ~/.telos/config.json)")
    parser.add_argument("--uninstall", action="store_true", help="undo the injection")
    parser.add_argument("--status", action="store_true", help="view only, do not change files")
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
    elif args.status or args.uninstall:
        # When unspecified, status/uninstall apply to all known harnesses.
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
        sub_rc, result = _run_one(name, gateway_url, uninstall=args.uninstall,
                                  status=args.status)
        rc |= sub_rc
        if result is not None and telos_config_path in result.changed_files:
            config_changed = True

    # ---- Start the gateway after a successful install ----
    if not args.uninstall and not args.status and not args.no_gateway and rc == 0:
        _start_gateway(config_changed=config_changed)

    return rc


if __name__ == "__main__":
    sys.exit(main())
