"""Unit tests for ``telos.refpool.RefPool``.

The ref-pool is the pointer table for large content. Its whole job is byte
stability: a slug is frozen on first registration and ``fold`` swaps the
*payload* without ever touching the slug, so a cache breakpoint anchored after
the ``[ref:slug]`` pointer keeps hitting. These behaviours had only indirect
coverage via the bridge; this file exercises the API directly. See
docs/ARCHITECTURE.md §8.
"""

from __future__ import annotations

import pytest

from telos.ir import Band, TelosBlock, TelosInvariantError
from telos.refpool import RefPool


def _fold_block(slug: str, payload: str = "big document") -> TelosBlock:
    return TelosBlock(
        id=f"blk-{slug}", band=Band.FOLD, kind="text",
        payload=payload, ref_slug=slug,
    )


def test_register_freezes_and_rejects_double() -> None:
    pool = RefPool()
    pool.register("doc-1", _fold_block("doc-1"))
    assert "doc-1" in pool.slugs
    # Re-registration must fail -- the slug is frozen.
    with pytest.raises(TelosInvariantError, match="already registered"):
        pool.register("doc-1", _fold_block("doc-1", "different"))


@pytest.mark.parametrize("bad", ["has space", "with:colon", "bad!char", ""])
def test_register_rejects_invalid_slug(bad: str) -> None:
    pool = RefPool()
    with pytest.raises(TelosInvariantError, match="Invalid ref slug"):
        pool.register(bad, _fold_block(bad or "x"))


def test_register_rejects_non_fold_band() -> None:
    pool = RefPool()
    blk = TelosBlock(id="b", band=Band.PIN, kind="text",
                     payload="x", ref_slug="doc-1")
    with pytest.raises(TelosInvariantError, match="band=FOLD"):
        pool.register("doc-1", blk)


def test_register_rejects_slug_mismatch() -> None:
    pool = RefPool()
    blk = TelosBlock(id="b", band=Band.FOLD, kind="text",
                     payload="x", ref_slug="other")
    with pytest.raises(TelosInvariantError, match="ref_slug"):
        pool.register("doc-1", blk)


def test_register_or_skip_is_idempotent_and_non_clobbering() -> None:
    pool = RefPool()
    pool.register("doc-1", _fold_block("doc-1", "original"))
    assert pool.register_or_skip("doc-1", _fold_block("doc-1")) is False
    pool.fold("doc-1", summary="[folded]")
    # A later turn re-supplies the full payload; register_or_skip must NOT
    # overwrite the folded placeholder.
    pool.register_or_skip("doc-1", _fold_block("doc-1", "original full text"))
    assert pool.to_mapping()["doc-1"].payload == "[folded]"


def test_fold_keeps_slug_swaps_payload() -> None:
    pool = RefPool()
    pool.register("doc-1", _fold_block("doc-1", "the big text"))
    blk = pool.fold("doc-1")
    assert blk.ref_slug == "doc-1"               # slug untouched
    assert blk.payload == "<folded ref:doc-1>"   # default placeholder
    assert pool.to_mapping()["doc-1"].payload == "<folded ref:doc-1>"


def test_fold_uses_summary_when_given() -> None:
    pool = RefPool()
    pool.register("doc-1", _fold_block("doc-1"))
    assert pool.fold("doc-1", summary="<3-line summary>").payload == "<3-line summary>"


def test_fold_unregistered_raises() -> None:
    pool = RefPool()
    with pytest.raises(TelosInvariantError, match="unregistered slug"):
        pool.fold("never-registered")


def test_render_blocks_and_slugs_are_lexicographic() -> None:
    pool = RefPool()
    for slug in ("doc-3", "doc-1", "doc-2"):
        pool.register(slug, _fold_block(slug))
    # Registration order was scrambled; render order must be stable (sorted).
    rendered = [b.ref_slug for b in pool.render_blocks()]
    assert rendered == ["doc-1", "doc-2", "doc-3"]


def test_lint_text_fails_fast_on_unregistered_pointer() -> None:
    pool = RefPool()
    pool.register("doc-1", _fold_block("doc-1"))
    pool.lint_text("see [ref:doc-1] for details", "sys")  # registered -> ok
    with pytest.raises(TelosInvariantError, match="Unregistered ref slug"):
        pool.lint_text("see [ref:doc-999] for details", "sys")


def test_lint_blocks_scans_text_payloads() -> None:
    pool = RefPool()
    pool.register("doc-1", _fold_block("doc-1"))
    ok = (TelosBlock(id="a", band=Band.PIN, kind="text", payload="ref [ref:doc-1] here"),)
    pool.lint_blocks(ok, "msg")
    bad = (TelosBlock(id="b", band=Band.PIN, kind="text", payload="ref [ref:missing] here"),)
    with pytest.raises(TelosInvariantError, match="Unregistered ref slug"):
        pool.lint_blocks(bad, "msg")
