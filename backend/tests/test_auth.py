"""Tests for the forward-auth identity helpers (app.auth)."""
from types import SimpleNamespace

from app import auth


def _req(headers):
    return SimpleNamespace(headers=headers)


def test_current_user_prefers_configured_header(monkeypatch):
    monkeypatch.setattr(auth.settings, "auth_user_header", "X-Auth-Request-User")
    req = _req({"X-Auth-Request-User": "alice", "X-Forwarded-User": "bob"})
    assert auth.current_user(req) == "alice"


def test_current_user_falls_back_to_x_forwarded_user(monkeypatch):
    monkeypatch.setattr(auth.settings, "auth_user_header", None)
    assert auth.current_user(_req({"X-Forwarded-User": "bob"})) == "bob"


def test_current_user_trims_and_caps_length(monkeypatch):
    monkeypatch.setattr(auth.settings, "auth_user_header", "X-Forwarded-User")
    assert auth.current_user(_req({"X-Forwarded-User": "  alice  "})) == "alice"
    assert auth.current_user(_req({"X-Forwarded-User": "x" * 200})) == "x" * 64


def test_current_user_and_signed_in_without_header(monkeypatch):
    monkeypatch.setattr(auth.settings, "auth_user_header", None)
    assert auth.current_user(_req({})) == "local"
    assert auth.signed_in(_req({})) is False
    assert auth.signed_in(_req({"X-Forwarded-User": "alice"})) is True


def test_logout_url_defaults_and_encodes_next():
    assert auth.logout_url() == "/oauth2/sign_out?rd=/"
    assert auth.logout_url("/vms/web1") == "/oauth2/sign_out?rd=/vms/web1"
    # An empty next-url still yields a usable target.
    assert auth.logout_url("") == "/oauth2/sign_out?rd=/"
