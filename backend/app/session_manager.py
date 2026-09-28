"""Thread-safe session manager for limiting concurrent console/VNC sessions.

Manages per-VM session limits using thread-safe counters. The module-level
``session_manager`` singleton is built from ``settings.console_max_sessions``.
"""

import threading
from typing import Dict


class SessionManager:
    """Manages per-VM session limits with thread-safe counters.

    Tracks active session counts per VM name and enforces a maximum number of
    concurrent sessions.
    """

    def __init__(self, max_sessions: int) -> None:
        self.max_sessions = max_sessions
        self._sessions: Dict[str, int] = {}
        self._lock = threading.Lock()

    def acquire(self, vm_name: str) -> bool:
        """Try to acquire a session slot for the given VM.

        Returns True if a slot was acquired, False if max_sessions exceeded.
        """
        with self._lock:
            if self._sessions.get(vm_name, 0) >= self.max_sessions:
                return False
            self._sessions[vm_name] = self._sessions.get(vm_name, 0) + 1
            return True

    def release(self, vm_name: str) -> None:
        """Release a session slot for the given VM."""
        with self._lock:
            remaining = self._sessions.get(vm_name, 1) - 1
            if remaining <= 0:
                self._sessions.pop(vm_name, None)
            else:
                self._sessions[vm_name] = remaining

    def count(self, vm_name: str) -> int:
        """Return the number of active sessions for a VM."""
        with self._lock:
            return self._sessions.get(vm_name, 0)

    def clear(self) -> None:
        """Clear all session counts (useful for testing or reset)."""
        with self._lock:
            self._sessions.clear()


def _make_session_manager() -> SessionManager:
    """Build the singleton from settings, importing lazily to avoid cycles."""
    from app.config import settings

    return SessionManager(max_sessions=settings.console_max_sessions)


# Module-level singleton used by the console and VNC handlers.
session_manager = _make_session_manager()
