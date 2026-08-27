"""``telos.corpus`` unit tests: recording / loading / listing session corpora."""

from __future__ import annotations

import tempfile
from pathlib import Path

from telos.corpus import list_sessions, load_session, record_call


def _sample_request(text: str) -> dict:
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
    }


def test_record_and_load_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        cd = Path(td)
        record_call(cd, "sess-1", 1, _sample_request("first"))
        record_call(cd, "sess-1", 2, _sample_request("second"))
        turns = load_session(cd, "sess-1")
        assert len(turns) == 2
        assert turns[0]["call_index"] == 1
        assert turns[1]["request"]["messages"][0]["content"][0]["text"] == "second"
    print("✓ test_record_and_load_roundtrip")


def test_load_session_preserves_append_order_across_gateway_restarts() -> None:
    """A reset process-local call_index must not reorder a persisted session."""
    with tempfile.TemporaryDirectory() as td:
        cd = Path(td)
        record_call(cd, "s", 98, _sample_request("before restart"))
        record_call(cd, "s", 1, _sample_request("after restart"))
        turns = load_session(cd, "s")
        assert [t["call_index"] for t in turns] == [98, 1]
    print("✓ test_load_session_preserves_append_order_across_gateway_restarts")


def test_list_sessions() -> None:
    with tempfile.TemporaryDirectory() as td:
        cd = Path(td)
        record_call(cd, "alpha", 1, _sample_request("x"))
        record_call(cd, "beta", 1, _sample_request("y"))
        record_call(cd, "beta", 2, _sample_request("z"))
        infos = {i.session_id: i for i in list_sessions(cd)}
        assert set(infos) == {"alpha", "beta"}
        assert infos["beta"].n_calls == 2
        assert infos["alpha"].n_calls == 1
    print("✓ test_list_sessions")


def test_load_by_inner_id_when_filename_differs() -> None:
    """session_id with special characters → filename is sanitized; should still load by the real id."""
    with tempfile.TemporaryDirectory() as td:
        cd = Path(td)
        weird = "client/key:abc 123"
        record_call(cd, weird, 1, _sample_request("hi"))
        # the filename is sanitized, but load_session can find it back by the internal session_id
        turns = load_session(cd, weird)
        assert len(turns) == 1
        assert turns[0]["session_id"] == weird
    print("✓ test_load_by_inner_id_when_filename_differs")


def test_load_missing_session_raises() -> None:
    with tempfile.TemporaryDirectory() as td:
        try:
            load_session(Path(td), "does-not-exist")
        except FileNotFoundError:
            print("✓ test_load_missing_session_raises")
            return
        raise AssertionError("should raise FileNotFoundError")


def test_empty_corpus_lists_nothing() -> None:
    with tempfile.TemporaryDirectory() as td:
        assert list_sessions(Path(td)) == []
    assert list_sessions(Path("/tmp/telos-corpus-does-not-exist-xyz")) == []
    print("✓ test_empty_corpus_lists_nothing")


def _harness_id(inner: str) -> str:
    """A harness-style session id: a JSON blob with a long device/account prefix."""
    return ('{"device_id":"7efd78cb9133b717bf13b90aa9293a58d1bfed1c531f6436'
            'df592fea146d6426","account_uuid":"e2346e67-8b7b-4efa-80a9-'
            f'ca1fcb8d60e4","session_id":"{inner}"}}')


def test_long_session_ids_do_not_collide() -> None:
    """Regression: two conversations sharing a device/account prefix but with
    different inner session_ids must land in separate corpus files."""
    with tempfile.TemporaryDirectory() as td:
        cd = Path(td)
        a = _harness_id("0ccc5ec7-ac71-44ee-ad40-0ae9555a26b5")
        b = _harness_id("11111111-2222-3333-4444-555555555555")
        record_call(cd, a, 1, _sample_request("conv-a"))
        record_call(cd, b, 1, _sample_request("conv-b"))
        record_call(cd, b, 2, _sample_request("conv-b2"))
        # the bug collapsed both into one file → list_sessions saw 1 session
        assert len(list_sessions(cd)) == 2
        assert len(load_session(cd, a)) == 1
        assert len(load_session(cd, b)) == 2
        # each session is still loadable by its full id
        assert load_session(cd, a)[0]["session_id"] == a
    print("✓ test_long_session_ids_do_not_collide")


def test_load_by_handle_stem() -> None:
    """A session is loadable by the short filename stem shown in `replay --list`."""
    with tempfile.TemporaryDirectory() as td:
        cd = Path(td)
        sid = _harness_id("abcdef01-2345-6789-abcd-ef0123456789")
        record_call(cd, sid, 1, _sample_request("hi"))
        handle = list_sessions(cd)[0].handle
        assert load_session(cd, handle)[0]["session_id"] == sid
    print("✓ test_load_by_handle_stem")


def test_load_by_inner_display_id() -> None:
    """`replay --list` shows the inner session id; `--session <that>` must resolve."""
    with tempfile.TemporaryDirectory() as td:
        cd = Path(td)
        inner = "70939675-ebcc-4b34-b835-9badef95aee1"
        sid = _harness_id(inner)
        record_call(cd, sid, 1, _sample_request("a"))
        record_call(cd, sid, 2, _sample_request("b"))
        # the pretty id from the --list "session" column resolves
        turns = load_session(cd, inner)
        assert len(turns) == 2
        assert turns[0]["session_id"] == sid
    print("✓ test_load_by_inner_display_id")


def main() -> None:
    test_record_and_load_roundtrip()
    test_load_session_preserves_append_order_across_gateway_restarts()
    test_list_sessions()
    test_load_by_inner_id_when_filename_differs()
    test_load_missing_session_raises()
    test_empty_corpus_lists_nothing()
    test_long_session_ids_do_not_collide()
    test_load_by_handle_stem()
    test_load_by_inner_display_id()
    print("\nall corpus tests passed.")


if __name__ == "__main__":
    main()
