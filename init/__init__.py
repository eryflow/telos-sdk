"""TELOS agent connectors —— ``python -m telos.init``.

One installer per harness:

- ``claude-code``: patches the ``env`` field of ``~/.claude/settings.json``
  (Claude Code reads ``ANTHROPIC_BASE_URL`` from there at launch).
- ``openclaw``: patches the primary provider's ``baseUrl`` in
  ``~/.openclaw/openclaw.json`` to route through the local telos gateway
  (and mirrors the original URL into telos's upstream table so the gateway
  can forward verbatim).
- ``hermes``: patches the top-level ``model.base_url`` in
  ``~/.hermes/config.yaml`` (analogous pattern).
- ``codex``: patches ``~/.codex/config.toml`` with a TELOS custom provider.
- ``generic``: prints a set of shell ``export`` commands the user can add
  to their rc file.
"""

from __future__ import annotations

from typing import Callable

from telos.init.base import AgentInstaller, InstallResult
from telos.init.claude_code import ClaudeCodeInstaller
from telos.init.codex import CodexInstaller
from telos.init.generic import GenericInstaller
from telos.init.hermes import HermesInstaller
from telos.init.openclaw import OpenClawInstaller

# name → factory; the factory signature is uniformly ``(*, proxy_url=...) -> AgentInstaller``.
InstallerFactory = Callable[..., AgentInstaller]

INSTALLERS: dict[str, InstallerFactory] = {
    "claude-code": ClaudeCodeInstaller,
    "codex": CodexInstaller,
    "openclaw": OpenClawInstaller,
    "hermes": HermesInstaller,
    "generic": GenericInstaller,
}

__all__ = ["AgentInstaller", "InstallResult", "INSTALLERS", "InstallerFactory"]
