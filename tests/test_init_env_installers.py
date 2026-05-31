"""Registry factory mapping for the init installers.

Note: this file previously tested ``EnvInstaller`` (``telos.init.anthropic_env``),
but that class is no longer wired into the ``INSTALLERS`` registry -- ``openclaw``
and ``hermes`` resolve to the config-patching ``OpenClawInstaller`` /
``HermesInstaller`` (see ``init/__init__.py``). Those stale env-installer tests
were removed; their real behaviour is covered by ``test_init_openclaw.py`` /
``test_init_hermes.py``. What remains is the one assertion that still matters:
the registry maps each name to its current installer class.
"""

from __future__ import annotations

from telos.init import INSTALLERS
from telos.init.claude_code import ClaudeCodeInstaller
from telos.init.codex import CodexInstaller
from telos.init.hermes import HermesInstaller
from telos.init.openclaw import OpenClawInstaller


def test_registry_maps_names_to_installer_classes() -> None:
    assert INSTALLERS["claude-code"] is ClaudeCodeInstaller
    assert INSTALLERS["openclaw"] is OpenClawInstaller
    assert INSTALLERS["hermes"] is HermesInstaller
    assert INSTALLERS["codex"] is CodexInstaller
