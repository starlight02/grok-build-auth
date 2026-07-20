# -*- coding: utf-8 -*-
"""Tempmail.lol email backend for the xconsole_client protocol.

Uses the Tempmail.lol REST API (api.tempmail.lol) as the default disposable
mailbox backend for signup verification codes.

API endpoint reference:
    POST /v2/inbox/create           -> { address, token }     (201)
    GET  /v2/inbox?token=<token>    -> { emails[], expired }  (200)

An Email object: { from, to, subject, body, html, date (unix ms) }.

Free tier needs no API key (https://github.com/tempmail-lol/api-python README).
TEMPMAIL_API_KEY is only for Plus/Ultra / higher rate limits.

Without a key, create() is process-wide paced (TEMPMAIL_FREE_CREATE_INTERVAL,
default 3s ≈ 20/min) so bulk -t N stays near free-tier max without 429 storms.

Usage:
    from xconsole_client.tempmail_transport import TempmailInbox
    inbox = TempmailInbox(prefix="xai")  # free tier
    address = inbox.create()
    # ... send email to address ...
    code = inbox.wait_for_code(timeout=90)
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests


BASE_URL = "https://api.tempmail.lol"

# Free-tier create pacing (no API key). Plus/Ultra skip this.
_free_create_lock = threading.Lock()
_free_create_next = 0.0


def _free_create_interval() -> float:
    raw = (os.environ.get("TEMPMAIL_FREE_CREATE_INTERVAL") or "").strip()
    if not raw:
        return 3.0
    try:
        return max(0.0, min(float(raw), 60.0))
    except ValueError:
        return 3.0


def free_create_ready_in() -> float:
    """Seconds until free-tier create slot is free (0 if ready / paid key)."""
    if (os.environ.get("TEMPMAIL_API_KEY") or "").strip():
        return 0.0
    interval = _free_create_interval()
    if interval <= 0:
        return 0.0
    with _free_create_lock:
        return max(0.0, _free_create_next - time.time())


def _pace_free_create(*, block: bool = True) -> None:
    """Serialize free-tier inbox creates to free max steady rate.

    When ``block=False`` (multi-channel mode), raises immediately if the slot
    is not free so the router can fail over to yyds/cloudflare without waiting.
    """
    global _free_create_next
    interval = _free_create_interval()
    if interval <= 0:
        return
    while True:
        with _free_create_lock:
            now = time.time()
            wait = _free_create_next - now
            if wait <= 0:
                _free_create_next = now + interval
                return
            if not block:
                from xconsole_client.mail_channels import ChannelBusy

                raise ChannelBusy(
                    "tempmail",
                    retry_after=wait,
                    reason="free-tier pacing",
                )
        time.sleep(wait)


@dataclass
class TempmailInbox:
    """A Tempmail.lol inbox with polling for x.ai verification codes."""

    api_key: str = ""
    prefix: str = ""
    base_url: str = BASE_URL
    timeout: float = 90.0
    interval: float = 3.0
    debug: bool = False
    proxy: str = ""

    # populated after create()
    address: str = ""
    token: str = ""
    _created: bool = field(default=False, init=False)

    def _proxies(self) -> Optional[dict]:
        p = (
            (self.proxy or "").strip()
            or (os.environ.get("HTTPS_PROXY") or "").strip()
            or (os.environ.get("HTTP_PROXY") or "").strip()
            or (os.environ.get("https_proxy") or "").strip()
            or (os.environ.get("http_proxy") or "").strip()
        )
        if not p:
            return None
        return {"http": p, "https": p}

    def _headers(self, *, content_type: bool = False) -> dict:
        # Match free web client headers; Authorization only when Plus/Ultra key set.
        headers = {
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            "Origin": "https://tempmail.lol",
            "Referer": "https://tempmail.lol/",
        }
        if content_type:
            headers["Content-Type"] = "application/json"
        key = (self.api_key or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def create(self, *, allow_wait: Optional[bool] = None) -> str:
        """Create a new inbox. Returns the email address.

        ``allow_wait``: free-tier pacing. Default reads MAIL_CREATE_ALLOW_WAIT
        (``0`` = multi-channel, raise ChannelBusy instead of sleeping).
        """
        if self._created:
            raise RuntimeError("Inbox already created")

        if allow_wait is None:
            raw = (os.environ.get("MAIL_CREATE_ALLOW_WAIT") or "1").strip().lower()
            allow_wait = raw not in {"0", "false", "no", "off"}

        # Free tier: pace creates process-wide so -t N ≈ free max without 429.
        if not (self.api_key or "").strip():
            _pace_free_create(block=bool(allow_wait))

        payload: dict = {}
        if self.prefix:
            payload["prefix"] = self.prefix

        last_err: Optional[Exception] = None
        for attempt in range(4):
            try:
                resp = requests.post(
                    f"{self.base_url}/v2/inbox/create",
                    headers=self._headers(content_type=True),
                    json=payload,
                    timeout=20,
                    proxies=self._proxies(),
                )
                if resp.status_code == 429:
                    last_err = RuntimeError(f"Tempmail.lol rate limited: {resp.text[:200]}")
                    # Multi-channel: fail over immediately; single-channel backoff.
                    if not allow_wait:
                        from xconsole_client.mail_channels import ChannelBusy

                        raise ChannelBusy(
                            "tempmail",
                            retry_after=8.0 + attempt * 4.0,
                            reason="http-429",
                        )
                    time.sleep(5 + attempt * 3)
                    continue
                if resp.status_code not in (200, 201):
                    raise RuntimeError(
                        f"Tempmail.lol create inbox failed: {resp.status_code} {resp.text[:300]}"
                    )

                data = resp.json() if resp.content else {}
                address = str(data.get("address") or data.get("email") or "").strip()
                token = str(data.get("token") or "").strip()
                if not address or not token:
                    raise RuntimeError(f"Tempmail.lol create missing address/token: {data}")

                self.address = address
                self.token = token
                self._created = True

                if self.debug:
                    print(f"  [Tempmail] inbox created: {self.address}")

                return self.address
            except Exception as exc:
                # Re-raise ChannelBusy for router fail-over (no local retry storm).
                from xconsole_client.mail_channels import ChannelBusy

                if isinstance(exc, ChannelBusy):
                    raise
                last_err = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
                if self.debug:
                    print(f"  [Tempmail] create attempt {attempt + 1}/4 failed: {exc}")
                if not allow_wait and attempt >= 1:
                    raise
                time.sleep(1.5 + attempt)

        raise RuntimeError(f"Tempmail.lol create failed after retries: {last_err}")

    def get_emails(self, *, budget: Optional[float] = None) -> list[dict]:
        """Fetch all emails currently in the inbox.

        ``budget`` caps total time spent in this call (including retries) so a
        slow/hung HTTP stack cannot overrun ``wait_for_code`` deadlines.
        """
        if not self._created:
            raise RuntimeError("Call create() first")

        started = time.time()
        # Default single-call budget when not driven by wait_for_code.
        hard_deadline = started + (budget if budget is not None else 20.0)
        last_err: Optional[Exception] = None
        for attempt in range(3):
            remaining = hard_deadline - time.time()
            if remaining <= 0:
                break
            # Keep each request short so we can re-check the outer deadline.
            req_timeout = max(1.0, min(10.0, remaining))
            try:
                resp = requests.get(
                    f"{self.base_url}/v2/inbox",
                    headers=self._headers(),
                    params={"token": self.token},
                    timeout=req_timeout,
                    proxies=self._proxies(),
                )
                if resp.status_code != 200:
                    return []

                data = resp.json() if resp.content else {}
                if isinstance(data, dict) and data.get("expired"):
                    raise RuntimeError("Tempmail.lol inbox expired")
                emails = data.get("emails") if isinstance(data, dict) else data
                if not isinstance(emails, list):
                    return []
                return emails
            except (requests.RequestException, ValueError) as exc:
                last_err = exc
                # Don't sleep past the budget.
                sleep_for = min(0.5 + attempt * 0.5, max(0.0, hard_deadline - time.time()))
                if sleep_for > 0:
                    time.sleep(sleep_for)
        if self.debug and last_err:
            print(f"  [Tempmail] get_emails failed: {last_err}")
        return []

    def wait_for_code(self, timeout: Optional[float] = None) -> str:
        """Poll until a 6-char x.ai code appears. Returns the code string.

        Raises TimeoutError if nothing arrives within the timeout.
        Deadline is enforced strictly — HTTP retries cannot push past it.
        """
        total = float(timeout if timeout is not None else self.timeout)
        deadline = time.time() + total
        seen_ids: set[str] = set()

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"Tempmail.lol: no x.ai code for {self.address} within "
                    f"{total:.0f}s ({len(seen_ids)} emails seen)"
                )

            # Bound each poll so a hung request cannot overrun the deadline.
            emails = self.get_emails(budget=min(8.0, remaining))
            for email in emails:
                # Use from+subject+date as a dedup key
                eid = f"{email.get('from', '')}:{email.get('subject', '')}:{email.get('date', '')}"
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)

                text = " ".join(
                    [
                        email.get("subject", "") or "",
                        email.get("body", "") or "",
                        email.get("from", "") or "",
                    ]
                )
                code = _extract_code(text)
                if code:
                    if self.debug:
                        print(f"  [Tempmail] code found: {code} (from: {email.get('from')})")
                    return code

            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"Tempmail.lol: no x.ai code for {self.address} within "
                    f"{total:.0f}s ({len(seen_ids)} emails seen)"
                )

            if self.debug:
                print(
                    f"  [Tempmail] polling... ({len(seen_ids)} emails so far, "
                    f"{remaining:.0f}s left)"
                )
            time.sleep(min(self.interval, remaining))


# --------------------------------------------------------------------------- #
# Code extractor — same logic as mailbox.py, kept standalone for this module.
# --------------------------------------------------------------------------- #
_CODE_PATTERNS = (
    # x.ai current format: "LSQ-OPU" (3 alphanum + dash + 3 alphanum = 7 chars)
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Z0-9])"),
    # x.ai legacy format: 6 uppercase alphanumeric, no dash (e.g. "XAI0X1")
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{6})(?![A-Z0-9])"),
    # keyword-anchored fallbacks
    re.compile(r"(?i)(?:code|otp|验证码|verification|verify)\s*[:：]?\s*([A-Z0-9]{3}-[A-Z0-9]{3})"),
    re.compile(r"(?i)(?:code|otp|验证码|verification|verify)\s*[:：]?\s*([A-Z0-9]{6})"),
)


def _extract_code(text: str) -> Optional[str]:
    if not text:
        return None
    for pat in _CODE_PATTERNS:
        m = pat.search(text)
        if m:
            raw = m.group(1) if m.groups() else m.group(0)
            # x.ai codes are uppercase alphanumeric (+ dash), not pure digits
            if raw.replace("-", "").isdigit():
                continue
            return raw.upper()
    return None
