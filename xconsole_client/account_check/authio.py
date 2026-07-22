# -*- coding: utf-8 -*-
"""Auth record I/O, JWT helpers, headers, and token refresh."""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import DEFAULT_BASE_URL, DEFAULT_HEADERS


def mask_email(value: str) -> str:
    value = (value or "").strip()
    if "@" not in value:
        return value or "(unknown)"
    name, domain = value.split("@", 1)
    if len(name) <= 2:
        masked = name[:1] + "*"
    else:
        masked = name[:2] + "***" + name[-1:]
    return f"{masked}@{domain}"


def b64url_json(segment: str) -> dict[str, Any] | None:
    try:
        pad = "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(segment + pad)
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def jwt_meta(token: str, now: int | None = None) -> dict[str, Any]:
    now = int(time.time()) if now is None else now
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {"valid_jwt": False, "expired": True, "error": "not_jwt"}
    payload = b64url_json(parts[1])
    if not payload:
        return {"valid_jwt": False, "expired": True, "error": "bad_payload"}
    exp = payload.get("exp")
    iat = payload.get("iat")
    try:
        exp_i = int(exp) if exp is not None else None
    except (TypeError, ValueError):
        exp_i = None
    try:
        iat_i = int(iat) if iat is not None else None
    except (TypeError, ValueError):
        iat_i = None
    expired = bool(exp_i is not None and exp_i < now)
    return {
        "valid_jwt": True,
        "expired": expired,
        "exp": exp_i,
        "iat": iat_i,
        "ttl_sec": (exp_i - now) if exp_i is not None else None,
        "scope": payload.get("scope") or payload.get("scp"),
        "aud": payload.get("aud"),
        "iss": payload.get("iss"),
        "tier": payload.get("tier"),
        "team_id": payload.get("team_id"),
        "sub": payload.get("sub"),
    }


def resolve_auth_record(path: Path) -> dict[str, Any]:
    """Normalize cliproxyapi_auth / accounts_output into one auth record."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")

    # accounts_output bundle → prefer linked cliproxyapi auth file
    linked = str(data.get("cliproxyapi_auth") or "").strip()
    if linked:
        linked_path = Path(linked)
        if not linked_path.is_absolute():
            linked_path = (path.parent / linked_path).resolve()
        if linked_path.is_file():
            linked_data = json.loads(linked_path.read_text(encoding="utf-8"))
            if isinstance(linked_data, dict) and (
                linked_data.get("access_token") or linked_data.get("token")
            ):
                linked_data = dict(linked_data)
                linked_data.setdefault("email", data.get("email"))
                linked_data["_source"] = str(path)
                linked_data["_auth_file"] = str(linked_path)
                return linked_data

    token = (
        str(data.get("access_token") or "").strip()
        or str(data.get("oauth_access_token") or "").strip()
        or str(data.get("token") or "").strip()
    )
    base_url = (
        str(data.get("base_url") or "").strip()
        or str(data.get("build_base_url") or "").strip()
        or DEFAULT_BASE_URL
    )
    headers = data.get("headers") if isinstance(data.get("headers"), dict) else {}
    return {
        "email": data.get("email") or "",
        "access_token": token,
        "refresh_token": data.get("refresh_token") or data.get("oauth_refresh_token") or "",
        "base_url": base_url,
        "headers": headers,
        "disabled": bool(data.get("disabled")),
        "_source": str(path),
        "_auth_file": str(path),
    }


def build_headers(auth: dict[str, Any]) -> dict[str, str]:
    token = str(auth.get("access_token") or "").strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "grok-cli/0.2.93",
        **DEFAULT_HEADERS,
    }
    extra = auth.get("headers")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip():
                headers[k] = v
    return headers


def normalize_base_url(base_url: str) -> str:
    base = (base_url or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    # Build free quota is on cli-chat-proxy, not paid api.x.ai
    if "api.x.ai" in base:
        return DEFAULT_BASE_URL
    return base.rstrip("/")


def _cli_proxy_base(base_url: str = DEFAULT_BASE_URL) -> str:
    """Force cli-chat-proxy host for settings/user/billing plan probes."""
    base = normalize_base_url(base_url or DEFAULT_BASE_URL)
    if "api.x.ai" in base:
        base = DEFAULT_BASE_URL
    return base.rstrip("/")


def _proxy_from_env() -> str:
    return (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("ALL_PROXY")
        or ""
    ).strip()


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_utc_from_unix(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def persist_refreshed_tokens(auth_path: Path, token: dict[str, Any]) -> dict[str, Any]:
    """Merge refreshed OAuth tokens into an existing CPA auth JSON and save."""
    raw = json.loads(auth_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("auth JSON root must be an object")
    access = str(token.get("access_token") or "").strip()
    if not access:
        raise ValueError("refresh response missing access_token")
    raw["access_token"] = access
    new_refresh = str(token.get("refresh_token") or "").strip()
    if new_refresh:
        raw["refresh_token"] = new_refresh
    if token.get("id_token"):
        raw["id_token"] = token.get("id_token")
    if token.get("token_type"):
        raw["token_type"] = token.get("token_type")
    if token.get("expires_in") is not None:
        raw["expires_in"] = token.get("expires_in")
    expires_at = token.get("expires_at")
    if expires_at is None and token.get("expires_in") is not None:
        try:
            expires_at = int(time.time()) + int(token["expires_in"])
        except Exception:
            expires_at = None
    if expires_at is not None:
        iso = _iso_utc_from_unix(expires_at)
        if iso:
            raw["expired"] = iso
    raw["last_refresh"] = _iso_utc_now()
    auth_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return raw


def refresh_auth_record(
    auth: dict[str, Any],
    *,
    timeout: float = 30.0,
    proxy: str = "",
) -> dict[str, Any]:
    """Refresh access_token via OAuth refresh_token grant; persist if path known.

    Returns:
      {
        ok: bool,
        auth: updated auth dict (on success),
        error: str|None,
        persisted: bool,
      }
    """
    refresh_token = str(auth.get("refresh_token") or "").strip()
    if not refresh_token:
        return {
            "ok": False,
            "auth": auth,
            "error": "missing_refresh_token",
            "persisted": False,
        }

    # Lazy import keeps check_accounts importable without oauth deps in tests.
    from xconsole_client.xai_oauth import DEFAULT_CLIENT_ID, refresh_access_token

    client_id = str(auth.get("client_id") or DEFAULT_CLIENT_ID).strip() or DEFAULT_CLIENT_ID
    try:
        token = refresh_access_token(
            refresh_token,
            client_id=client_id,
            timeout=timeout,
            proxy=proxy or _proxy_from_env(),
        )
    except Exception as exc:
        return {
            "ok": False,
            "auth": auth,
            "error": f"{type(exc).__name__}: {exc}",
            "persisted": False,
        }

    auth_path_raw = str(auth.get("_auth_file") or auth.get("_source") or "").strip()
    if auth_path_raw:
        try:
            raw = persist_refreshed_tokens(Path(auth_path_raw), token)
            # Keep resolve-style shape but prefer disk content for token fields.
            updated = dict(auth)
            updated["access_token"] = str(raw.get("access_token") or "")
            updated["refresh_token"] = str(raw.get("refresh_token") or refresh_token)
            if raw.get("id_token"):
                updated["id_token"] = raw.get("id_token")
            return {
                "ok": True,
                "auth": updated,
                "error": None,
                "persisted": True,
            }
        except Exception as exc:
            # Still return in-memory tokens if disk write fails.
            err = f"persist_failed: {type(exc).__name__}: {exc}"
            updated = dict(auth)
            updated["access_token"] = str(token.get("access_token") or "")
            if token.get("refresh_token"):
                updated["refresh_token"] = token.get("refresh_token")
            return {
                "ok": True,
                "auth": updated,
                "error": err,
                "persisted": False,
            }

    updated = dict(auth)
    updated["access_token"] = str(token.get("access_token") or "")
    if token.get("refresh_token"):
        updated["refresh_token"] = token.get("refresh_token")
    return {"ok": True, "auth": updated, "error": None, "persisted": False}
