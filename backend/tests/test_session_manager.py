"""Unit tests for the per-VM session limiter."""
import threading

from app.session_manager import SessionManager


def test_acquire_release_and_limit():
    manager = SessionManager(max_sessions=2)
    assert manager.acquire("vm") is True
    assert manager.count("vm") == 1
    assert manager.acquire("vm") is True
    assert manager.acquire("vm") is False  # limit reached
    manager.release("vm")
    assert manager.count("vm") == 1
    manager.release("vm")
    assert manager.count("vm") == 0
    assert manager.acquire("vm") is True


def test_release_unknown_is_safe():
    manager = SessionManager(max_sessions=1)
    manager.release("ghost")  # must not raise
    assert manager.count("ghost") == 0


def test_clear_resets_all():
    manager = SessionManager(max_sessions=5)
    manager.acquire("a")
    manager.acquire("b")
    manager.clear()
    assert manager.count("a") == 0
    assert manager.count("b") == 0


def test_concurrent_acquire_never_exceeds_limit():
    manager = SessionManager(max_sessions=3)
    granted = []

    def worker():
        granted.append(manager.acquire("vm"))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert granted.count(True) == 3
    assert manager.count("vm") == 3
