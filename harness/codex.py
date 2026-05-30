"""Codex harness plugin.

Input contract: OpenAI ChatCompletions / Responses wire shape, same as
``harness/telos.py``. This file exists as a separate identity so attribution
is unambiguous and so Codex-specific conventions (chatgpt-mode response
items, reasoning blocks, etc.) can specialize here without polluting the
generic Telos plugin.

Created in 2026-05 alongside the URL-tag attribution mechanism. Currently
identical to ``TelosPlugin`` — specialize as Codex's wire conventions
diverge from the generic OpenAI shape.
"""

from __future__ import annotations

from telos.harness.telos import TelosPlugin


class CodexPlugin(TelosPlugin):
    """Codex's OpenAI-shape parser. See module docstring."""
    pass
