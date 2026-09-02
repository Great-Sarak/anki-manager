from pathlib import Path

from anki_manager.config import Config


def test_lifecycle_socket_is_unset_by_default(monkeypatch):
    monkeypatch.delenv("ANKI_MANAGER_LIFECYCLE_SOCKET", raising=False)
    assert Config().lifecycle_socket is None


def test_lifecycle_socket_comes_from_environment(monkeypatch):
    monkeypatch.setenv("ANKI_MANAGER_LIFECYCLE_SOCKET", "/run/test/lifecycle.sock")
    assert Config().lifecycle_socket == Path("/run/test/lifecycle.sock")
