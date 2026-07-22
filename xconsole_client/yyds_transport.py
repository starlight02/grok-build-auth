# -*- coding: utf-8 -*-
"""YYDS (maliapi.215.im) disposable mailbox backend.

Ported from grok_reg/grok_register_ttk.py YYDS helpers, with the same
create() / wait_for_code(timeout) surface as TempmailInbox.

Auth (either works):
  YYDS_API_KEY  → X-API-Key
  YYDS_JWT      → Authorization: Bearer …

Optional:
  YYDS_API_BASE   default https://maliapi.215.im/v1
  YYDS_DOMAINS    optional allow-list (comma-separated). Empty = ALL verified
                  domains, load-balanced. When set, balance across that full set.

Usage:
    from xconsole_client.yyds_transport import YydsInbox
    inbox = YydsInbox(api_key=..., jwt=..., preferred_domains="a.com,b.com")
    email = inbox.create()
    code = inbox.wait_for_code(timeout=90)
"""

from __future__ import annotations

import os
import re
import secrets
import string
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from xconsole_client.codes import extract_xai_code
from xconsole_client.envutil import proxy_from_env


DEFAULT_API_BASE = "https://maliapi.215.im/v1"

# Process-wide domain load balancer (least-used + round-robin ties).
_domain_lb_lock = threading.Lock()
_domain_use_count: dict[str, int] = {}
_domain_rr = 0


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _balance_pick(names: list[str]) -> str:
    """Pick least-used name; ties broken by rotating round-robin."""
    global _domain_rr
    if not names:
        raise RuntimeError("empty domain candidate list")
    if len(names) == 1:
        with _domain_lb_lock:
            n = names[0]
            _domain_use_count[n] = _domain_use_count.get(n, 0) + 1
            return n
    with _domain_lb_lock:
        min_c = min(_domain_use_count.get(n, 0) for n in names)
        pool = sorted(n for n in names if _domain_use_count.get(n, 0) == min_c)
        choice = pool[_domain_rr % len(pool)]
        _domain_rr += 1
        _domain_use_count[choice] = _domain_use_count.get(choice, 0) + 1
        return choice


@dataclass
class YydsInbox:
    """YYDS mailbox: create account → poll messages → extract x.ai code."""

    api_key: str = ""
    jwt: str = ""
    base_url: str = ""
    preferred_domains: str = ""
    prefix: str = "xai"
    timeout: float = 90.0
    interval: float = 3.0
    debug: bool = False
    proxy: str = ""

    address: str = ""
    token: str = ""
    _created: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not self.api_key:
            self.api_key = _env("YYDS_API_KEY")
        if not self.jwt:
            self.jwt = _env("YYDS_JWT")
        if not self.base_url:
            self.base_url = _env("YYDS_API_BASE", DEFAULT_API_BASE).rstrip("/")
        if not self.preferred_domains:
            self.preferred_domains = _env("YYDS_DOMAINS")

    def _proxies(self) -> Optional[dict]:
        p = proxy_from_env(self.proxy)
        if not p:
            return None
        return {"http": p, "https": p}

    def _auth_headers(self, *, content_type: bool = False, use_mailbox_token: bool = False) -> dict:
        headers: dict[str, str] = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = "application/json"
        # Mailbox-scoped token (from create/token) for /messages
        if use_mailbox_token and (self.token or "").strip():
            headers["Authorization"] = f"Bearer {self.token.strip()}"
            return headers
        jwt = (self.jwt or "").strip()
        key = (self.api_key or "").strip()
        if jwt:
            headers["Authorization"] = f"Bearer {jwt}"
        elif key:
            headers["X-API-Key"] = key
        return headers

    def _require_creds(self) -> None:
        if not (self.api_key or "").strip() and not (self.jwt or "").strip():
            raise RuntimeError(
                "YYDS credentials missing: set YYDS_API_KEY or YYDS_JWT (see .env.example)"
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        use_mailbox_token: bool = False,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self._auth_headers(
            content_type=json_body is not None, use_mailbox_token=use_mailbox_token
        )
        resp = requests.request(
            method,
            url,
            headers=headers,
            json=json_body,
            params=params,
            timeout=timeout,
            proxies=self._proxies(),
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"YYDS {method} {path} failed: {resp.status_code} {resp.text[:300]}")
        try:
            data = resp.json() if resp.content else {}
        except ValueError as exc:
            raise RuntimeError(f"YYDS {method} {path} non-JSON: {resp.text[:200]}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"YYDS {method} {path} unexpected body type")
        return data

    def list_domains(self) -> list[dict]:
        self._require_creds()
        data = self._request("GET", "/domains")
        if not data.get("success"):
            return []
        raw = data.get("data") or []
        return raw if isinstance(raw, list) else []

    def candidate_domains(self) -> list[str]:
        """Verified domain names to create on (load-balanced).

        - YYDS_DOMAINS / preferred_domains empty → **all** verified domains
        - set → only listed names that are verified (full set is balanced)
        """
        domains = self.list_domains()
        if not domains:
            raise RuntimeError("YYDS returned no domains")
        verified = [
            str(d.get("domain") or "").strip().lower()
            for d in domains
            if d.get("isVerified") and d.get("domain")
        ]
        verified = [d for d in verified if d]
        if not verified:
            raise RuntimeError("YYDS has no verified domains")

        preferred = [
            name.strip().lower()
            for name in re.split(r"[,，\s]+", self.preferred_domains or "")
            if name.strip()
        ]
        if preferred:
            allow = set(preferred)
            picked = [d for d in verified if d in allow]
            # keep allow-list order uniqueness then fill any verified matches
            if not picked:
                raise RuntimeError(
                    "YYDS_DOMAINS set but none match verified domains: " + ",".join(preferred[:8])
                )
            return picked
        return verified

    def pick_domain(self, *, exclude: Optional[set[str]] = None) -> str:
        """Load-balanced domain pick across the candidate set."""
        names = self.candidate_domains()
        if exclude:
            filtered = [n for n in names if n not in exclude]
            if filtered:
                names = filtered
        return _balance_pick(names)

    @staticmethod
    def _local_part(prefix: str = "xai", length: int = 8) -> str:
        chars = string.ascii_lowercase + string.digits
        body = "".join(secrets.choice(chars) for _ in range(length))
        p = (prefix or "xai").strip().lower()
        p = re.sub(r"[^a-z0-9]", "", p) or "xai"
        return f"{p}{body}"

    def create(self) -> str:
        """Create a new YYDS inbox. Returns the email address."""
        if self._created:
            raise RuntimeError("Inbox already created")
        self._require_creds()

        last_err: Optional[Exception] = None
        tried_domains: set[str] = set()
        # Up to 4 domain attempts so one bad domain does not stick the pool.
        for attempt in range(4):
            try:
                domain = self.pick_domain(exclude=tried_domains)
            except RuntimeError as exc:
                last_err = exc
                break
            tried_domains.add(domain)
            local = self._local_part(self.prefix)
            payload: dict[str, Any] = {"address": local, "domain": domain}
            try:
                data = self._request("POST", "/accounts", json_body=payload)
                if not data.get("success"):
                    raise RuntimeError(f"YYDS create account failed: {data}")
                result = data.get("data") or {}
                if not isinstance(result, dict):
                    result = {}
                address = str(result.get("address") or f"{local}@{domain}").strip()
                token = str(result.get("token") or "").strip()
                if not token:
                    token = self._fetch_token(address)
                if not address or not token:
                    raise RuntimeError(f"YYDS create missing address/token: {data}")
                self.address = address
                self.token = token
                self._created = True
                if self.debug:
                    print(f"  [YYDS] inbox created: {self.address} (domain={domain})")
                return self.address
            except (requests.RequestException, RuntimeError) as exc:
                last_err = exc
                if self.debug:
                    print(f"  [YYDS] create attempt {attempt + 1}/4 domain={domain} failed: {exc}")
                time.sleep(0.5 + attempt * 0.4)

        raise RuntimeError(f"YYDS create failed after retries: {last_err}")

    def _fetch_token(self, address: str) -> str:
        data = self._request("POST", "/token", json_body={"address": address})
        if not data.get("success"):
            raise RuntimeError(f"YYDS token failed: {data}")
        inner = data.get("data") or {}
        if isinstance(inner, dict):
            return str(inner.get("token") or "").strip()
        return ""

    def get_messages(self, *, budget: Optional[float] = None) -> list[dict]:
        if not self._created:
            raise RuntimeError("Call create() first")
        started = time.time()
        hard_deadline = started + (budget if budget is not None else 20.0)
        last_err: Optional[Exception] = None
        for attempt in range(3):
            remaining = hard_deadline - time.time()
            if remaining <= 0:
                break
            req_timeout = max(1.0, min(10.0, remaining))
            try:
                data = self._request(
                    "GET",
                    "/messages",
                    params={"address": self.address},
                    use_mailbox_token=True,
                    timeout=req_timeout,
                )
                if not data.get("success"):
                    return []
                inner = data.get("data") or {}
                msgs = inner.get("messages") if isinstance(inner, dict) else None
                if not isinstance(msgs, list):
                    return []
                return [m for m in msgs if isinstance(m, dict)]
            except (requests.RequestException, RuntimeError) as exc:
                last_err = exc
                sleep_for = min(0.5 + attempt * 0.5, max(0.0, hard_deadline - time.time()))
                if sleep_for > 0:
                    time.sleep(sleep_for)
        if self.debug and last_err:
            print(f"  [YYDS] get_messages failed: {last_err}")
        return []

    def get_message_detail(self, message_id: str, *, budget: float = 8.0) -> dict:
        if not message_id:
            return {}
        try:
            data = self._request(
                "GET",
                f"/messages/{message_id}",
                use_mailbox_token=True,
                timeout=max(1.0, min(10.0, budget)),
            )
            if not data.get("success"):
                return {}
            detail = data.get("data") or {}
            return detail if isinstance(detail, dict) else {}
        except (requests.RequestException, RuntimeError) as exc:
            if self.debug:
                print(f"  [YYDS] message detail failed: {exc}")
            return {}

    def wait_for_code(self, timeout: Optional[float] = None) -> str:
        """Poll until an x.ai verification code appears."""
        total = float(timeout if timeout is not None else self.timeout)
        deadline = time.time() + total
        seen_ids: set[str] = set()

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"YYDS: no x.ai code for {self.address} within "
                    f"{total:.0f}s ({len(seen_ids)} messages seen)"
                )

            messages = self.get_messages(budget=min(8.0, remaining))
            for msg in messages:
                mid = str(
                    msg.get("id")
                    or msg.get("messageId")
                    or msg.get("_id")
                    or f"{msg.get('from', '')}:{msg.get('subject', '')}:{msg.get('createdAt') or msg.get('date') or ''}"
                )
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)

                parts = [
                    str(msg.get("subject") or ""),
                    str(msg.get("from") or msg.get("fromAddress") or ""),
                    str(msg.get("text") or msg.get("body") or msg.get("preview") or ""),
                    str(msg.get("html") or ""),
                ]
                # Detail endpoint often holds full body
                if msg.get("id") or msg.get("messageId") or msg.get("_id"):
                    detail = self.get_message_detail(
                        str(msg.get("id") or msg.get("messageId") or msg.get("_id")),
                        budget=min(8.0, deadline - time.time()),
                    )
                    if detail:
                        parts.extend(
                            [
                                str(detail.get("subject") or ""),
                                str(detail.get("text") or detail.get("body") or ""),
                                str(detail.get("html") or ""),
                                str(detail.get("from") or ""),
                            ]
                        )

                code = extract_xai_code(" ".join(parts))
                if code:
                    if self.debug:
                        print(f"  [YYDS] code found: {code}")
                    return code

            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"YYDS: no x.ai code for {self.address} within "
                    f"{total:.0f}s ({len(seen_ids)} messages seen)"
                )
            if self.debug:
                print(f"  [YYDS] polling... ({len(seen_ids)} msgs so far, {remaining:.0f}s left)")
            time.sleep(min(self.interval, remaining))
