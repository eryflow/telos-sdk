"""Small CLI used by Harness hooks to report facts the gateway cannot see."""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from telos.config import load_config
from telos.gateway import control, daemon
from telos.trace_store import REPORTER_EVENT_KINDS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="telos report",
        description="Append one Harness lifecycle event to the local Trace.",
    )
    parser.add_argument("--harness", required=True)
    parser.add_argument("--session", required=True, dest="session_id")
    parser.add_argument("--event", required=True, choices=sorted(REPORTER_EVENT_KINDS))
    parser.add_argument("--event-id", default=None)
    parser.add_argument("--observed-at", default=None)
    parser.add_argument("--data", default="{}", metavar="JSON")
    args = parser.parse_args(argv)

    try:
        data = json.loads(args.data)
    except json.JSONDecodeError as e:
        print(f"error: --data is not valid JSON: {e}", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print("error: --data must be a JSON object", file=sys.stderr)
        return 2

    policy = load_config().trace_harnesses.get(args.harness)
    token = policy.get("reporter_token") if policy else None
    if not policy or policy.get("enabled") is not True or not isinstance(token, str):
        print(
            f"error: {args.harness!r} is not registered; run "
            f"telos init --harness {args.harness}",
            file=sys.stderr,
        )
        return 1
    state = daemon.read_state()
    if state is None:
        print("error: gateway is not running", file=sys.stderr)
        return 1

    payload = {
        "harness": args.harness,
        "reporter_token": token,
        "session_id": args.session_id,
        "events": [{
            "event_id": args.event_id or str(uuid.uuid4()),
            "kind": args.event,
            "observed_at": args.observed_at,
            "data": data,
        }],
    }
    try:
        result = control.post_reporter_events(state.host, state.port, payload)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    accepted = result.get("accepted") or []
    seq = accepted[0].get("seq") if accepted else "?"
    print(f"reported {args.event} for {args.session_id} (seq={seq})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
