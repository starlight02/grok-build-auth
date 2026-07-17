#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone Grok Build account checker.

Checks whether local auth JSON files can actually call the Build/CLI free
endpoint. Does NOT print tokens / passwords / SSO.

Accepts either:
  - CLIProxyAPI auth files (access_token, base_url, headers, email, disabled)
  - accounts_output bundles (oauth_access_token / cliproxyapi_auth path)

Examples:

  python check_accounts.py cliproxyapi_auth/
  python check_accounts.py accounts_output/account_*.json
  python check_accounts.py cliproxyapi_auth/user@example.com.json --json
  HTTPS_PROXY=http://127.0.0.1:7890 python check_accounts.py cliproxyapi_auth/
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
DEFAULT_HEADERS = {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": "0.2.93",
    "x-grok-client-identifier": "grok-shell",
}
PROBE_MODEL = "grok-4.5"
PROBE_BODY = {
    "model": PROBE_MODEL,
    "input": "Reply exactly: OK",
    "max_output_tokens": 8,
}


# Permanent account rejection (matches grokcli2api-go permanentChatDenialReason).
# Distinct from 429 free-usage / model quota exhaustion, and from transient 403.
CHAT_ENDPOINT_DENIED = "chat_endpoint_denied"
_CHAT_ENDPOINT_DENIED_MSG = "access to the chat endpoint is denied"
_GENERIC_ACCESS_DENIED = "access denied"


def _error_text_parts(error: Any) -> list[str]:
    """Flatten nested API error payloads into plain strings for matching."""
    if error is None:
        return []
    if isinstance(error, dict):
        parts: list[str] = []
        for key in ("message", "code", "type", "error"):
            if error.get(key) is not None:
                parts.extend(_error_text_parts(error.get(key)))
        return parts or [str(error)]
    return [str(error)]


def format_probe_error(error: Any, body_text: str = "") -> str:
    """Human-readable error snippet for tables / logs (no secrets)."""
    if isinstance(error, dict):
        msg = error.get("message") or error.get("code") or error
        return str(msg)[:300]
    if error is not None:
        return str(error)[:300]
    return (body_text or "")[:300]


def is_chat_endpoint_denied(
    status: int | None,
    *,
    body: str = "",
    error: Any = None,
    code: str | None = None,
) -> bool:
    """True when upstream permanently denies chat (HTTP 403 + denied text).

    Aligned with grokcli2api-go ``isPermanentAccountDenial``:
    - status must be 403
    - body/error contains "access to the chat endpoint is denied", or
    - a candidate string normalizes exactly to "access denied"
    """
    if status != 403:
        return False
    candidates: list[str] = []
    if code:
        candidates.append(str(code))
    candidates.extend(_error_text_parts(error))
    if body:
        candidates.append(body)
    joined = " ".join(candidates).lower()
    if _CHAT_ENDPOINT_DENIED_MSG in joined:
        return True
    for raw in candidates:
        normalized = str(raw).strip().lower().strip(" .!\t\r\n")
        if normalized == _GENERIC_ACCESS_DENIED:
            return True
    return False



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
    }


def header_int(headers: Any, name: str) -> int | None:
    raw = headers.get(name) if headers is not None else None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_exhausted(body: str) -> tuple[int | None, int | None]:
    m = re.search(
        r"tokens\s*\(actual/limit\)\s*:\s*(\d+)\s*/\s*(\d+)", body or "", re.I
    )
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def summarize_response(resp: requests.Response) -> dict[str, Any]:
    out: dict[str, Any] = {"status": resp.status_code}
    for key, header in (
        ("limit_tokens", "x-ratelimit-limit-tokens"),
        ("remaining_tokens", "x-ratelimit-remaining-tokens"),
        ("limit_requests", "x-ratelimit-limit-requests"),
        ("remaining_requests", "x-ratelimit-remaining-requests"),
    ):
        val = header_int(resp.headers, header)
        if val is not None:
            out[key] = val

    body_text = resp.text or ""
    if resp.status_code == 429:
        actual, limit = parse_exhausted(body_text)
        if actual is not None and limit is not None:
            out.update(
                {
                    "code": "subscription:free-usage-exhausted",
                    "actual_tokens": actual,
                    "limit_tokens": limit,
                    "remaining_tokens": max(0, limit - actual),
                    "reset": "rolling 24h window",
                }
            )
        else:
            out["error"] = body_text[:300]
        return out

    data: Any = None
    try:
        data = resp.json()
    except Exception:
        if body_text:
            out["error"] = body_text[:300]
        if is_chat_endpoint_denied(resp.status_code, body=body_text, error=out.get("error")):
            out["code"] = CHAT_ENDPOINT_DENIED
        return out

    err_obj: Any = None
    top_code: str | None = None
    if isinstance(data, dict):
        if model := data.get("model"):
            out["model"] = model
        usage = data.get("usage")
        if isinstance(usage, dict):
            total = usage.get("total_tokens") or usage.get("totalTokens")
            if total is not None:
                out["probe_total_tokens"] = total
        err_obj = data.get("error")
        if err_obj is not None and resp.status_code >= 400:
            out["error"] = format_probe_error(err_obj, body_text)
        raw_code = data.get("code")
        if isinstance(raw_code, str) and raw_code.strip():
            top_code = raw_code.strip()

    if is_chat_endpoint_denied(
        resp.status_code,
        body=body_text,
        error=err_obj if err_obj is not None else out.get("error"),
        code=top_code,
    ):
        out["code"] = CHAT_ENDPOINT_DENIED
    return out



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
        "refresh_token": data.get("refresh_token")
        or data.get("oauth_refresh_token")
        or "",
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
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
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
    auth_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
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
    persisted = False
    if auth_path_raw:
        try:
            raw = persist_refreshed_tokens(Path(auth_path_raw), token)
            persisted = True
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


def _apply_probe_summary(out: dict[str, Any], summary: dict[str, Any]) -> None:
    out["probe"] = summary
    out["status"] = summary.get("status")
    for k in (
        "remaining_tokens",
        "limit_tokens",
        "remaining_requests",
        "limit_requests",
        "model",
        "code",
        "actual_tokens",
        "reset",
        "probe_total_tokens",
    ):
        if k in summary:
            out[k] = summary[k]
        elif k in out and k in {
            "remaining_tokens",
            "limit_tokens",
            "remaining_requests",
            "limit_requests",
            "model",
            "code",
            "actual_tokens",
            "reset",
            "probe_total_tokens",
        }:
            # Clear stale fields from a previous probe attempt.
            out.pop(k, None)
    if summary.get("error"):
        out["probe_error"] = summary["error"]
    else:
        out.pop("probe_error", None)


def check_one(
    path: Path,
    *,
    timeout: float = 45.0,
    check_models: bool = True,
    session: requests.Session | None = None,
    refresh: bool = True,
    proxy: str = "",
) -> dict[str, Any]:
    now = int(time.time())
    out: dict[str, Any] = {
        "file": path.name,
        "path": str(path),
        "usable": False,
        "reasons": [],
        "refreshed": False,
        "refresh_persisted": False,
    }
    try:
        auth = resolve_auth_record(path)
    except Exception as exc:
        out["error"] = f"load_failed: {type(exc).__name__}: {exc}"
        out["reasons"] = ["load_failed"]
        return out

    email = str(auth.get("email") or "")
    token = str(auth.get("access_token") or "").strip()
    refresh_token = str(auth.get("refresh_token") or "").strip()
    out["email"] = email
    out["email_masked"] = mask_email(email)
    out["disabled"] = bool(auth.get("disabled"))
    out["auth_file"] = auth.get("_auth_file") or str(path)
    out["has_refresh_token"] = bool(refresh_token)

    if out["disabled"]:
        out["reasons"].append("disabled")

    meta = jwt_meta(token, now=now) if token else {
        "valid_jwt": False,
        "expired": True,
        "ttl_sec": None,
        "scope": None,
        "iss": None,
    }
    out["jwt"] = {
        "valid": meta.get("valid_jwt"),
        "expired": meta.get("expired"),
        "ttl_sec": meta.get("ttl_sec"),
        "scope": meta.get("scope"),
        "iss": meta.get("iss"),
    }

    did_refresh = False
    proxy = proxy or _proxy_from_env()

    def _maybe_refresh(reason: str) -> bool:
        nonlocal auth, token, refresh_token, meta, did_refresh, now
        if not refresh or did_refresh:
            return False
        if not refresh_token:
            out["reasons"].append("missing_refresh_token")
            return False
        result = refresh_auth_record(auth, timeout=min(timeout, 30.0), proxy=proxy)
        if not result.get("ok"):
            out["refresh_error"] = result.get("error") or "refresh_failed"
            out["reasons"].append(f"refresh_failed:{reason}")
            return False
        did_refresh = True
        out["refreshed"] = True
        out["refresh_persisted"] = bool(result.get("persisted"))
        # Drop prior failed-refresh noise once a later attempt succeeds.
        out["reasons"] = [
            r
            for r in out["reasons"]
            if not str(r).startswith("refresh_failed:")
            and r
            not in {
                "access_token_expired",
                "missing_refresh_token",
                "missing_access_token",
            }
        ]
        out["reasons"].append("access_token_refreshed")
        if result.get("error"):
            out["refresh_error"] = result["error"]
        else:
            out.pop("refresh_error", None)
        auth = result["auth"]
        token = str(auth.get("access_token") or "").strip()
        refresh_token = str(auth.get("refresh_token") or "").strip()
        now = int(time.time())
        meta = jwt_meta(token, now=now)
        out["jwt"] = {
            "valid": meta.get("valid_jwt"),
            "expired": meta.get("expired"),
            "ttl_sec": meta.get("ttl_sec"),
            "scope": meta.get("scope"),
            "iss": meta.get("iss"),
        }
        return True


    # Proactive refresh when access token is missing/expired.
    if not token:
        if not _maybe_refresh("missing_access_token"):
            out["reasons"].append("missing_access_token")
            out["error"] = "missing access_token / oauth_access_token"
            return out
    elif meta.get("expired"):
        if not _maybe_refresh("jwt_expired"):
            out["reasons"].append("access_token_expired")

    if not token:
        out["reasons"].append("missing_access_token")
        out["error"] = "missing access_token / oauth_access_token"
        return out

    base_url = normalize_base_url(str(auth.get("base_url") or DEFAULT_BASE_URL))
    headers = build_headers(auth)
    sess = session or requests.Session()

    # 1) responses probe (authoritative for Build free quota)
    responses_url = urljoin(base_url.rstrip("/") + "/", "responses")
    out["responses_url"] = responses_url
    try:
        resp = sess.post(
            responses_url, headers=headers, json=PROBE_BODY, timeout=timeout
        )
        summary = summarize_response(resp)
        _apply_probe_summary(out, summary)
    except Exception as exc:
        out["status"] = None
        out["probe_error"] = f"{type(exc).__name__}: {exc}"
        out["reasons"].append("probe_network_error")

    # Reactive refresh once on 401 (token rejected even if JWT looked fresh).
    if out.get("status") == 401 and refresh and not did_refresh and refresh_token:
        if _maybe_refresh("probe_401"):
            headers = build_headers(auth)
            try:
                resp = sess.post(
                    responses_url, headers=headers, json=PROBE_BODY, timeout=timeout
                )
                summary = summarize_response(resp)
                _apply_probe_summary(out, summary)
                # Drop prior network/auth noise after successful re-probe path.
                out["reasons"] = [
                    r
                    for r in out["reasons"]
                    if r
                    not in {
                        "probe_network_error",
                        "access_token_expired",
                        "auth_rejected_401",
                    }
                    and not str(r).startswith("refresh_failed:")
                ]

            except Exception as exc:
                out["status"] = None
                out["probe_error"] = f"{type(exc).__name__}: {exc}"
                out["reasons"].append("probe_network_error")

    # 2) optional /models smoke (uses final token)
    if check_models:
        models_url = urljoin(base_url.rstrip("/") + "/", "models")
        try:
            mresp = sess.get(models_url, headers=headers, timeout=timeout)
            models_info: dict[str, Any] = {"status": mresp.status_code}
            if mresp.ok:
                try:
                    data = mresp.json()
                    ids: list[str] = []
                    if isinstance(data, dict) and isinstance(data.get("data"), list):
                        for item in data["data"]:
                            if isinstance(item, dict) and item.get("id"):
                                ids.append(str(item["id"]))
                    models_info["model_count"] = len(ids)
                    models_info["sample"] = ids[:8]
                except Exception:
                    models_info["body_prefix"] = (mresp.text or "")[:120]
            else:
                models_info["body_prefix"] = (mresp.text or "")[:120]
            out["models"] = models_info
        except Exception as exc:
            out["models"] = {"error": f"{type(exc).__name__}: {exc}"}

    # verdict — use final JWT meta (post-refresh if any)
    status = out.get("status")
    if out["disabled"]:
        out["usable"] = False
    elif meta.get("expired"):
        out["usable"] = False
        if "access_token_expired" not in out["reasons"]:
            out["reasons"].append("access_token_expired")
    elif status == 200:
        out["usable"] = True
        out["reasons"].append("responses_ok")
        if out.get("remaining_tokens") is not None:
            out["reasons"].append(f"remaining_tokens={out['remaining_tokens']}")
    elif status == 429:
        out["usable"] = False
        out["reasons"].append("quota_exhausted_or_rate_limited")
        if out.get("code"):
            out["reasons"].append(str(out["code"]))
    elif status in (401, 403):
        out["usable"] = False
        if (
            out.get("code") == CHAT_ENDPOINT_DENIED
            or is_chat_endpoint_denied(
                status,
                body=str((out.get("probe") or {}).get("error") or out.get("probe_error") or ""),
                error=(out.get("probe") or {}).get("error") or out.get("probe_error"),
                code=str(out.get("code") or "") or None,
            )
        ):
            out["chat_endpoint_denied"] = True
            out["code"] = CHAT_ENDPOINT_DENIED
            out["reasons"].append(CHAT_ENDPOINT_DENIED)
        else:
            out["reasons"].append(f"auth_rejected_{status}")
    elif status is None:
        out["usable"] = False
        if "probe_network_error" not in out["reasons"]:
            out["reasons"].append("probe_failed")
    else:
        out["usable"] = False
        out["reasons"].append(f"http_{status}")
        if out.get("probe_error"):
            out["reasons"].append(str(out["probe_error"])[:120])

    if not out["reasons"]:
        out["reasons"] = ["unknown"]
    return out



def collect_paths(inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for raw in inputs:
        p = Path(raw).expanduser()
        candidates: list[Path] = []
        if any(ch in raw for ch in "*?[]"):
            # glob relative to cwd
            candidates = sorted(Path().glob(raw))
        elif p.is_dir():
            # both naming styles
            candidates = sorted(
                set(p.glob("*.json"))
                | set(p.glob("xai-*.json"))
                | set(p.glob("xai*.json"))
                | set(p.glob("account_*.json"))
            )
        elif p.is_file():
            candidates = [p]
        else:
            print(f"warn: not found: {raw}", file=sys.stderr)
            continue
        for c in candidates:
            if not c.is_file() or c.suffix.lower() != ".json":
                continue
            if c.name == ".DS_Store":
                continue
            key = str(c.resolve())
            if key in seen:
                continue
            # skip obvious non-auth dumps
            try:
                head = c.read_text(encoding="utf-8", errors="ignore")[:200].lower()
            except Exception:
                continue
            if "access_token" not in head and "oauth_access_token" not in head and "cliproxyapi_auth" not in head:
                # still allow if full file has token (small files)
                try:
                    full = c.read_text(encoding="utf-8", errors="ignore").lower()
                except Exception:
                    continue
                if (
                    "access_token" not in full
                    and "oauth_access_token" not in full
                    and "cliproxyapi_auth" not in full
                ):
                    continue
            seen.add(key)
            paths.append(c)
    return paths


def fmt_ttl(sec: int | None) -> str:
    if sec is None:
        return "--"
    if sec < 0:
        return "expired"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def print_table(results: list[dict[str, Any]]) -> None:
    if not results:
        print("No auth JSON files found.")
        return

    usable_n = sum(1 for r in results if r.get("usable"))
    refreshed_n = sum(1 for r in results if r.get("refreshed"))
    queued_n = sum(1 for r in results if r.get("refresh_queued"))
    print(
        f"checked={len(results)} usable={usable_n} unusable={len(results) - usable_n}"
        f"  refreshed={refreshed_n} queued={queued_n}"
    )
    print("-" * 88)
    for r in results:
        flag = "OK " if r.get("usable") else "NO "
        label = r.get("email_masked") or r.get("email") or r.get("file")
        status = r.get("status", "-")
        rem = r.get("remaining_tokens", "--")
        lim = r.get("limit_tokens", "--")
        req = r.get("remaining_requests", "--")
        req_lim = r.get("limit_requests", "--")
        ttl = fmt_ttl((r.get("jwt") or {}).get("ttl_sec"))
        reasons = ", ".join(r.get("reasons") or [])
        print(
            f"[{flag}] {label}  status={status}  tokens={rem}/{lim}  "
            f"req={req}/{req_lim}  jwt_ttl={ttl}"
        )
        print(f"       file={r.get('file')}  reasons={reasons}")
        if r.get("refresh_queued") and not r.get("refreshed"):
            print(
                f"       refresh_queued=yes"
                + (
                    f" reason={r.get('refresh_queue_reason')}"
                    if r.get("refresh_queue_reason")
                    else ""
                )
            )
        if r.get("refreshed"):
            persist = "persisted" if r.get("refresh_persisted") else "memory-only"
            print(f"       refreshed=yes ({persist})")
        if r.get("refresh_error"):
            print(f"       refresh_error={str(r['refresh_error'])[:160]}")
        if r.get("probe_error") and not r.get("usable"):
            print(f"       probe_error={r['probe_error'][:160]}")
        models = r.get("models") or {}
        if models.get("status") is not None:
            sample = models.get("sample") or []
            print(
                f"       models={models.get('status')} "
                f"count={models.get('model_count', '-')} "
                f"sample={sample}"
            )
        elif models.get("error"):
            print(f"       models_error={models['error'][:120]}")
    print("-" * 88)
    print(f"summary: {usable_n}/{len(results)} usable")


def _should_queue_refresh(result: dict[str, Any]) -> bool:
    """True when probe found auth expiry/401 and a refresh_token is available."""
    if result.get("disabled") or result.get("chat_endpoint_denied"):
        return False
    if not result.get("has_refresh_token"):
        return False
    if result.get("status") == 401:
        return True
    jwt = result.get("jwt") or {}
    if jwt.get("expired"):
        return True
    reasons = set(result.get("reasons") or [])
    return bool(
        reasons
        & {
            "access_token_expired",
            "auth_rejected_401",
            "missing_access_token",
        }
    )


def _queue_reason(result: dict[str, Any]) -> str:
    if result.get("status") == 401:
        return "probe_401"
    jwt = result.get("jwt") or {}
    if jwt.get("expired"):
        return "jwt_expired"
    reasons = set(result.get("reasons") or [])
    if "missing_access_token" in reasons:
        return "missing_access_token"
    if "access_token_expired" in reasons:
        return "access_token_expired"
    if "auth_rejected_401" in reasons:
        return "auth_rejected_401"
    return "unknown"


def _safe_check_one(
    path: Path,
    *,
    timeout: float,
    check_models: bool,
    refresh: bool,
) -> dict[str, Any]:
    """Thread worker: own Session; never raise out of the pool."""
    try:
        return check_one(
            path,
            timeout=timeout,
            check_models=check_models,
            session=None,  # per-thread session
            refresh=refresh,
        )
    except Exception as exc:
        return {
            "file": path.name,
            "path": str(path),
            "usable": False,
            "reasons": ["check_exception"],
            "error": f"{type(exc).__name__}: {exc}",
            "refreshed": False,
            "refresh_persisted": False,
            "has_refresh_token": False,
        }


def _run_parallel_checks(
    paths: list[Path],
    *,
    workers: int,
    timeout: float,
    check_models: bool,
    refresh: bool,
) -> list[dict[str, Any]]:
    """Check paths with a thread pool; return results in input order."""
    if not paths:
        return []
    workers = max(1, min(int(workers), len(paths), 32))
    if workers == 1:
        return [
            _safe_check_one(
                p, timeout=timeout, check_models=check_models, refresh=refresh
            )
            for p in paths
        ]

    by_path: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(
                _safe_check_one,
                p,
                timeout=timeout,
                check_models=check_models,
                refresh=refresh,
            ): p
            for p in paths
        }
        for fut in as_completed(futs):
            path = futs[fut]
            by_path[str(path)] = fut.result()
    return [by_path[str(p)] for p in paths]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check whether Grok Build auth JSON accounts are usable"
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="auth files, directories, or globs (default: ./cliproxyapi_auth)",
    )
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument(
        "-j",
        "--workers",
        type=int,
        default=4,
        help="parallel probe threads (default 4, max 32)",
    )
    parser.add_argument(
        "--no-models",
        action="store_true",
        help="Skip GET /v1/models smoke check",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Do not refresh expired/401 access tokens via refresh_token",
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Also check files marked disabled=true",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON results")
    parser.add_argument(
        "--only-usable",
        action="store_true",
        help="Only print usable accounts (table mode)",
    )
    parser.add_argument(
        "--only-unusable",
        action="store_true",
        help="Only print unusable accounts (table mode)",
    )
    args = parser.parse_args(argv)

    inputs = list(args.paths) if args.paths else ["cliproxyapi_auth"]
    paths = collect_paths(inputs)
    if not paths:
        print("No matching auth JSON files.", file=sys.stderr)
        return 2

    # Optional disabled filter before network.
    if not args.include_disabled:
        filtered: list[Path] = []
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("disabled") is True:
                    continue
            except Exception:
                pass
            filtered.append(path)
        paths = filtered

    workers = max(1, min(int(args.workers or 4), 32))
    do_refresh = not args.no_refresh
    check_models = not args.no_models

    # Phase 1: parallel probe only — never refresh mid-scan (keeps other accounts moving).
    if not args.json:
        print(
            f"phase1 probe: {len(paths)} files, workers={workers}, refresh=deferred",
            flush=True,
        )
    results = _run_parallel_checks(
        paths,
        workers=workers,
        timeout=args.timeout,
        check_models=check_models,
        refresh=False,
    )

    # Phase 2: queue 401 / expired JWT, refresh+reprobe at the end (also parallel).
    if do_refresh:
        queue_paths: list[Path] = []
        queue_idx: dict[str, int] = {}
        for i, r in enumerate(results):
            if not _should_queue_refresh(r):
                continue
            reason = _queue_reason(r)
            r["refresh_queued"] = True
            r["refresh_queue_reason"] = reason
            if "refresh_queued" not in (r.get("reasons") or []):
                r.setdefault("reasons", []).append("refresh_queued")
            p = Path(r.get("path") or r.get("auth_file") or "")
            if not p.is_file():
                continue
            key = str(p)
            if key in queue_idx:
                continue
            queue_idx[key] = i
            queue_paths.append(p)

        if queue_paths:
            if not args.json:
                print(
                    f"phase2 refresh queue: {len(queue_paths)} accounts, "
                    f"workers={min(workers, len(queue_paths))}",
                    flush=True,
                )
            refreshed = _run_parallel_checks(
                queue_paths,
                workers=workers,
                timeout=args.timeout,
                check_models=check_models,
                refresh=True,  # allow refresh now that probe pass finished
            )
            for r2 in refreshed:
                key = str(r2.get("path") or "")
                idx = queue_idx.get(key)
                if idx is None:
                    continue
                # Preserve that it came from the deferred queue.
                r2["refresh_queued"] = True
                r2["refresh_queue_reason"] = results[idx].get("refresh_queue_reason")
                if r2.get("refreshed") and "access_token_refreshed" not in (
                    r2.get("reasons") or []
                ):
                    r2.setdefault("reasons", []).insert(0, "access_token_refreshed")
                results[idx] = r2

    if args.only_usable:
        results = [r for r in results if r.get("usable")]
    elif args.only_unusable:
        results = [r for r in results if not r.get("usable")]

    if args.json:
        # never dump tokens
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print_table(results)

    # exit 0 if any usable, 1 if all unusable, 2 if none checked
    if not results:
        return 2
    return 0 if any(r.get("usable") for r in results) else 1



if __name__ == "__main__":
    raise SystemExit(main())
