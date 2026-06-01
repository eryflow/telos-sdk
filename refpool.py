r"""Ref-pool: the "pointer table" for all large content blocks.

Design points
-------------
- Every ref-pool entry *binds to a stable slug*, and that slug must not change
  for the lifetime of the session. The emit-time lint scans all text blocks for
  ``[ref:<slug>]`` references and raises ``TelosInvariantError`` immediately if
  it finds an unregistered slug.
- On compact, *only the payload changes, not the slug*: the ``[ref:login.py]``
  string stays in place, so the bytes at every reference point in the
  user/assistant content are unchanged —— this is the real implementation of the
  "references fold naturally" idea mentioned in Janus §8.
- Slug naming rules: ``[A-Za-z0-9_\-./]+``, timestamps / version numbers / hashes
  are forbidden, and normalization is case-sensitive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from telos.ir import Band, TelosBlock, TelosInvariantError


SLUG_RE = re.compile(r"^[A-Za-z0-9_\-./]+$")
REF_RE = re.compile(r"\[ref:([A-Za-z0-9_\-./]+)\]")


@dataclass
class RefPool:
    """Session-level ref-pool state.

    The bridge manages it as "one band": it freezes slugs on registration and,
    at emit time, renders them in lexicographic order at the tail of the system
    band (guaranteeing byte stability).
    """

    _entries: dict[str, TelosBlock] = field(default_factory=dict)

    # ------ Registration and replacement ------

    def register(self, slug: str, block: TelosBlock) -> None:
        """Register a slug for the first time; once registered a slug cannot be renamed (violates I3)."""
        if not SLUG_RE.match(slug):
            raise TelosInvariantError(
                f"Invalid ref slug {slug!r}; must match {SLUG_RE.pattern}"
            )
        if slug in self._entries:
            raise TelosInvariantError(
                f"Slug {slug!r} already registered; ref-pool slugs are frozen"
            )
        if block.ref_slug != slug:
            raise TelosInvariantError(
                f"Block.ref_slug ({block.ref_slug!r}) must equal slug ({slug!r})"
            )
        if block.band is not Band.FOLD:
            raise TelosInvariantError(
                "Ref-pool entries must have band=FOLD (foldable on compact)"
            )
        self._entries[slug] = block

    def register_or_skip(self, slug: str, block: TelosBlock) -> bool:
        """Idempotent register —— used when sharing a RefPool across turns.

        - slug not registered → go through the standard register
        - slug already registered → skip (keep the existing entry, which may
          already have been folded into a placeholder)

        Key invariant: the harness produces a ref_pool with full payloads every
        turn; this method prevents the second turn from overwriting an entry that
        the first turn folded back to full content.

        Returns True if it actually registered, False if it skipped.
        """
        if slug in self._entries:
            return False
        self.register(slug, block)
        return True

    def fold(self, slug: str, *, summary: str | None = None) -> TelosBlock:
        """Fold a ref-pool entry into a short placeholder; return the new block.

        The slug stays unchanged, the bytes at ``[ref:slug]`` reference points are
        unchanged → later BP can still hit.
        """
        if slug not in self._entries:
            raise TelosInvariantError(f"Cannot fold unregistered slug {slug!r}")
        old = self._entries[slug]
        placeholder = summary or f"<folded ref:{slug}>"
        new = TelosBlock(
            id=old.id,
            band=Band.FOLD,
            kind="text",
            payload=placeholder,
            ref_slug=slug,
            source_tag="ref-pool/folded",
        )
        self._entries[slug] = new
        return new

    # ------ Query and iteration ------

    @property
    def slugs(self) -> frozenset[str]:
        return frozenset(self._entries.keys())

    def render_blocks(self) -> tuple[TelosBlock, ...]:
        """Render entries in lexicographic slug order; guarantees byte stability across emits."""
        return tuple(self._entries[k] for k in sorted(self._entries.keys()))

    def to_mapping(self) -> dict[str, TelosBlock]:
        return dict(self._entries)

    # ------ Reference lint (§4 / §8.5 L1) ------

    def lint_text(self, text: str, where: str) -> None:
        """Scan all ``[ref:slug]`` in the text; fail-fast on any unregistered slug."""
        for m in REF_RE.finditer(text):
            slug = m.group(1)
            if slug not in self._entries:
                raise TelosInvariantError(
                    f"Unregistered ref slug {slug!r} in {where}; "
                    f"register via Pin() before emitting."
                )

    def lint_blocks(self, blocks: Iterable[TelosBlock], where: str) -> None:
        for blk in blocks:
            if blk.kind == "text" and isinstance(blk.payload, str):
                self.lint_text(blk.payload, f"{where}/block:{blk.id}")
