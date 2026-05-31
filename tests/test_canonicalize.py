"""Determinism tests for the bridge's canonicalization layer.

This is the literal core of TELOS's value: the same logical request, however
its JSON keys or tool array happen to be ordered upstream, must canonicalize to
*byte-identical* output -- otherwise the prefix hash drifts and the KV cache
misses (docs/ARCHITECTURE.md §5.3, R5). These private functions had no direct
coverage before.
"""

from __future__ import annotations

import json

from telos.bridge import (
    _canonicalize_ir,
    _canonicalize_payload,
    _canonicalize_schema,
    _canonicalize_tool_def,
    _tool_sort_key,
)
from telos.ir import Band, TelosBlock, TelosHints, TelosIR, TelosMessage


# --- payload key sorting -------------------------------------------------

def test_payload_sorts_dict_keys_recursively() -> None:
    out = _canonicalize_payload({"b": 1, "a": {"d": 2, "c": 3}})
    assert list(out) == ["a", "b"]
    assert list(out["a"]) == ["c", "d"]


def test_payload_preserves_list_order() -> None:
    # Arrays are sequence-semantic: order must never change.
    assert _canonicalize_payload({"x": ["z", "a", "m"]})["x"] == ["z", "a", "m"]


def test_payload_is_order_independent() -> None:
    a = _canonicalize_payload({"b": 1, "a": 2, "c": {"y": 1, "x": 2}})
    b = _canonicalize_payload({"c": {"x": 2, "y": 1}, "a": 2, "b": 1})
    assert json.dumps(a) == json.dumps(b)


# --- schema: sort set-arrays, never sequence-arrays ----------------------

def test_schema_sorts_required_set_array() -> None:
    out = _canonicalize_schema({"required": ["b", "a", "c"], "type": "object"})
    assert out["required"] == ["a", "b", "c"]


def test_schema_preserves_semantic_arrays() -> None:
    # enum / anyOf order is semantic -- must NOT be sorted.
    out = _canonicalize_schema({"enum": ["z", "a"], "anyOf": [{"b": 1}, {"a": 1}]})
    assert out["enum"] == ["z", "a"]
    assert out["anyOf"] == [{"b": 1}, {"a": 1}]


# --- tool sort key -------------------------------------------------------

def _tool(name: str, *, source: str | None = None, mcp_server: str | None = None) -> TelosBlock:
    extra: dict[str, str] = {}
    if source is not None:
        extra["source"] = source
    if mcp_server is not None:
        extra["mcp_server"] = mcp_server
    return TelosBlock(id=f"t-{name}", band=Band.PIN, kind="tool_def",
                      payload={"name": name, "input_schema": {"type": "object"}},
                      extra=extra)


def test_tool_sort_key_orders_by_source_then_name() -> None:
    keys = [
        _tool_sort_key(_tool("z", source="builtin")),
        _tool_sort_key(_tool("a", source="mcp", mcp_server="srv")),
        _tool_sort_key(_tool("b", source="user")),
        _tool_sort_key(_tool("c")),  # untagged -> default rank (last)
    ]
    # builtin(0) < mcp(1) < user(2) < untagged(3), regardless of name.
    assert keys == sorted(keys)
    assert keys[0][0] == 0 and keys[-1][0] == 3


def test_tool_sort_key_mcp_server_is_tiebreak() -> None:
    a = _tool_sort_key(_tool("t", source="mcp", mcp_server="alpha"))
    b = _tool_sort_key(_tool("t", source="mcp", mcp_server="beta"))
    assert a < b


# --- the headline metamorphic property -----------------------------------

def _ir_with(tools: tuple[TelosBlock, ...]) -> TelosIR:
    return TelosIR(
        session_id="s", tools=tools,
        system=(TelosBlock(id="sys", band=Band.PIN, kind="text", payload="hi"),),
        messages=(TelosMessage(role="user",
                               blocks=(TelosBlock(id="u", band=Band.PIN, kind="text", payload="q"),)),),
        ref_pool={}, hints=TelosHints(engine="anthropic"),
    )


def test_reordered_tools_canonicalize_byte_identical() -> None:
    t1 = _tool("read", source="builtin")
    t2 = _tool("grep", source="builtin")
    t3 = _tool("search", source="mcp", mcp_server="srv")
    forward = _canonicalize_ir(_ir_with((t1, t2, t3)))
    shuffled = _canonicalize_ir(_ir_with((t3, t2, t1)))
    dump = lambda ir: json.dumps([t.payload for t in ir.tools])
    assert dump(forward) == dump(shuffled)


def test_reordered_schema_keys_canonicalize_byte_identical() -> None:
    a = {"name": "write", "description": "w",
         "input_schema": {"type": "object", "required": ["b", "a"],
                          "properties": {"b": {}, "a": {}}}}
    b = {"description": "w", "name": "write",
         "input_schema": {"required": ["a", "b"], "properties": {"a": {}, "b": {}},
                          "type": "object"}}
    assert json.dumps(_canonicalize_tool_def(a)) == json.dumps(_canonicalize_tool_def(b))
