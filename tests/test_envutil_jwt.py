# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import json

from xconsole_client.envutil import env_truthy, proxy_from_env
from xconsole_client.sso import parse_jwt_payload
from xconsole_client.xai_oauth import parse_jwt_payload as oauth_parse_jwt_payload


def _jwt(payload: dict) -> str:
    head = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{head}.{body}.sig"


def test_proxy_from_env_explicit_wins(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://env:1")
    assert proxy_from_env("http://explicit:2") == "http://explicit:2"


def test_proxy_from_env_https_then_http(monkeypatch) -> None:
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://http-only:9")
    assert proxy_from_env() == "http://http-only:9"


def test_env_truthy_default(monkeypatch) -> None:
    monkeypatch.delenv("TURNSTILE_DEBUG", raising=False)
    assert env_truthy("TURNSTILE_DEBUG", False) is False
    monkeypatch.setenv("TURNSTILE_DEBUG", "yes")
    assert env_truthy("TURNSTILE_DEBUG", False) is True


def test_parse_jwt_payload_shared() -> None:
    token = _jwt({"session_id": "abc", "sub": "u1"})
    a = parse_jwt_payload(token)
    b = oauth_parse_jwt_payload(token)
    assert a == b == {"session_id": "abc", "sub": "u1"}


def test_parse_jwt_payload_invalid() -> None:
    assert parse_jwt_payload("not-a-jwt") is None
