"""`telos init --harness` enables local Trace for that Harness."""

from __future__ import annotations

import os

from telos.config import load_config
from telos.init.__main__ import main as init_main, uninstall_main


def test_init_and_uninstall_toggle_trace_registration(tmp_path) -> None:
    previous = os.environ.get("TELOS_HOME")
    os.environ["TELOS_HOME"] = str(tmp_path / ".telos")
    try:
        assert init_main(["--harness", "generic", "--no-gateway"]) == 0
        registered = load_config().trace_harnesses["generic"]
        assert registered["enabled"] is True
        assert registered["capture"] == "full"
        token = registered["reporter_token"]

        assert uninstall_main(["--harness", "generic"]) == 0
        disabled = load_config().trace_harnesses["generic"]
        assert disabled["enabled"] is False
        assert disabled["reporter_token"] == token
    finally:
        if previous is None:
            os.environ.pop("TELOS_HOME", None)
        else:
            os.environ["TELOS_HOME"] = previous
