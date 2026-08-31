import json

from nagi.session_store import SessionStore


def test_session_store_saves_loads_and_finds_latest_session(tmp_path):
    store = SessionStore(tmp_path / ".nagi" / "sessions")
    first = {"id": "session_001", "history": [{"role": "user", "content": "first"}]}
    second = {"id": "session_002", "history": [{"role": "user", "content": "second"}]}

    first_path = store.save(first)
    second_path = store.save(second)

    assert first_path == store.path("session_001")
    assert json.loads(first_path.read_text(encoding="utf-8"))["id"] == "session_001"
    assert store.load("session_002") == second
    assert store.latest() == second_path.stem


def test_session_store_latest_is_none_when_empty(tmp_path):
    store = SessionStore(tmp_path / ".nagi" / "sessions")

    assert store.latest() is None
