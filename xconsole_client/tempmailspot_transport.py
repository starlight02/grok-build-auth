# -*- coding: utf-8 -*-
"""TempMailSpot disposable mailbox backend (https://tempmailspot.com).

Public guide documents ``/api/v1/generate|inbox|message`` but those endpoints
are incomplete (generate returns email=undefined; inbox 500). The live site
uses:

    POST /api/mailbox/new     -> { success, data: { email, token, provider } }
    POST /api/mailbox/fetch   -> { success, emails[], expired }
         body: { email, sessionToken, existingMailIds? }

No API key. Documented generate limit ≈10/min (HTTP 429) — process-wide create
pacing defaults to 6s.

Optional env:
  TEMPMAILSPOT_API_BASE           default https://tempmailspot.com
  TEMPMAILSPOT_CREATE_INTERVAL    create pacing seconds (default 6 ≈ 10/min)

Usage:
    from xconsole_client.tempmailspot_transport import TempmailspotInbox
    inbox = TempmailspotInbox()
    address = inbox.create()
    code = inbox.wait_for_code(timeout=90)
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from xconsole_client.codes import extract_xai_code
from xconsole_client.envutil import proxy_from_env

DEFAULT_API_BASE = "https://tempmailspot.com"

_create_lock = threading.Lock()
_create_next = 0.0


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _create_interval() -> float:
    raw = _env("TEMPMAILSPOT_CREATE_INTERVAL")
    if not raw:
        return 6.0
    try:
        return max(0.0, min(float(raw), 60.0))
    except ValueError:
        return 6.0


def create_ready_in() -> float:
    """Seconds until the next create slot (0 if ready / pacing disabled)."""
    interval = _create_interval()
    if interval <= 0:
        return 0.0
    with _create_lock:
        return max(0.0, _create_next - time.time())


def _pace_create(*, block: bool = True) -> None:
    """Process-wide create pacing (documented generate ≈10/min)."""
    global _create_next
    interval = _create_interval()
    if interval <= 0:
        return
    while True:
        with _create_lock:
            now = time.time()
            wait = _create_next - now
            if wait <= 0:
                _create_next = now + interval
                return
            if not block:
                from xconsole_client.mail_channels import ChannelBusy

                raise ChannelBusy(
                    "tempmailspot",
                    retry_after=wait,
                    reason="create pacing",
                )
        time.sleep(wait)


@dataclass
class TempmailspotInbox:
    """TempMailSpot inbox: create → poll fetch → extract x.ai code."""

    base_url: str = ""
    timeout: float = 90.0
    interval: float = 5.0
    debug: bool = False
    proxy: str = ""

    address: str = ""
    token: str = ""
    provider: str = ""
    _created: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not self.base_url:
            self.base_url = _env("TEMPMAILSPOT_API_BASE", DEFAULT_API_BASE).rstrip("/")

    def _proxies(self) -> Optional[dict]:
        p = proxy_from_env(self.proxy)
        if not p:
            return None
        return {"http": p, "https": p}

    def _headers(self, *, content_type: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            "Origin": "https://tempmailspot.com",
            "Referer": "https://tempmailspot.com/",
        }
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        timeout: float = 20.0,
        allow_wait: bool = True,
    ) -> Any:
        url = f"{self.base_url}{path}"
        resp = requests.request(
            method,
            url,
            headers=self._headers(content_type=json_body is not None),
            json=json_body,
            params=params,
            timeout=timeout,
            proxies=self._proxies(),
        )
        if resp.status_code == 429:
            retry_after = 6.0
            ra = (resp.headers.get("Retry-After") or "").strip()
            if ra:
                try:
                    retry_after = max(0.2, float(ra))
                except ValueError:
                    pass
            if not allow_wait:
                from xconsole_client.mail_channels import ChannelBusy

                raise ChannelBusy(
                    "tempmailspot",
                    retry_after=retry_after,
                    reason="http-429",
                )
            raise RuntimeError(
                f"TempMailSpot rate limited (429) retry_after={retry_after:.1f}: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"TempMailSpot {method} {path} failed: {resp.status_code} {resp.text[:300]}"
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"TempMailSpot {method} {path} non-JSON: {resp.text[:200]}") from exc
        return data

    def create(self, *, allow_wait: Optional[bool] = None) -> str:
        """Create a new inbox. Returns the email address.

        ``allow_wait``: create pacing / 429. Default reads MAIL_CREATE_ALLOW_WAIT
        (``0`` = multi-channel, raise ChannelBusy instead of sleeping).
        """
        if self._created:
            raise RuntimeError("Inbox already created")

        if allow_wait is None:
            raw = (os.environ.get("MAIL_CREATE_ALLOW_WAIT") or "1").strip().lower()
            allow_wait = raw not in {"0", "false", "no", "off"}

        _pace_create(block=bool(allow_wait))

        last_err: Optional[Exception] = None
        for attempt in range(4):
            try:
                data = self._request(
                    "POST",
                    "/api/mailbox/new",
                    json_body={},
                    allow_wait=bool(allow_wait),
                )
                if not isinstance(data, dict):
                    raise RuntimeError(f"TempMailSpot create unexpected body: {data!r}")

                code = str(data.get("code") or "").strip().upper()
                if code == "GEO_BLOCKED" or data.get("geoBlocked"):
                    raise RuntimeError(f"TempMailSpot geo-blocked: {data.get('error') or data}")
                if (
                    code == "CAPTCHA_REQUIRED"
                    or data.get("challengeRequired")
                    or data.get("captchaRequired")
                ):
                    raise RuntimeError(
                        f"TempMailSpot captcha required: {data.get('error') or data}"
                    )
                if data.get("success") is False:
                    raise RuntimeError(f"TempMailSpot create failed: {data.get('error') or data}")

                inner = data.get("data") if isinstance(data.get("data"), dict) else data
                if not isinstance(inner, dict):
                    raise RuntimeError(f"TempMailSpot create missing data: {data}")

                address = str(
                    inner.get("email") or inner.get("address") or data.get("email") or ""
                ).strip()
                token = str(
                    inner.get("token")
                    or inner.get("sessionToken")
                    or data.get("token")
                    or data.get("sessionToken")
                    or ""
                ).strip()
                provider = str(inner.get("provider") or data.get("provider") or "").strip()
                if not address or not token:
                    raise RuntimeError(f"TempMailSpot create missing email/token: {data}")

                self.address = address
                self.token = token
                self.provider = provider
                self._created = True
                if self.debug:
                    print(
                        f"  [TempMailSpot] inbox created: {self.address}"
                        f" (provider={provider or '?'})"
                    )
                return self.address
            except Exception as exc:  # noqa: BLE001
                from xconsole_client.mail_channels import ChannelBusy

                if isinstance(exc, ChannelBusy):
                    raise
                last_err = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
                if self.debug:
                    print(f"  [TempMailSpot] create attempt {attempt + 1}/4 failed: {exc}")
                if not allow_wait and attempt >= 1:
                    raise
                time.sleep(1.0 + attempt * 0.8)

        raise RuntimeError(f"TempMailSpot create failed after retries: {last_err}")

    def get_emails(self, *, budget: Optional[float] = None) -> list[dict]:
        """Fetch inbox messages via POST /api/mailbox/fetch."""
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
                    "POST",
                    "/api/mailbox/fetch",
                    json_body={
                        "email": self.address,
                        "sessionToken": self.token,
                        "existingMailIds": [],
                    },
                    timeout=req_timeout,
                    allow_wait=True,
                )
                if not isinstance(data, dict):
                    return []
                if data.get("expired"):
                    raise RuntimeError("TempMailSpot inbox expired")
                if data.get("success") is False:
                    if self.debug:
                        print(f"  [TempMailSpot] fetch error: {data.get('error')}")
                    return []
                raw_emails = data.get("emails")
                if not isinstance(raw_emails, list):
                    raw_emails = data.get("messages")
                if not isinstance(raw_emails, list):
                    raw_emails = []
                return [m for m in raw_emails if isinstance(m, dict)]
            except (requests.RequestException, RuntimeError) as exc:
                if "expired" in str(exc).lower():
                    raise
                last_err = exc
                sleep_for = min(0.5 + attempt * 0.5, max(0.0, hard_deadline - time.time()))
                if sleep_for > 0:
                    time.sleep(sleep_for)
        if self.debug and last_err:
            print(f"  [TempMailSpot] get_emails failed: {last_err}")
        return []

    @staticmethod
    def _from_text(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("address") or value.get("email") or value.get("name") or "")
        return str(value or "")

    @staticmethod
    def _html_text(value: Any) -> str:
        if isinstance(value, list):
            return " ".join(str(x or "") for x in value)
        return str(value or "")

    def wait_for_code(self, timeout: Optional[float] = None) -> str:
        """Poll until an x.ai verification code appears."""
        total = float(timeout if timeout is not None else self.timeout)
        deadline = time.time() + total
        seen_ids: set[str] = set()

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"TempMailSpot: no x.ai code for {self.address} within "
                    f"{total:.0f}s ({len(seen_ids)} messages seen)"
                )

            emails = self.get_emails(budget=min(8.0, remaining))
            for msg in emails:
                mid = str(
                    msg.get("id")
                    or msg.get("mailId")
                    or msg.get("messageId")
                    or f"{self._from_text(msg.get('from'))}:{msg.get('subject', '')}:{msg.get('timestamp') or msg.get('receivedAt') or msg.get('date') or ''}"
                )
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)

                parts = [
                    str(msg.get("subject") or ""),
                    self._from_text(msg.get("from")),
                    str(msg.get("textBody") or msg.get("body") or msg.get("text") or ""),
                    self._html_text(msg.get("htmlBody") or msg.get("html") or ""),
                    str(msg.get("preview") or msg.get("intro") or ""),
                ]
                code = extract_xai_code(" ".join(parts))
                if code:
                    if self.debug:
                        print(f"  [TempMailSpot] code found: {code}")
                    return code

            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"TempMailSpot: no x.ai code for {self.address} within "
                    f"{total:.0f}s ({len(seen_ids)} messages seen)"
                )
            if self.debug:
                print(
                    f"  [TempMailSpot] polling... "
                    f"({len(seen_ids)} msgs so far, {remaining:.0f}s left)"
                )
            time.sleep(min(self.interval, remaining))
