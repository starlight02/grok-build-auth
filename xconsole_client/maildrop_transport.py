# -*- coding: utf-8 -*-
"""Maildrop.cc disposable mailbox backend (https://docs.maildrop.cc).

GraphQL API (POST only, Content-Type: application/json):

    https://api.maildrop.cc/graphql

    query  { inbox(mailbox: $m) { id subject date headerfrom } }
    query  { message(mailbox: $m, id: $id) { id subject date headerfrom data html } }
    query  { altinbox(mailbox: $m) }   # optional privacy alias

No API key. Mailboxes are virtual: any local-part @ maildrop.cc works without
prior registration. Listing omits body/html; fetch each message by id.

Rate limit (docs): 50 queries / 10s window (~5 QPS). Poll inbox every few
seconds, not continuously.

Optional env:
  MAILDROP_API_URL       default https://api.maildrop.cc/graphql
  MAILDROP_DOMAIN        default maildrop.cc
  MAILDROP_USE_ALIAS     1 = signup with altinbox alias, poll original mailbox
  MAILDROP_POLL_INTERVAL poll seconds (default 5)

Usage:
    from xconsole_client.maildrop_transport import MaildropInbox
    inbox = MaildropInbox(prefix="xai")
    address = inbox.create()
    code = inbox.wait_for_code(timeout=90)
"""

from __future__ import annotations

import os
import re
import secrets
import string
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from xconsole_client.codes import extract_xai_code
from xconsole_client.envutil import env_truthy, proxy_from_env

DEFAULT_API_URL = "https://api.maildrop.cc/graphql"
DEFAULT_DOMAIN = "maildrop.cc"

_INBOX_QUERY = """
query Inbox($mailbox: String!) {
  inbox(mailbox: $mailbox) {
    id
    subject
    date
    headerfrom
  }
}
""".strip()

_MESSAGE_QUERY = """
query Message($mailbox: String!, $id: String!) {
  message(mailbox: $mailbox, id: $id) {
    id
    subject
    date
    headerfrom
    data
    html
  }
}
""".strip()

_ALTINBOX_QUERY = """
query AltInbox($mailbox: String!) {
  altinbox(mailbox: $mailbox)
}
""".strip()


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def create_ready_in() -> float:
    """Create is local (no API) — always ready."""
    return 0.0


@dataclass
class MaildropInbox:
    """Maildrop inbox: pick local-part → poll GraphQL inbox/message → extract code."""

    api_url: str = ""
    domain: str = ""
    prefix: str = "xai"
    use_alias: bool = False
    timeout: float = 90.0
    interval: float = 5.0
    debug: bool = False
    proxy: str = ""

    # Public address used for signup (may be alias@domain).
    address: str = ""
    # GraphQL mailbox key (local-part without domain).
    mailbox: str = ""
    # Optional alias local-part when use_alias.
    alias: str = ""
    _created: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not self.api_url:
            self.api_url = _env("MAILDROP_API_URL", DEFAULT_API_URL)
        if not self.domain:
            self.domain = (
                _env("MAILDROP_DOMAIN", DEFAULT_DOMAIN).lstrip("@").lower() or DEFAULT_DOMAIN
            )
        if not self.use_alias:
            self.use_alias = env_truthy("MAILDROP_USE_ALIAS", False)
        raw_interval = _env("MAILDROP_POLL_INTERVAL")
        if raw_interval:
            try:
                self.interval = max(1.0, min(float(raw_interval), 60.0))
            except ValueError:
                pass

    def _proxies(self) -> Optional[dict]:
        p = proxy_from_env(self.proxy)
        if not p:
            return None
        return {"http": p, "https": p}

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
        }

    def _gql(
        self,
        query: str,
        variables: Optional[dict] = None,
        *,
        timeout: float = 20.0,
        allow_wait: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        resp = requests.post(
            self.api_url,
            headers=self._headers(),
            json=payload,
            timeout=timeout,
            proxies=self._proxies(),
        )
        if resp.status_code == 429:
            retry_after = 10.0
            ra = (resp.headers.get("Retry-After") or "").strip()
            if ra:
                try:
                    retry_after = max(0.2, float(ra))
                except ValueError:
                    pass
            if not allow_wait:
                from xconsole_client.mail_channels import ChannelBusy

                raise ChannelBusy(
                    "maildrop",
                    retry_after=retry_after,
                    reason="http-429",
                )
            raise RuntimeError(
                f"Maildrop rate limited (429) retry_after={retry_after:.1f}: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Maildrop GraphQL failed: {resp.status_code} {resp.text[:300]}")
        try:
            data = resp.json() if resp.content else {}
        except ValueError as exc:
            raise RuntimeError(f"Maildrop non-JSON: {resp.text[:200]}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Maildrop unexpected body type: {type(data)}")
        errors = data.get("errors")
        if errors:
            msg = errors if isinstance(errors, str) else str(errors)[:300]
            raise RuntimeError(f"Maildrop GraphQL errors: {msg}")
        inner = data.get("data")
        if not isinstance(inner, dict):
            raise RuntimeError(f"Maildrop missing data: {data}")
        return inner

    @staticmethod
    def _local_part(prefix: str = "xai", length: int = 10) -> str:
        chars = string.ascii_lowercase + string.digits
        body = "".join(secrets.choice(chars) for _ in range(length))
        p = (prefix or "xai").strip().lower()
        p = re.sub(r"[^a-z0-9]", "", p) or "xai"
        return f"{p}{body}"

    def create(self, *, allow_wait: Optional[bool] = None) -> str:
        """Allocate a mailbox (local only). Returns the public email address.

        ``allow_wait`` kept for channel interface parity; create needs no API
        unless ``use_alias`` is enabled (one altinbox query).
        """
        if self._created:
            raise RuntimeError("Inbox already created")

        if allow_wait is None:
            raw = (os.environ.get("MAIL_CREATE_ALLOW_WAIT") or "1").strip().lower()
            allow_wait = raw not in {"0", "false", "no", "off"}

        mailbox = self._local_part(self.prefix)
        public = f"{mailbox}@{self.domain}".lower()
        alias = ""

        if self.use_alias:
            last_err: Optional[Exception] = None
            for attempt in range(3):
                try:
                    data = self._gql(
                        _ALTINBOX_QUERY,
                        {"mailbox": mailbox},
                        allow_wait=bool(allow_wait),
                    )
                    alias = str(data.get("altinbox") or "").strip()
                    if not alias:
                        raise RuntimeError(f"Maildrop altinbox empty: {data}")
                    # alias is local-part only
                    if "@" in alias:
                        alias = alias.split("@", 1)[0]
                    public = f"{alias}@{self.domain}".lower()
                    last_err = None
                    break
                except Exception as exc:  # noqa: BLE001
                    from xconsole_client.mail_channels import ChannelBusy

                    if isinstance(exc, ChannelBusy):
                        raise
                    last_err = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
                    if self.debug:
                        print(f"  [Maildrop] altinbox attempt {attempt + 1}/3 failed: {exc}")
                    if not allow_wait and attempt >= 1:
                        raise
                    time.sleep(0.5 + attempt * 0.4)
            if last_err is not None:
                raise RuntimeError(f"Maildrop altinbox failed: {last_err}")

        self.mailbox = mailbox
        self.alias = alias
        self.address = public
        self._created = True
        if self.debug:
            extra = f" alias={alias}" if alias else ""
            print(f"  [Maildrop] inbox ready: {self.address} (mailbox={mailbox}{extra})")
        return self.address

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
                data = self._gql(
                    _INBOX_QUERY,
                    {"mailbox": self.mailbox},
                    timeout=req_timeout,
                    allow_wait=True,
                )
                msgs = data.get("inbox")
                if not isinstance(msgs, list):
                    return []
                return [m for m in msgs if isinstance(m, dict)]
            except (requests.RequestException, RuntimeError) as exc:
                last_err = exc
                sleep_for = min(0.5 + attempt * 0.5, max(0.0, hard_deadline - time.time()))
                if sleep_for > 0:
                    time.sleep(sleep_for)
        if self.debug and last_err:
            print(f"  [Maildrop] get_messages failed: {last_err}")
        return []

    def get_message_detail(self, message_id: str, *, budget: float = 8.0) -> dict:
        if not message_id or not self._created:
            return {}
        try:
            data = self._gql(
                _MESSAGE_QUERY,
                {"mailbox": self.mailbox, "id": str(message_id)},
                timeout=max(1.0, min(10.0, budget)),
                allow_wait=True,
            )
            msg = data.get("message")
            return msg if isinstance(msg, dict) else {}
        except (requests.RequestException, RuntimeError) as exc:
            if self.debug:
                print(f"  [Maildrop] message detail failed: {exc}")
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
                    f"Maildrop: no x.ai code for {self.address} within "
                    f"{total:.0f}s ({len(seen_ids)} messages seen)"
                )

            messages = self.get_messages(budget=min(8.0, remaining))
            for msg in messages:
                mid = str(
                    msg.get("id")
                    or f"{msg.get('headerfrom', '')}:{msg.get('subject', '')}:{msg.get('date', '')}"
                )
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)

                parts = [
                    str(msg.get("subject") or ""),
                    str(msg.get("headerfrom") or ""),
                    str(msg.get("data") or ""),
                    str(msg.get("html") or ""),
                ]
                raw_id = str(msg.get("id") or "").strip()
                if raw_id:
                    detail = self.get_message_detail(
                        raw_id,
                        budget=min(8.0, deadline - time.time()),
                    )
                    if detail:
                        parts.extend(
                            [
                                str(detail.get("subject") or ""),
                                str(detail.get("headerfrom") or ""),
                                str(detail.get("data") or ""),
                                str(detail.get("html") or ""),
                            ]
                        )

                code = extract_xai_code(" ".join(parts))
                if code:
                    if self.debug:
                        print(f"  [Maildrop] code found: {code}")
                    return code

            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"Maildrop: no x.ai code for {self.address} within "
                    f"{total:.0f}s ({len(seen_ids)} messages seen)"
                )
            if self.debug:
                print(
                    f"  [Maildrop] polling... ({len(seen_ids)} msgs so far, {remaining:.0f}s left)"
                )
            time.sleep(min(self.interval, remaining))
