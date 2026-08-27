"""``python -m telos.proxy`` entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from telos.config import load_config
from telos.corpus import DEFAULT_CORPUS_DIR
from telos.output_filter import MODE_LABELS, TelosMode
from telos.proxy.server import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="telos.proxy",
        description="TELOS Anthropic reverse proxy."
                    " An agent sets ANTHROPIC_BASE_URL=http://<host>:<port> for zero-intrusion integration.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="listen address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7171,
                        help="listen port (default 7171)")
    parser.add_argument("--upstream", default="https://api.anthropic.com",
                        help="real Anthropic API endpoint")
    parser.add_argument("--usage-log", type=Path, default=None,
                        help="write one jsonl line to this path per call")
    parser.add_argument("--harness", default=None,
                        choices=["openclaw", "hermes", "claude-code"],
                        help="force a specific harness (default: auto-detect). "
                             "claude-code is an alias for hermes.")
    parser.add_argument("--mode", default="telos", choices=list(MODE_LABELS),
                        help="default optimization switches: none=pure passthrough / telos=prefix caching only / "
                             "rtk=tool output filtering only / both=both enabled (default telos). "
                             "A single request can override this with the X-Telos-Mode header.")
    parser.add_argument("--strict", action="store_true",
                        help="return 500 when TELOS fails (default: fall back to passthrough)")
    parser.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR,
                        help=f"conversation corpus directory; records raw requests for telos replay "
                             f"(default {DEFAULT_CORPUS_DIR})")
    parser.add_argument("--no-record", action="store_true",
                        help="disable session recording (enabled by default). It records the raw request "
                             "body, including your prompt / code -- use this switch if you'd rather not "
                             "persist it to disk.")
    parser.add_argument("--dashboard-refresh", type=int, default=5,
                        metavar="SECONDS",
                        help="meta-refresh interval for GET /dashboard, "
                             "0 = disable auto-refresh (default 5 seconds)")
    args = parser.parse_args(argv)

    # Load the named upstream table from the user config so /upstreams/<slug>/...
    # works out of the box (anthropic / openrouter / deepseek by default).
    cfg = load_config()
    # Legacy /v1/messages forwards to ``args.upstream``. When the user has not
    # overridden it on the command line, take the URL from the anthropic entry
    # of the upstream table so a single config edit changes both routes.
    if args.upstream == parser.get_default("upstream"):
        args.upstream = cfg.anthropic_upstream_url()

    run(
        host=args.host,
        port=args.port,
        upstream=args.upstream,
        upstreams=cfg.upstreams,
        usage_log=args.usage_log,
        harness_override=args.harness,
        strict=args.strict,
        dashboard_refresh=args.dashboard_refresh,
        mode=TelosMode.from_label(args.mode),
        corpus_dir=args.corpus_dir,
        record=not args.no_record,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
