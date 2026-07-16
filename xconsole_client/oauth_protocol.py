# -*- coding: utf-8 -*-
"""Protocolized xAI OAuth login (no browser) for Grok Build / CLIProxyAPI.

After account signup (or with email/password), this module:

  1. Starts OAuth PKCE against auth.x.ai
  2. Lands on accounts.x.ai/sign-in?redirect=oauth2-provider&return_to=/oauth2/consent?...
  3. Solves Cloudflare Turnstile (local browser / Playwright)
  4. Calls auth_mgmt.AuthManagement/CreateSession (gRPC-web)
  5. Follows cookieSetterUrl + OAuth redirects to capture authorization code
  6. Exchanges code for tokens and exports CLIProxyAPI Grok Build auth JSON

CreateSessionRequest wire layout (reverse-engineered 2026-07):

  field 1  Credentials {
      field 1  EmailAndPassword { email=1, clearTextPassword=2 }
  }
  field 4  AntiAbuseToken {
      field 1  turnstileToken
      field 2  castleRequestToken (optional, may be empty)
  }
"""

from __future__ import annotations

import http.cookiejar
import io
import os
import re
import secrets
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

from . import grpcweb
from .solver import resolve_turnstile_solver
from .xai_oauth import (
    CLIPROXYAPI_GROK_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_SCOPES,
    OAuthLoginResult,
    _finalize_oauth_code,
    build_authorization_url,
    code_challenge_s256,
    generate_code_verifier,
)

TURNSTILE_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
CREATE_SESSION_RPC = "https://accounts.x.ai/auth_mgmt.AuthManagement/CreateSession"
CREATE_COOKIE_SETTER_RPC = (
    "https://accounts.x.ai/auth_mgmt.AuthManagement/CreateCookieSetterLink"
)
ACCOUNTS_ORIGIN = "https://accounts.x.ai"
# Observed Next.js server action for the consent Allow button (may change on deploy).
SUBMIT_OAUTH2_CONSENT_ACTION = "401b73e22a5e68737d0037e1aa449fef82cd1b35fb"



def resolve_submit_oauth2_consent_action(
    page_html: str,
    *,
    session: Any = None,
    timeout: float = 20.0,
    allow_fallback: bool = True,
) -> str:
    """Resolve submitOAuth2Consent next-action id from consent page chunks.

    The action id is NOT embedded in the HTML document. Minified binding lives
    in a ``/_next/static/chunks/*.js`` module that is only present on the real
    logged-in ``/oauth2/consent`` page (not the sign-in shell).
    """
    html = page_html or ""
    patterns = (
        re.compile(
            r'createServerReference\)\("([a-f0-9]{40,44})"[^)]{0,400}submitOAuth2Consent'
        ),
        re.compile(
            r'"([a-f0-9]{40,44})"[^"]{0,80}findSourceMapURL,"submitOAuth2Consent"'
        ),
        re.compile(
            r'findSourceMapURL,"submitOAuth2Consent"[^"]{0,20}"([a-f0-9]{40,44})"'
        ),
    )
    for pat in patterns:
        m = pat.search(html)
        if m:
            resolve_submit_oauth2_consent_action.last_source = "live"
            return m.group(1)

    paths = list(
        dict.fromkeys(re.findall(r'src="(/_next/static/chunks/[^"]+\.js)"', html))
    )
    if not paths:
        paths = list(
            dict.fromkeys(
                re.findall(r'(/_next/static/chunks/[^"\'\\s)]+\.js)', html)
            )
        )
    if not paths or session is None:
        if not allow_fallback:
            raise RuntimeError(
                "submitOAuth2Consent action id not found (no chunks/session); "
                "refusing hardcoded fallback"
            )
        resolve_submit_oauth2_consent_action.last_source = "fallback"
        return SUBMIT_OAUTH2_CONSENT_ACTION

    def _score(p: str) -> int:
        # Prefer smaller page-specific chunks over giant shared bundles.
        name = p.rsplit("/", 1)[-1]
        return (0 if "0n0" in name or "consent" in name.lower() else 1, len(name))

    for rel in sorted(paths, key=_score)[:50]:
        url = urljoin(ACCOUNTS_ORIGIN + "/", rel.lstrip("/"))
        try:
            resp = session.get(url, timeout=timeout)
            body = getattr(resp, "text", None)
            if body is None:
                content = getattr(resp, "content", b"") or b""
                body = content.decode("utf-8", "replace")
        except Exception:
            continue
        body = body or ""
        for pat in patterns:
            mm = pat.search(body)
            if mm:
                resolve_submit_oauth2_consent_action.last_source = "live"
                return mm.group(1)
        idx = body.find("submitOAuth2Consent")
        if idx >= 0:
            window = body[max(0, idx - 200) : idx + 60]
            hm = re.search(r'"([a-f0-9]{40,44})"', window)
            if hm:
                resolve_submit_oauth2_consent_action.last_source = "live"
                return hm.group(1)
    if not allow_fallback:
        raise RuntimeError(
            "submitOAuth2Consent action id not found in consent chunks; "
            "refusing hardcoded fallback"
        )
    resolve_submit_oauth2_consent_action.last_source = "fallback"
    return SUBMIT_OAUTH2_CONSENT_ACTION


resolve_submit_oauth2_consent_action.last_source = "unset"


def _enc_msg(field_no: int, raw: bytes) -> bytes:
    return grpcweb.encode_bytes(field_no, raw)


def encode_create_session_request(
    email: str,
    password: str,
    *,
    turnstile_token: str,
    castle_request_token: str = "",
) -> bytes:
    """Encode CreateSessionRequest protobuf body."""
    email_pw = grpcweb.encode_string(1, email) + grpcweb.encode_string(2, password)
    # Credentials.credentials oneof emailAndPassword = field 1
    credentials = _enc_msg(1, email_pw)
    # CreateSessionRequest.credentials = field 1
    req = _enc_msg(1, credentials)
    # CreateSessionRequest.anti_abuse_token = field 4
    anti = grpcweb.encode_string(1, turnstile_token)
    if castle_request_token:
        anti += grpcweb.encode_string(2, castle_request_token)
    else:
        anti += grpcweb.encode_string(2, "")
    req += _enc_msg(4, anti)
    return req


def _grpc_headers(referer: str) -> Dict[str, str]:
    return {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "accept": "*/*",
        "origin": ACCOUNTS_ORIGIN,
        "referer": referer,
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


def _extract_urls_from_fields(fields: List[Dict[str, Any]]) -> List[str]:
    urls: List[str] = []
    for f in fields:
        if f.get("type") == "string":
            val = str(f.get("value") or "")
            if val.startswith("http://") or val.startswith("https://"):
                urls.append(val)
        elif f.get("type") == "bytes" and f.get("hex"):
            try:
                raw = bytes.fromhex(f["hex"])
                nested = grpcweb.decode_message(raw)
                urls.extend(_extract_urls_from_fields(nested))
            except Exception:
                pass
    return urls


def _parse_grpc_error(
    headers: Dict[str, str], body: bytes
) -> Tuple[Optional[int], str]:
    # Trailers may be in body frames or HTTP headers (connect/grpc-web).
    status = headers.get("grpc-status")
    message = unquote(headers.get("grpc-message") or "")
    if status is not None:
        try:
            return int(status), message
        except ValueError:
            return None, message
    try:
        parsed = grpcweb.parse_response(body)
    except Exception:
        return None, message
    if parsed.get("grpc_status") is not None:
        return int(parsed["grpc_status"]), message or str(parsed.get("trailers") or "")
    return None, message




class _Resp:
    """Minimal response object matching curl_cffi Response fields we use."""

    __slots__ = ("status_code", "headers", "content", "text")

    def __init__(self, status_code: int, headers: Dict[str, str], content: bytes):
        self.status_code = status_code
        self.headers = headers
        self.content = content or b""
        try:
            self.text = self.content.decode("utf-8", "replace")
        except Exception:
            self.text = ""


class _UrllibCookieJar:
    """Tiny cookie jar with curl_cffi-like ``set`` / iteration."""

    def __init__(self, jar: Optional[http.cookiejar.CookieJar] = None):
        self._jar = jar if jar is not None else http.cookiejar.CookieJar()

    def set(self, name: str, value: str, domain: str = "accounts.x.ai", path: str = "/") -> None:
        ck = http.cookiejar.Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain.lstrip(".") if domain else "accounts.x.ai",
            domain_specified=bool(domain),
            domain_initial_dot=bool(domain and domain.startswith(".")),
            path=path or "/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        self._jar.set_cookie(ck)

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        for ck in self._jar:
            if ck.name == name:
                return ck.value
        return default

    def items(self):
        seen: Dict[str, str] = {}
        for ck in self._jar:
            seen[ck.name] = ck.value
        return seen.items()

    def __iter__(self):
        return iter(self._jar)


class _UrllibOAuthSession:
    """urllib-backed session for protocol OAuth when curl_cffi is CF-blocked."""

    def __init__(self, *, timeout: float = 45.0, proxy: str = ""):
        self._timeout = timeout
        self.cookies = _UrllibCookieJar()
        handlers: list[Any] = [
            urllib.request.HTTPCookieProcessor(self.cookies._jar),
        ]
        proxy = (proxy or "").strip()
        if proxy:
            handlers.insert(0, urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        self._opener = urllib.request.build_opener(*handlers)

    def get(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        allow_redirects: bool = True,
        timeout: Optional[float] = None,
    ) -> _Resp:
        return self.request(
            "GET",
            url,
            headers=headers,
            allow_redirects=allow_redirects,
            timeout=timeout,
        )

    def post(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[bytes] = None,
        timeout: Optional[float] = None,
        allow_redirects: bool = True,
    ) -> _Resp:
        return self.request(
            "POST",
            url,
            headers=headers,
            data=data,
            allow_redirects=allow_redirects,
            timeout=timeout,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[bytes] = None,
        allow_redirects: bool = True,
        timeout: Optional[float] = None,
    ) -> _Resp:
        current = url
        body = data
        meth = method.upper()
        to = self._timeout if timeout is None else float(timeout)
        status = 0
        hdrs: Dict[str, str] = {}
        raw = b""
        for _ in range(8 if allow_redirects else 1):
            req = urllib.request.Request(current, data=body, method=meth)
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            if "user-agent" not in {k.lower() for k in (headers or {})}:
                req.add_header(
                    "User-Agent",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15",
                )
            try:
                resp = self._opener.open(req, timeout=to)
            except urllib.error.HTTPError as e:
                resp = e
            status = int(resp.getcode() or 0)
            raw = resp.read() or b""
            if resp.headers.get("content-encoding", "").lower() == "gzip" and raw[:2] == b"\x1f\x8b":
                try:
                    import gzip

                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                except OSError:
                    pass
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            if allow_redirects and status in (301, 302, 303, 307, 308):
                loc = hdrs.get("location") or ""
                if not loc:
                    return _Resp(status, hdrs, raw)
                current = urljoin(current, loc)
                body = None
                meth = "GET"
                continue
            return _Resp(status, hdrs, raw)
        return _Resp(status, hdrs, raw)


def _prefer_urllib_transport() -> bool:
    """True when this IP/path should avoid curl_cffi (CF blocks it)."""
    raw = (os.environ.get("XCONSOLE_TRANSPORT") or "").strip().lower()
    if raw in {"urllib", "stdlib"}:
        return True
    if raw in {"curl_cffi", "curl", "cffi"}:
        return False
    solver = (os.environ.get("TURNSTILE_SOLVER") or "").strip().lower()
    return solver in {"safari", "webkit-system", "system-safari"}


def extract_cookies_from_auth_client(client: Any) -> Dict[str, str]:
    """Best-effort dump of name->value cookies from XConsoleAuthClient."""
    out: Dict[str, str] = {}
    try:
        jar = client._t.cookies  # type: ignore[attr-defined]
    except Exception:
        return out
    # dict-like
    try:
        if hasattr(jar, "items"):
            for k, v in jar.items():
                if k and v is not None:
                    out[str(k)] = str(v)
            if out:
                return out
    except Exception:
        pass
    # curl_cffi jar iteration
    try:
        iterable = jar.jar if hasattr(jar, "jar") else jar
        for ck in iterable:
            name = getattr(ck, "name", None)
            value = getattr(ck, "value", None)
            if name and value is not None:
                out[str(name)] = str(value)
    except Exception:
        pass
    return out


class ProtocolOAuthClient:
    """HTTP-only OAuth client (curl_cffi or urllib) + Turnstile solver."""

    def __init__(
        self,
        *,
        proxy: str = "",
        impersonate: str = "chrome131",
        debug: bool = False,
        turnstile_premium: bool = True,
    ):
        self.debug = debug
        self.turnstile_premium = turnstile_premium
        # Local browser Turnstile only.
        try:
            self.solver = resolve_turnstile_solver(
                proxy=proxy,
                debug=debug,
            )
        except Exception as exc:
            self.solver = None
            self._solver_error = str(exc)
        else:
            self._solver_error = ""
        mode = (os.environ.get("XCONSOLE_TRANSPORT") or "").strip().lower()
        if not mode:
            mode = "urllib" if _prefer_urllib_transport() else "curl_cffi"
        self._transport_name = mode if mode in {"urllib", "stdlib", "curl_cffi", "curl", "cffi"} else "curl_cffi"
        if self._transport_name in {"urllib", "stdlib"}:
            self._s = _UrllibOAuthSession(timeout=45.0, proxy=proxy or "")
            self._log("protocol OAuth transport=urllib")
        else:
            try:
                from curl_cffi import requests as creq
            except ImportError as exc:
                raise RuntimeError("curl_cffi is required for protocol OAuth") from exc
            kwargs: Dict[str, Any] = {"impersonate": impersonate}
            if proxy:
                kwargs["proxies"] = {"http": proxy, "https": proxy}
            self._s = creq.Session(**kwargs)
            self._log("protocol OAuth transport=curl_cffi")

    def load_cookies(self, cookies: Dict[str, str]) -> None:
        """Inject pre-existing session cookies (e.g. post-signup).

        ``sso`` / ``sso-rw`` are mirrored onto the domains used by the authorize
        and set-cookie hops (same as sso2auth), otherwise consent falls back to
        the signed-out sign-in shell and action-id scrape fails.
        """
        if not cookies:
            return
        jar = getattr(self._s, "cookies", None)
        sso_domains = (
            "accounts.x.ai",
            ".x.ai",
            "auth.x.ai",
            "auth.grok.com",
            ".grok.com",
        )
        for name, value in cookies.items():
            if not name or value is None:
                continue
            domains = sso_domains if name in {"sso", "sso-rw"} else ("accounts.x.ai",)
            for domain in domains:
                try:
                    if hasattr(jar, "set"):
                        try:
                            jar.set(name, value, domain=domain)
                        except TypeError:
                            jar.set(name, value)
                            break
                except Exception:
                    try:
                        if hasattr(jar, "set"):
                            jar.set(name, value)
                    except Exception:
                        pass
        self._log(f"loaded {len(cookies)} cookies into OAuth session")

    def _log(self, msg: str) -> None:
        if self.debug:
            print(f"  [oauth-protocol] {msg}")

    def _get(
        self,
        url: str,
        *,
        allow_redirects: bool = True,
        headers: Optional[Dict[str, str]] = None,
    ):
        h = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "upgrade-insecure-requests": "1",
        }
        if headers:
            h.update(headers)
        return self._s.get(url, headers=h, allow_redirects=allow_redirects, timeout=45)

    def _set_sso_cookie(self, jwt_token: str) -> None:
        """Attach session JWT as ``sso`` / ``sso-rw`` on authorize + accounts domains."""
        if not jwt_token:
            return
        jar = getattr(self._s, "cookies", None)
        if not hasattr(jar, "set"):
            return
        for domain in (
            "accounts.x.ai",
            ".x.ai",
            "auth.x.ai",
            "auth.grok.com",
            ".grok.com",
        ):
            for name in ("sso", "sso-rw"):
                try:
                    try:
                        jar.set(name, jwt_token, domain=domain)
                    except TypeError:
                        jar.set(name, jwt_token)
                        return
                except Exception:
                    pass

    def create_cookie_setter_link(
        self,
        success_url: str,
        *,
        error_url: str = f"{ACCOUNTS_ORIGIN}/sign-in",
        referer: str = f"{ACCOUNTS_ORIGIN}/sign-in",
    ) -> Dict[str, Any]:
        """Call CreateCookieSetterLink; returns cookie_setter_url for the multi-domain hop."""
        msg = grpcweb.encode_string(1, success_url) + grpcweb.encode_string(
            2, error_url
        )
        resp = self._s.post(
            CREATE_COOKIE_SETTER_RPC,
            headers=_grpc_headers(referer),
            data=grpcweb.frame_request(msg),
            timeout=45,
        )
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        header_status, header_msg = _parse_grpc_error(hdrs, resp.content)
        try:
            parsed = grpcweb.parse_response(resp.content)
        except Exception:
            parsed = {"messages": [], "trailers": {}, "grpc_status": None}
        grpc_status = parsed.get("grpc_status")
        if grpc_status is None:
            grpc_status = header_status
        grpc_msg = header_msg or unquote(
            str((parsed.get("trailers") or {}).get("grpc-message") or "")
        )
        fields = parsed["messages"][0] if parsed.get("messages") else []
        urls = _extract_urls_from_fields(fields)
        cookie_setter = next((u for u in urls if "set-cookie" in u), None) or (
            urls[0] if urls else None
        )
        ok = grpc_status in (None, 0) and bool(cookie_setter)
        return {
            "ok": ok,
            "error": None if ok else (grpc_msg or "CreateCookieSetterLink failed"),
            "grpc_status": grpc_status,
            "cookie_setter_url": cookie_setter,
            "raw_fields": fields,
        }

    def create_session(
        self, email: str, password: str, *, referer: str
    ) -> Dict[str, Any]:
        """Call CreateSession; on success stores sso JWT on the session.

        CreateSession field 2 is a session JWT (not the cookie-setter URL).
        Call :meth:`create_cookie_setter_link` next with the OAuth consent URL.
        """
        if not self.solver:
            return {
                "ok": False,
                "error": (
                    "Turnstile solver unavailable for CreateSession: "
                    + (self._solver_error or "no solver")
                ),
                "grpc_status": None,
                "session_jwt": None,
                "raw_fields": [],
            }
        self._log("solving Turnstile for sign-in...")
        turnstile = self.solver.solve_turnstile(
            website_url=referer.split("#")[0],
            website_key=TURNSTILE_SITEKEY,
            premium=self.turnstile_premium,
        )
        self._log(f"Turnstile {len(turnstile)} chars")

        body = encode_create_session_request(
            email, password, turnstile_token=turnstile, castle_request_token=""
        )
        framed = grpcweb.frame_request(body)
        resp = self._s.post(
            CREATE_SESSION_RPC,
            headers=_grpc_headers(referer),
            data=framed,
            timeout=45,
        )
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        header_status, header_msg = _parse_grpc_error(hdrs, resp.content)
        try:
            parsed = grpcweb.parse_response(resp.content)
        except Exception:
            parsed = {"messages": [], "trailers": {}, "grpc_status": None}

        grpc_status = parsed.get("grpc_status")
        if grpc_status is None:
            grpc_status = header_status
        grpc_msg = header_msg
        if not grpc_msg and parsed.get("trailers"):
            grpc_msg = unquote(str(parsed["trailers"].get("grpc-message") or ""))

        fields = parsed["messages"][0] if parsed.get("messages") else []
        session_jwt = None
        for f in fields:
            if f.get("type") == "string":
                val = str(f.get("value") or "")
                if val.startswith("eyJ") and val.count(".") >= 2:
                    session_jwt = val
                    break

        if grpc_status not in (None, 0) or not session_jwt:
            return {
                "ok": False,
                "error": grpc_msg
                or (
                    f"CreateSession failed (status={grpc_status}, fields={len(fields)})"
                ),
                "grpc_status": grpc_status,
                "session_jwt": session_jwt,
                "raw_fields": fields,
            }

        self._set_sso_cookie(session_jwt)
        self._log(f"CreateSession OK session_jwt={session_jwt[:24]}...")
        return {
            "ok": True,
            "error": None,
            "grpc_status": 0 if grpc_status is None else grpc_status,
            "session_jwt": session_jwt,
            "raw_fields": fields,
        }

    @staticmethod
    def _absolute_return_to(url: str) -> Optional[str]:
        """Extract absolute return_to target from a sign-in URL."""
        qs = parse_qs(urlparse(url).query)
        rt = (qs.get("return_to") or [""])[0]
        if not rt:
            return None
        rt = unquote(rt)
        if rt.startswith("/"):
            return ACCOUNTS_ORIGIN + rt
        if rt.startswith("http://") or rt.startswith("https://"):
            return rt
        return urljoin(ACCOUNTS_ORIGIN + "/", rt)

    def _follow_for_code(
        self,
        start_url: str,
        *,
        redirect_uri: str,
        state: str,
        max_hops: int = 25,
    ) -> str:
        """Follow redirects / cookie-setter until redirect_uri?code=... is reached."""
        current = start_url
        pending_return_to: Optional[str] = None
        visited: set[str] = set()

        for hop in range(max_hops):
            self._log(f"hop {hop}: {current[:160]}")
            # Never let the HTTP client connect to localhost callback.
            if current.startswith(redirect_uri) or (
                "code=" in current and "state=" in current and "127.0.0.1" in current
            ):
                return self._code_from_url(current, state)

            # Remember OAuth return_to while we bounce through sign-in.
            rt = self._absolute_return_to(current)
            if rt:
                pending_return_to = rt

            # If a hop dumps us on /account while OAuth return_to is known, recover.
            # Do NOT auto-jump from /sign-in (that can trigger sign-out loops).
            path = urlparse(current).path or ""
            if pending_return_to and path.rstrip("/") in ("/account", "/home"):
                key = "rt:" + pending_return_to
                if key not in visited:
                    visited.add(key)
                    self._log(f"account page → return_to {pending_return_to[:140]}")
                    current = pending_return_to
                    continue

            if current in visited and hop > 2:
                raise RuntimeError(f"OAuth redirect loop at {current[:180]}")
            visited.add(current)

            resp = self._get(current, allow_redirects=False)
            status = resp.status_code
            loc = resp.headers.get("location") or resp.headers.get("Location")

            if status in (301, 302, 303, 307, 308) and loc:
                nxt = urljoin(current, loc)
                if nxt.startswith(redirect_uri) or (
                    "code=" in nxt and ("127.0.0.1" in nxt or "localhost" in nxt)
                ):
                    return self._code_from_url(nxt, state)
                # sign-in → /account while we still have return_to: go to consent
                nxt_path = urlparse(nxt).path or ""
                if pending_return_to and nxt_path.rstrip("/") in ("/account", "/home"):
                    self._log("redirect to account intercepted; using return_to")
                    current = pending_return_to
                    continue
                current = nxt
                continue

            # HTML page: try meta-refresh / JS location / form action
            html = resp.text or ""
            m2 = re.search(
                r"https?://127\.0\.0\.1[^\"\'\s<>]*code=[^\"\'\s<>]+",
                html,
            )
            if m2:
                return self._code_from_url(m2.group(0).replace("&amp;", "&"), state)

            m = re.search(
                r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+url=([^\"\'>\s]+)',
                html,
                re.I,
            )
            if m:
                current = urljoin(current, unquote(m.group(1)))
                continue

            # Consent page: look for authorize/continue links or form actions
            for pat in (
                r'href=["\']([^"\']*oauth2[^"\']*)["\']',
                r'action=["\']([^"\']*oauth2[^"\']*)["\']',
                r'href=["\']([^"\']*callback[^"\']*)["\']',
            ):
                m = re.search(pat, html, re.I)
                if m:
                    candidate = urljoin(current, m.group(1).replace("&amp;", "&"))
                    if candidate != current and candidate not in visited:
                        current = candidate
                        break
            else:
                # If consent URL itself is the current page and already logged in,
                # try POST approve is unknown; last resort: re-hit return_to once.
                if (
                    pending_return_to
                    and current != pending_return_to
                    and pending_return_to not in visited
                ):
                    current = pending_return_to
                    continue
                raise RuntimeError(
                    f"OAuth redirect chain stalled at HTTP {status} {current[:180]} "
                    f"(no authorization code)."
                )
            continue

        raise TimeoutError("OAuth redirect chain exceeded max hops without code")

    @staticmethod
    def _code_from_url(url: str, expected_state: str) -> str:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if qs.get("error"):
            detail = (qs.get("error_description") or qs.get("error") or [""])[0]
            raise RuntimeError(f"authorization failed: {detail}")
        got_state = (qs.get("state") or [""])[0]
        if got_state and got_state != expected_state:
            raise RuntimeError("authorization failed: state mismatch")
        code = (qs.get("code") or [""])[0]
        if not code:
            raise RuntimeError(f"authorization failed: missing code in {url[:200]}")
        return code

    def login(
        self,
        email: str,
        password: str,
        *,
        client_id: str = DEFAULT_CLIENT_ID,
        scopes: Optional[List[str]] = None,
        redirect_host: str = "127.0.0.1",
        redirect_port: int = 56121,
        output_dir: Optional[str] = None,
        cliproxyapi_auth_dir: Optional[str] = None,
        cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
        cliproxyapi_disabled: bool = False,
        proxy: str = "",
        session_cookies: Optional[Dict[str, str]] = None,
        allow_create_session: bool = True,
    ) -> OAuthLoginResult:
        scopes = scopes or list(DEFAULT_SCOPES)
        if session_cookies:
            self.load_cookies(session_cookies)

        state = secrets.token_hex(16)
        nonce = secrets.token_hex(16)
        verifier = generate_code_verifier()
        challenge = code_challenge_s256(verifier)
        redirect_uri = f"http://{redirect_host}:{int(redirect_port)}/callback"

        auth_url = build_authorization_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            nonce=nonce,
            code_challenge=challenge,
            scopes=scopes,
        )
        # Consent URL is on the CreateCookieSetterLink allowlist (authorize URL is not).
        consent_url = f"{ACCOUNTS_ORIGIN}/oauth2/consent?" + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(scopes),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "nonce": nonce,
            }
        )

        def _apply_set_cookie_url(setter_url: str) -> str:
            """GET set-cookie hop and return next success_url.

            The JWT ``config.token`` is a short-lived hop secret for the
            set-cookie endpoint — it is NOT the accounts ``sso`` session JWT.
            Never write it into the ``sso`` cookie (that clobbers a valid
            session and forces the sign-in shell).
            """
            from .sso import parse_jwt_payload, _extract_jwt_from_url

            jwt = _extract_jwt_from_url(setter_url) or ""
            payload = parse_jwt_payload(jwt) if jwt else None
            cfg = (payload or {}).get("config") if isinstance(payload, dict) else None
            success = ""
            if isinstance(cfg, dict):
                success = str(cfg.get("success_url") or "")
            # Hit the set-cookie endpoint so domain cookies may be written.
            resp = self._get(setter_url, allow_redirects=False)
            # Prefer any sso minted by Set-Cookie headers (do not invent one).
            try:
                jar = getattr(self._s, "cookies", None)
                if jar is not None and hasattr(jar, "get"):
                    minted = jar.get("sso")
                    if minted and len(str(minted)) > 80:
                        self._set_sso_cookie(str(minted))
            except Exception:
                pass
            loc = resp.headers.get("location") or resp.headers.get("Location") or ""
            if loc:
                nxt = urljoin(setter_url, loc)
                self._log(f"set-cookie Location → {nxt[:160]}")
                return nxt
            if success:
                self._log(f"set-cookie no Location; using JWT success_url")
                return success
            return success or setter_url

        def _submit_oauth2_consent(page_url: str, page_html: str = "") -> str:
            """POST Next.js submitOAuth2Consent server action; return authorization code."""
            import json as _json

            # Action id lives in JS chunks, not the HTML document.
            action_id = resolve_submit_oauth2_consent_action(
                page_html or "",
                session=self._s,
                timeout=20.0,
            )
            src = getattr(
                resolve_submit_oauth2_consent_action, "last_source", "unknown"
            )
            self._log(
                f"resolved submitOAuth2Consent action={action_id} ({src})"
            )

            from urllib.parse import quote as _quote

            router_tree = (
                '["",{"children":["(app)",{"children":["(auth)",{"children":["oauth2",'
                '{"children":["consent",{"children":["__PAGE__",{}]}]}]}]}]},'
                '"$undefined","$undefined",16]'
            )
            principal_id = ""
            if page_html:
                m_uid = re.search(r'"userId"\s*:\s*"([^"]+)"', page_html)
                if m_uid:
                    principal_id = m_uid.group(1)
            payload = [
                {
                    "action": "allow",
                    "clientId": client_id,
                    "redirectUri": redirect_uri,
                    "scope": " ".join(scopes),
                    "state": state,
                    "codeChallenge": challenge,
                    "codeChallengeMethod": "S256",
                    "nonce": nonce,
                    "principalType": "User",
                    "principalId": principal_id,
                    "referrer": "",
                }
            ]
            body = _json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers = {
                "accept": "text/x-component",
                "content-type": "text/plain;charset=UTF-8",
                "next-action": action_id,
                "next-router-state-tree": _quote(router_tree, safe=""),
                "origin": ACCOUNTS_ORIGIN,
                "referer": page_url,
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
            }
            self._log(f"submitOAuth2Consent action={action_id[:16]}...")
            resp = self._s.post(
                page_url.split("?")[0] if "consent" in page_url else page_url,
                headers=headers,
                data=body,
                timeout=45,
            )
            # Some deployments post to the consent path with query string:
            if resp.status_code >= 400 or (
                resp.text
                and "error" in resp.text[:200].lower()
                and "code" not in resp.text
            ):
                resp = self._s.post(page_url, headers=headers, data=body, timeout=45)
            text = resp.text or ""
            self._log(f"consent action HTTP {resp.status_code} body={text[:180]!r}")

            def _code_from_body(body: str) -> str:
                m = re.search(r'"code"\s*:\s*"([^"]+)"', body)
                if m:
                    return m.group(1)
                m = re.search(r"code=([A-Za-z0-9._~\-]+)", body)
                if m and "error" not in m.group(0):
                    return m.group(1)
                return ""

            code = _code_from_body(text)
            if code:
                return code
            loc = resp.headers.get("location") or resp.headers.get("Location") or ""
            if "code=" in loc:
                return self._code_from_url(urljoin(page_url, loc), state)

            # Full HTML means next-action was ignored — re-resolve from response and retry once.
            if "<!DOCTYPE html>" in text[:200] or "<html" in text[:200].lower():
                action2 = resolve_submit_oauth2_consent_action(
                    text, session=self._s, timeout=20.0
                )
                if action2 and action2 != action_id:
                    headers["next-action"] = action2
                    self._log(f"retry consent with action={action2[:20]}...")
                    resp = self._s.post(
                        page_url.split("?")[0] if "consent" in page_url else page_url,
                        headers=headers,
                        data=body,
                        timeout=45,
                    )
                    text = resp.text or ""
                    self._log(
                        f"consent retry HTTP {resp.status_code} body={text[:180]!r}"
                    )
                    code = _code_from_body(text)
                    if code:
                        return code
                    loc = (
                        resp.headers.get("location")
                        or resp.headers.get("Location")
                        or ""
                    )
                    if "code=" in loc:
                        return self._code_from_url(urljoin(page_url, loc), state)

            raise RuntimeError(
                f"submitOAuth2Consent failed HTTP {resp.status_code}: {text[:300]}"
            )

        def _complete_via_cookie_setter(label: str) -> str:
            """Mint set-cookie chain with consent as success_url, then Allow consent."""
            # Prime authorize so the AS has a pending OAuth request.
            self._get(auth_url, allow_redirects=False)
            csl = self.create_cookie_setter_link(
                consent_url,
                error_url=f"{ACCOUNTS_ORIGIN}/sign-in",
                referer=f"{ACCOUNTS_ORIGIN}/sign-in?redirect=oauth2-provider",
            )
            if not csl.get("ok"):
                raise RuntimeError(
                    f"{label}: CreateCookieSetterLink failed: {csl.get('error')}"
                )
            setter = str(csl.get("cookie_setter_url") or "")
            self._log(f"{label}: cookie_setter={setter[:100]}...")

            # Apply set-cookie hop(s): JWT payload carries success_url when
            # Location is missing (common on urllib / some CF edges).
            current = setter
            for _ in range(6):
                if "code=" in current and (
                    current.startswith(redirect_uri) or "127.0.0.1" in current
                ):
                    return self._code_from_url(current, state)
                if "set-cookie" in current:
                    nxt = _apply_set_cookie_url(current)
                    self._log(f"set-cookie next={(nxt or '')[:160]}")
                    if not nxt or nxt == current:
                        break
                    current = nxt
                    continue
                break

            # Consent page (HTML) → server action Allow → code
            if "consent" in current:
                page = self._get(current, allow_redirects=False)
                # If redirected with code already (auto-approve)
                loc = page.headers.get("location") or page.headers.get("Location") or ""
                if loc and "code=" in loc:
                    return self._code_from_url(urljoin(current, loc), state)
                html = page.text or ""
                # Real consent is logged-in Authorize UI. Sign-in shell also
                # contains the word "consent" in return_to — do not POST there.
                real = page.status_code == 200 and html and (
                    "Authorize —" in html
                    or '"c":["","oauth2"' in html
                    or ("Allow" in html and "Deny" in html and "Signed in" in html)
                )
                if real:
                    return _submit_oauth2_consent(current, html)
                self._log(
                    "consent URL returned sign-in shell; session cookie missing/invalid"
                )
            return self._follow_for_code(
                current, redirect_uri=redirect_uri, state=state
            )

        self._log("OAuth PKCE start...")
        try:
            if session_cookies and session_cookies.get("sso"):
                self._set_sso_cookie(session_cookies["sso"])
            code = _complete_via_cookie_setter("session-reuse")
            self._log("authorization code obtained via session cookie-setter")
        except Exception as session_err:
            if not allow_create_session:
                raise RuntimeError(
                    f"session-reuse OAuth failed (no CreateSession): {session_err}"
                ) from session_err
            self._log(f"session-reuse failed ({session_err}); password CreateSession")
            if not email or not password:
                raise RuntimeError(
                    f"OAuth needs password login; prior error: {session_err}"
                ) from session_err
            signin = f"{ACCOUNTS_ORIGIN}/sign-in?redirect=oauth2-provider"
            self._get(signin, allow_redirects=True)
            sess = self.create_session(email, password, referer=signin)
            if not sess.get("ok"):
                raise RuntimeError(
                    f"CreateSession failed: {sess.get('error')}; prior: {session_err}"
                ) from session_err
            # Prefer CreateSession jwt as sso; keep signup sso as fallback.
            jwt = sess.get("session_jwt") or (session_cookies or {}).get("sso")
            if jwt:
                self._set_sso_cookie(str(jwt))
            try:
                code = _complete_via_cookie_setter("password-login")
            except Exception as csl_err:
                self._log(
                    f"cookie-setter path failed ({csl_err}); raw authorize follow"
                )
                code = self._follow_for_code(
                    auth_url, redirect_uri=redirect_uri, state=state
                )

        self._log("exchanging authorization code...")
        return _finalize_oauth_code(
            code=code,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            client_id=client_id,
            proxy=proxy,
            output_dir=output_dir,
            cliproxyapi_auth_dir=cliproxyapi_auth_dir,
            cliproxyapi_base_url=cliproxyapi_base_url,
            cliproxyapi_disabled=cliproxyapi_disabled,
        )


def login_with_protocol(
    email: str,
    password: str,
    *,
    proxy: str = "",
    debug: bool = False,
    turnstile_premium: bool = True,
    cliproxyapi_auth_dir: Optional[str] = None,
    cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
    cliproxyapi_disabled: bool = False,
    output_dir: Optional[str] = None,
    redirect_port: int = 56121,
    session_cookies: Optional[Dict[str, str]] = None,
    auth_client: Any = None,
    allow_create_session: bool = True,
) -> OAuthLoginResult:
    """Convenience wrapper: protocol OAuth + optional CLIProxyAPI Build export.

    If *auth_client* (XConsoleAuthClient) is provided after signup, its live
    curl_cffi session is reused so accounts.x.ai cookies stay attached.

    When *allow_create_session* is False (post-signup path), only SSO session
    reuse is attempted; password CreateSession + Turnstile is skipped so the
    caller can fall back to pure HTTP Device Flow instead.
    """
    transport_mode = None
    if auth_client is not None:
        tname = str(getattr(auth_client, "transport_name", "") or "")
        if "urllib" in tname:
            transport_mode = "urllib"
        elif "curl_cffi" in tname:
            transport_mode = "curl_cffi"
    # Temporarily force ProtocolOAuthClient transport via env if inferred.
    old_env = os.environ.get("XCONSOLE_TRANSPORT")
    if transport_mode and not (old_env or "").strip():
        os.environ["XCONSOLE_TRANSPORT"] = transport_mode
    try:
        client = ProtocolOAuthClient(
            proxy=proxy,
            debug=debug,
            turnstile_premium=turnstile_premium,
        )
    finally:
        if transport_mode and not (old_env or "").strip():
            if old_env is None:
                os.environ.pop("XCONSOLE_TRANSPORT", None)
            else:
                os.environ["XCONSOLE_TRANSPORT"] = old_env
    if auth_client is not None:
        try:
            transport = auth_client._t
            session = getattr(transport, "_session", None)
            if session is not None and getattr(client, "_transport_name", "").startswith("curl"):
                client._s = session
                client._log("reusing XConsoleAuthClient curl_cffi session for OAuth")
            else:
                if not session_cookies:
                    session_cookies = extract_cookies_from_auth_client(auth_client)
                if session_cookies:
                    client.load_cookies(session_cookies)
        except Exception as exc:
            client._log(f"could not reuse auth client session: {exc}")
            if not session_cookies:
                session_cookies = extract_cookies_from_auth_client(auth_client)
    return client.login(
        email,
        password,
        cliproxyapi_auth_dir=cliproxyapi_auth_dir,
        cliproxyapi_base_url=cliproxyapi_base_url,
        cliproxyapi_disabled=cliproxyapi_disabled,
        output_dir=output_dir,
        redirect_port=redirect_port,
        proxy=proxy,
        session_cookies=session_cookies,
        allow_create_session=allow_create_session,
    )
