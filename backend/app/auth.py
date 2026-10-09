"""Forward-auth identity helpers.

LabForge does not authenticate users itself; it runs behind a forward-auth
proxy (for example oauth2-proxy) that injects a user header. These helpers read
that header for *display only* - it is never an authorization boundary.
"""
from urllib.parse import quote

from fastapi import Request

from app.config import settings


def current_user(request: Request) -> str:
    """Best-effort display name for the signed-in user.

    Prefers the configured forward-auth header (``AUTH_USER_HEADER``), then
    ``X-Forwarded-User``. Returns ``"local"`` when no header is present, which
    is the normal case when LabForge runs without a reverse proxy.
    """
    for name in (settings.auth_user_header, "X-Forwarded-User"):
        if not name:
            continue
        value = request.headers.get(name)
        if value and value.strip():
            return value.strip()[:64]
    return "local"


def signed_in(request: Request) -> bool:
    """True when a forward-auth identity is present on the request."""
    return current_user(request) != "local"


def logout_url(next_url: str = "/") -> str:
    """oauth2-proxy sign-out URL, returning to ``next_url`` after it clears the
    session cookie. Only rendered when a user header is present, so it is
    harmless when no proxy is in front.
    """
    return "/oauth2/sign_out?rd=" + quote(next_url or "/", safe="/")
