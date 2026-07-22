# -*- coding: utf-8 -*-
"""SSO cookie → Build/CPA OAuth via pure HTTP Device Flow.

Ported from grok_reg/sso2auth.py. No browser, no Playwright, no Turnstile.
Flow:
  1. Attach ``sso`` / ``sso-rw`` cookies
  2. POST /oauth2/device/code
  3. GET verification_uri_complete + POST device/verify + device/approve
  4. Poll /oauth2/token
  5. Write CLIProxyAPI auth JSON
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .xai_oauth import (
    CLIPROXYAPI_GROK_BASE_URL,
    DEFAULT_CLIENT_ID,
    TOKEN_ENDPOINT,
    USERINFO_ENDPOINT,
    parse_jwt_payload,
    save_cliproxyapi_auth_record,
)

OIDC_ISSUER = "https://auth.x.ai"
DEVICE_CODE_URL = f"{OIDC_ISSUER}/oauth2/device/code"
DEVICE_VERIFY_URL = f"{OIDC_ISSUER}/oauth2/device/verify"
DEVICE_APPROVE_URL = f"{OIDC_ISSUER}/oauth2/device/approve"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:56121/callback"

# Same scope set as grok_reg/sso2auth (device-flow proven).
DEVICE_SCOPES = (
    "openid profile email offline_access grok-cli:access "
    "api:access conversations:read conversations:write"
)

_DEVICE_CODE_LOCK = threading.Lock()
_DEVICE_CODE_NEXT_OK = 0.0
_DEVICE_CODE_MIN_INTERVAL = 0.35


def _proxy_from_env(explicit: str = "") -> str:
    from .envutil import proxy_from_env

    return proxy_from_env(explicit, include_all=True)


def _is_transient_error(exc: BaseException | str) -> bool:
    s = str(exc).lower()
    keys = (
        "timeout",
        "timed out",
        "temporarily",
        "connection reset",
        "connection refused",
        "broken pipe",
        "ssl",
        "tls",
        "429",
        "502",
        "503",
        "504",
        "slow_down",
        "max retries",
    )
    return any(k in s for k in keys)


def _urlopen(req: urllib.request.Request, *, timeout: float = 20.0, proxy: str = ""):
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)
    else:
        opener = urllib.request.build_opener()
    return opener.open(req, timeout=timeout)


def _wait_device_code_slot(min_interval: float | None = None) -> None:
    global _DEVICE_CODE_NEXT_OK
    gap = float(min_interval if min_interval is not None else _DEVICE_CODE_MIN_INTERVAL)
    with _DEVICE_CODE_LOCK:
        now = time.time()
        wait = _DEVICE_CODE_NEXT_OK - now
        if wait > 0:
            time.sleep(wait)
            now = time.time()
        _DEVICE_CODE_NEXT_OK = now + max(gap, 0.05)


def request_device_code(proxy: str = "", *, retries: int = 6) -> Optional[dict]:
    """Request device code with global throttle + 429/timeout backoff."""
    global _DEVICE_CODE_MIN_INTERVAL, _DEVICE_CODE_NEXT_OK
    data = urllib.parse.urlencode({"client_id": DEFAULT_CLIENT_ID, "scope": DEVICE_SCOPES}).encode()
    backoff = 2.0
    for attempt in range(1, max(retries, 1) + 1):
        _wait_device_code_slot()
        req = urllib.request.Request(
            DEVICE_CODE_URL,
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with _urlopen(req, timeout=30.0, proxy=proxy) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            err_name = ""
            try:
                err_name = str(json.loads(body).get("error") or "")
            except Exception:
                pass
            transient = e.code in (429, 502, 503, 504) or err_name in (
                "slow_down",
                "temporarily_unavailable",
            )
            if transient and attempt < retries:
                sleep_for = backoff
                if e.code == 429 or err_name == "slow_down":
                    sleep_for = max(backoff, 8.0) * attempt
                    with _DEVICE_CODE_LOCK:
                        _DEVICE_CODE_MIN_INTERVAL = min(_DEVICE_CODE_MIN_INTERVAL * 1.5, 2.5)
                        _DEVICE_CODE_NEXT_OK = max(_DEVICE_CODE_NEXT_OK, time.time() + sleep_for)
                time.sleep(sleep_for)
                backoff = min(backoff * 1.8, 60.0)
                continue
            return None
        except Exception as e:
            if _is_transient_error(e) and attempt < retries:
                time.sleep(backoff * attempt)
                backoff = min(backoff * 1.8, 60.0)
                continue
            return None
    return None


def poll_token(
    device_code: str,
    interval: int,
    expires_in: int,
    timeout: int = 90,
    proxy: str = "",
) -> Optional[dict]:
    deadline = time.time() + min(expires_in, timeout)
    sleep_for = max(int(interval or 5), 1)
    while time.time() < deadline:
        time.sleep(sleep_for)
        data = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": DEFAULT_CLIENT_ID,
                "device_code": device_code,
            }
        ).encode()
        req = urllib.request.Request(
            TOKEN_ENDPOINT,
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with _urlopen(req, timeout=20.0, proxy=proxy) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except Exception:
                return None
            error = err.get("error", "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                sleep_for = min(sleep_for + 5, 30)
                continue
            return None
        except Exception:
            continue
    return None


def fetch_userinfo(access_token: str, proxy: str = "") -> dict:
    try:
        from curl_cffi import requests as creq
    except ImportError:
        return {}
    try:
        r = creq.get(
            USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
            impersonate="chrome",
            timeout=15,
            proxies={"http": proxy, "https": proxy} if proxy else None,
        )
        if r.status_code == 200:
            return r.json() if r.content else {}
    except Exception:
        pass
    return {}


def extract_email_from_account_html(html: str) -> str:
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", html or "")
    for e in emails:
        el = e.lower()
        if el.endswith("@x.ai") or "support@" in el or "noreply" in el:
            continue
        return e
    return ""


def normalize_sso_token(raw: str) -> str:
    t = (raw or "").strip()
    if not t:
        return ""
    # allow "sso=..." or JSON-ish fragments
    if t.lower().startswith("sso="):
        t = t.split("=", 1)[1].strip()
    if t.startswith("{") and "sso" in t:
        try:
            d = json.loads(t)
            t = str(d.get("sso") or d.get("token") or "").strip()
        except Exception:
            pass
    # JWT has 2 dots
    if t.count(".") < 2:
        return ""
    return t


def sso_to_token(
    sso_cookie: str,
    proxy: str = "",
    *,
    http_timeout: float = 30.0,
    max_attempts: int = 3,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[dict]:
    """SSO cookie → token dict (access/refresh/id_token/email). Pure HTTP."""

    def _log(msg: str) -> None:
        if log:
            log(msg)

    sso = normalize_sso_token(sso_cookie)
    if not sso:
        _log("sso empty/invalid")
        return None

    proxy = _proxy_from_env(proxy)
    try:
        from curl_cffi import requests as creq
    except ImportError as exc:
        raise RuntimeError("curl_cffi is required for sso2auth device flow") from exc

    last_err = ""
    for attempt in range(1, max(max_attempts, 1) + 1):
        s = creq.Session()
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        s.cookies.set("sso", sso, domain=".x.ai")
        s.cookies.set("sso-rw", sso, domain=".x.ai")
        s.cookies.set("sso", sso, domain="accounts.x.ai")
        s.cookies.set("sso-rw", sso, domain="accounts.x.ai")

        email_hint = ""
        try:
            r = s.get(
                "https://accounts.x.ai/",
                impersonate="chrome",
                timeout=http_timeout,
            )
        except Exception as e:
            last_err = f"network: {e}"
            if _is_transient_error(e) and attempt < max_attempts:
                time.sleep(2.0 * attempt)
                continue
            _log(last_err)
            return None
        if "sign-in" in r.url or "sign-up" in r.url:
            _log("sso invalid (redirected to sign-in)")
            return None
        _log("sso valid")
        email_hint = extract_email_from_account_html(r.text)

        _log("device flow start")
        dc = request_device_code(proxy=proxy)
        if not dc:
            last_err = "device_code failed"
            if attempt < max_attempts:
                time.sleep(3.0 * attempt)
                continue
            _log(last_err)
            return None
        _log(f"user_code={dc.get('user_code')}")

        try:
            s.get(
                dc["verification_uri_complete"],
                impersonate="chrome",
                timeout=http_timeout,
            )
            r = s.post(
                DEVICE_VERIFY_URL,
                data={"user_code": dc["user_code"]},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                impersonate="chrome",
                timeout=http_timeout,
                allow_redirects=True,
            )
            if "consent" not in r.url:
                last_err = f"verify failed: {r.url}"
                _log(last_err)
                if attempt < max_attempts:
                    time.sleep(2.0 * attempt)
                    continue
                return None
        except Exception as e:
            last_err = f"verify exception: {e}"
            if _is_transient_error(e) and attempt < max_attempts:
                time.sleep(3.0 * attempt)
                continue
            _log(last_err)
            return None

        try:
            r = s.post(
                DEVICE_APPROVE_URL,
                data={
                    "user_code": dc["user_code"],
                    "action": "allow",
                    "principal_type": "User",
                    "principal_id": "",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                impersonate="chrome",
                timeout=http_timeout,
                allow_redirects=True,
            )
            if "done" not in r.url:
                last_err = f"approve failed: {r.url}"
                _log(last_err)
                if attempt < max_attempts:
                    time.sleep(2.0 * attempt)
                    continue
                return None
            _log("device approved")
        except Exception as e:
            last_err = f"approve exception: {e}"
            if _is_transient_error(e) and attempt < max_attempts:
                time.sleep(3.0 * attempt)
                continue
            _log(last_err)
            return None

        token = poll_token(
            dc["device_code"],
            int(dc.get("interval") or 5),
            int(dc.get("expires_in") or 1800),
            timeout=90,
            proxy=proxy,
        )
        if not token:
            last_err = "token poll failed"
            if attempt < max_attempts:
                time.sleep(4.0 * attempt)
                continue
            _log(last_err)
            return None

        access = token.get("access_token") or ""
        info = fetch_userinfo(access, proxy=proxy) if access else {}
        email = (info.get("email") or "").strip() or email_hint or ""
        if email:
            token["email"] = email
            _log(f"email={email}")
        if info.get("sub"):
            token["userinfo_sub"] = info.get("sub")
        if info:
            token["userinfo"] = info
        _log(
            f"access_token ok expires_in={token.get('expires_in')}s"
            + (" +refresh" if token.get("refresh_token") else "")
        )
        return token

    if last_err:
        _log(f"exhausted retries: {last_err}")
    return None


def mint_cpa_from_sso(
    sso: str,
    *,
    email: str = "",
    auth_dir: str | Path,
    proxy: str = "",
    base_url: str = CLIPROXYAPI_GROK_BASE_URL,
    skip_existing: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """One SSO cookie → CLIProxyAPI auth JSON. Pure HTTP Device Flow."""

    def _log(msg: str) -> None:
        if log:
            log(msg)

    sso_n = normalize_sso_token(sso)
    if not sso_n:
        return {"ok": False, "error": "sso_invalid_or_empty", "email": email or ""}

    out_dir = Path(auth_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    email_hint = (email or "").strip()

    if skip_existing and email_hint:
        # Match both `{email}.json` and `xai-{email}.json` styles.
        candidates = [
            out_dir / f"{email_hint}.json",
            out_dir / f"xai-{email_hint}.json",
        ]
        for existing in candidates:
            if existing.is_file():
                _log(f"skip existing {existing.name}")
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "existing",
                    "path": str(existing),
                    "email": email_hint,
                }

    token = sso_to_token(sso_n, proxy=proxy, log=log)
    if not token:
        return {"ok": False, "error": "mint_failed", "email": email_hint}

    raw_info = token.get("userinfo")
    info: Dict[str, Any] = dict(raw_info) if isinstance(raw_info, dict) else {}
    em = (token.get("email") or email_hint or "").strip()
    if em and not info.get("email"):
        info["email"] = em
    if token.get("userinfo_sub") and not info.get("sub"):
        info["sub"] = token.get("userinfo_sub")

    try:
        path = save_cliproxyapi_auth_record(
            token,
            userinfo=info or None,
            auth_dir=out_dir,
            redirect_uri=DEFAULT_REDIRECT_URI,
            base_url=base_url or CLIPROXYAPI_GROK_BASE_URL,
        )
    except Exception as e:
        return {"ok": False, "error": f"write_failed: {e}", "email": em or email_hint}

    # Ensure email field if userinfo missed it but we know it.
    if em:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            if not rec.get("email"):
                rec["email"] = em
                # rebuild sub from access if missing
                if not rec.get("sub"):
                    pl = parse_jwt_payload(str(token.get("access_token") or "")) or {}
                    rec["sub"] = str(pl.get("sub") or token.get("userinfo_sub") or "")
                path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    _log(f"wrote {path}")
    return {
        "ok": True,
        "path": str(path),
        "email": em or email_hint,
        "access_token": str(token.get("access_token") or ""),
        "refresh_token": str(token.get("refresh_token") or ""),
        "token": token,
        "userinfo": info,
    }


__all__ = [
    "mint_cpa_from_sso",
    "sso_to_token",
    "normalize_sso_token",
    "request_device_code",
    "poll_token",
]
