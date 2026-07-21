# -*- coding: utf-8 -*-
"""mailbox.py — x.ai Cloud Console sign-up: alias email + verification-code receiver.

PURPOSE
=======
Close the last manual step of the x.ai Cloud Console sign-up protocol. After the
client calls ``XConsoleAuthClient.create_email_validation_code(email)``, x.ai
emails a 6-character verification code to the address. The protocol then needs
that code passed to ``XConsoleAuthClient.verify_email_validation_code(email, code)``.

Until now the program stopped and asked the user to paste the code manually. This
module provides an automated receiver backed by the local ``alias_mail`` module
(``alias_mail\\alias_mail.py``), which itself talks to the
Cloudflare mailapi Worker + D1 mail_db.

CAPTURED x.ai CODE SHAPE (THE ONE THING that matters for the regex)
==================================================================
Sample code observed in the mitmproxy capture: 6-character uppercase
alphanumeric (e.g. ``"XAI0X1"``).  No real x.ai email body was captured —
only the code itself.

``alias_mail.extract_code`` ships two regexes:

  * ``(?<!\\d)(\\d{6})(?!\\d)``                       — pure digits
  * ``(?:code|otp|验证码|verification|verify)[^\\d]{0,30}(\\d{4,8})``
                                                      — keyword-anchored digits

NEITHER of those matches the captured alphanumeric shape.  This is why we ship our own
:func:`extract_xai_code` here, and re-implement the polling loop in
:class:`AliasMailCodeReceiver` so the loop runs OUR extractor instead of
``alias_mail.extract_code``.

We DO NOT modify ``alias_mail`` itself — we just wrap it.

DESIGN
======
* Pure standard library + the ``requests`` that ``alias_mail`` already uses.
* No new dependencies.  The ``<venv>`` already
  contains everything we need.
* Public API:
    - :class:`AliasMailAccount`         — fresh ``xai.<name>@<your-domain>`` address
    - :func:`extract_xai_code`          — pure-string helper for the captured shape
    - :class:`AliasMailCodeReceiver`    — poll mail_db and return the code
* Backwards compatibility: ``alias_mail`` is importable as a sibling of this
  module's parent (``alias_mail``).  When ``alias_mail`` cannot
  be imported, the import path of the new helpers degrades to a clear
  ``RuntimeError`` instead of crashing the whole program.

Running tests::

    cd xconsole_client
    python mailbox.py
"""

from __future__ import annotations

import time
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# alias_mail import
# --------------------------------------------------------------------------- #
def _load_alias_mail() -> Any:
    """Return the ``contrib.alias_mail`` package.

    Raises ``RuntimeError`` (NOT ``SystemExit``) with a clear remediation
    message if the module is not discoverable.
    """
    try:
        from contrib import alias_mail as mod

        return mod
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("alias_mail backend not available; see contrib/alias_mail") from exc


# --------------------------------------------------------------------------- #
# extract_xai_code — canonical implementation in codes.py
# --------------------------------------------------------------------------- #
from .codes import extract_xai_code  # noqa: E402


# --------------------------------------------------------------------------- #
# AliasMailAccount
# --------------------------------------------------------------------------- #
class AliasMailAccount:
    """Allocate a fresh alias address on the configured domain rotation.

    Domains come from ``ALIAS_MAIL_DOMAINS`` (see ``alias_mail`` / ``.env.example``).
    Each :meth:`create` call advances the rotating domain state and inserts a
    row into the Cloudflare D1 ``mail_db.address`` table.
    """

    #: Default local-part prefix used for x.ai flows.
    DEFAULT_PREFIX = "xai"

    @classmethod
    def ensure_cf(cls) -> Any:
        """Build a Cloudflare ``CF`` client from the environment.

        Reads ``CLOUDFLARE_API_TOKEN`` (preferred) or
        ``CLOUDFLARE_MCP_READ_ALL_TOKEN`` via ``alias_mail.env_token()``.

        Raises:
            RuntimeError: if neither environment variable is set, with a
                clear message pointing at the alias_mail README env section.
        """
        am = _load_alias_mail()
        try:
            token = am.env_token()
        except SystemExit as exc:
            # alias_mail.env_token calls SystemExit on missing token; we
            # want a recoverable exception so higher layers can fall back
            # to manual input.
            raise RuntimeError(
                "CLOUDFLARE_API_TOKEN (or CLOUDFLARE_MCP_READ_ALL_TOKEN) is "
                "not set. See .env.example. (original: %r)" % (exc,)
            ) from exc
        return am.CF(token)

    def __init__(self, cf: Optional[Any] = None) -> None:
        # cf is optional so callers can defer credential loading to
        # ensure_cf() at the moment of the first .create() call.
        self._cf = cf

    def create(self, cf: Optional[Any] = None, prefix: str = DEFAULT_PREFIX) -> str:
        """Create (or fetch) an alias address and return it as ``local@domain``.

        Args:
            cf: A :class:`alias_mail.CF` client.  If ``None``, falls back to
                the instance's ``self._cf`` (set in the constructor) and
                finally to :meth:`ensure_cf`.
            prefix: Local-part prefix passed to ``alias_mail.random_local``.

        Returns:
            The full address string, e.g. ``"xai.racheladams@mail.example.com"``.

        Raises:
            RuntimeError: if domain rotation is exhausted.  The message
                names the health-state file (``alias_mail.HEALTH_FILE``) so
                the operator knows where to inspect / reset.
        """
        am = _load_alias_mail()
        if cf is None:
            cf = self._cf
        if cf is None:
            cf = AliasMailAccount.ensure_cf()
        # Translate alias_mail's SystemExit to a recoverable RuntimeError.
        try:
            domain = am.next_rotating_domain(commit=True)
        except SystemExit as exc:
            raise RuntimeError(
                "alias_mail: no usable rotating domain.  "
                "Inspect the domain health state file at %s and reset "
                "any entries with status email_unreachable/silent_drop/disabled.  "
                "(original: %r)" % (am.HEALTH_FILE, exc)
            ) from exc
        local = am.random_local(prefix=prefix)
        address = f"{local}@{domain}"
        am.create_alias(cf, address, password=None, source_meta="xconsole_client/mailbox")
        return address


# --------------------------------------------------------------------------- #
# AliasMailCodeReceiver
# --------------------------------------------------------------------------- #
class AliasMailCodeReceiver:
    """Poll ``mail_db.raw_mails`` until a verification code is observed.

    Why not just call ``alias_mail.latest_code``?

    Because that helper runs ``alias_mail.extract_code``, which is digit-only
    and would silently miss the 6-character uppercase alphanumeric shape.  This class
    re-implements the same polling loop and feeds the same subject+body+from
    text into :func:`extract_xai_code` instead.

    Parameters
    ----------
    cf:
        A ready :class:`alias_mail.CF` client.
    address:
        Full alias address, e.g. ``"xai.racheladams@mail.example.com"``.
    timeout:
        Total seconds to keep polling before raising :class:`TimeoutError`.
    interval:
        Seconds between polls.
    digits:
        Kept for API symmetry with ``alias_mail.latest_code`` (default 6).
        Unused here because the code shape is uppercase alphanumeric, not
        digits.
    since_now:
        If ``True``, snapshot the current ``MAX(id)`` at construction time
        and ignore any pre-existing mails whose id is at or below that
        baseline.  Mirrors ``alias_mail poll-domains --since-now``.
    alias_mail_module:
        Optional injected ``alias_mail`` module (useful for tests or for
        callers that have already arranged the import).  If ``None``, the
        normal discovery path is used.
    """

    def __init__(
        self,
        cf: Any,
        address: str,
        timeout: float = 90,
        interval: float = 3,
        digits: int = 6,
        since_now: bool = False,
        alias_mail_module: Any = None,
    ) -> None:
        self.cf = cf
        self.address = address
        self.timeout = float(timeout)
        self.interval = float(interval)
        # Stored for introspection; unused by the matcher because the code
        # shape is uppercase alphanumeric, not digits.
        self.digits = int(digits)
        self.since_now = bool(since_now)
        self.alias_mail = alias_mail_module if alias_mail_module is not None else _load_alias_mail()
        # Normalize the address through alias_mail so a typo in the domain
        # fails fast (and only once) instead of producing a confusing SQL
        # error on the first poll.
        try:
            self.address = self.alias_mail.normalize_address(address)
        except SystemExit as exc:
            raise RuntimeError(
                "AliasMailCodeReceiver: invalid address %r: %r" % (address, exc)
            ) from exc
        self._baseline_max_id: Optional[int] = None
        if self.since_now:
            self._baseline_max_id = self._query_max_id()

    # ----------------------------------------------------------------- helpers
    def _query_max_id(self) -> int:
        """Return the current max id for this address, or 0 if no mails yet."""
        rows = self.cf.d1(
            "SELECT COALESCE(MAX(id), 0) AS max_id FROM raw_mails WHERE address = ?",
            [self.address],
        )
        if not rows:
            return 0
        try:
            return int(rows[0].get("max_id") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _mail_text(mail: dict) -> str:
        # Same concatenation as alias_mail.latest_code, kept locally so a
        # future change in alias_mail doesn't silently drop fields we depend
        # on.
        return " ".join(
            [
                str(mail.get("subject", "") or ""),
                str(mail.get("body", "") or ""),
                str(mail.get("from", "") or ""),
            ]
        )

    # ----------------------------------------------------------------- public
    def wait_for_code(self) -> str:
        """Block until :func:`extract_xai_code` finds a code in any new mail.

        Returns the matched code string.

        Raises:
            TimeoutError: if no code was observed within ``self.timeout`` seconds.
            RuntimeError: if the alias_mail backend is not importable.
        """
        if self.alias_mail is None:
            raise RuntimeError("alias_mail backend not available; see alias_mail\\README.md")
        deadline = time.time() + self.timeout
        seen_ids: set[int] = set()
        last_err: Optional[BaseException] = None
        while True:
            try:
                mails = self.alias_mail.list_mails(self.cf, self.address, limit=20)
            except Exception as exc:  # noqa: BLE001
                # Network blip / transient D1 failure — keep trying until
                # timeout, then surface the last error.
                last_err = exc
                if time.time() >= deadline:
                    raise TimeoutError(
                        "alias_mail.list_mails kept failing for %.0fs: %r" % (self.timeout, exc)
                    ) from exc
                time.sleep(self.interval)
                continue

            for mail in mails:
                mid = mail.get("id")
                try:
                    mid_int = int(mid) if mid is not None else None
                except (TypeError, ValueError):
                    mid_int = None
                if mid_int is not None:
                    if mid_int in seen_ids:
                        continue
                    if self._baseline_max_id is not None and mid_int <= self._baseline_max_id:
                        continue
                    seen_ids.add(mid_int)
                code = extract_xai_code(self._mail_text(mail))
                if code:
                    return code

            if time.time() >= deadline:
                tail = " (last error: %r)" % (last_err,) if last_err is not None else ""
                raise TimeoutError(
                    "no x.ai verification code arrived for %s within %.0fs%s"
                    % (self.address, self.timeout, tail)
                )
            time.sleep(self.interval)


# --------------------------------------------------------------------------- #
# __main__ — canned unit tests
# --------------------------------------------------------------------------- #
def _run_unit_tests() -> int:
    """Run the canned-input unit tests for :func:`extract_xai_code`.

    The asserts are exactly the ones called out in the spec; the rest of the
    test cases are defensive coverage for the same shape.
    """
    spec_cases = [
        # (input, expected, description)
        ("Your code is XAI0X1", "XAI0X1", "synthetic sample, sentence form"),
        (
            "Subject: xAI verification\n\nXAI0X1",
            "XAI0X1",
            "synthetic sample, subject+body form",
        ),
        ("123456", None, "pure digits NOT a code we accept here"),
        ("ABCDEF", "ABCDEF", "plain 6-char uppercase alnum"),
        ("AB12CD34EF", "AB12CD34", "10-char run, 8-char pattern takes first 8"),
        ("", None, "empty input"),
    ]
    extra_cases = [
        (
            "Your verification code is XAI0X1. It expires in 10 minutes.",
            "XAI0X1",
            "typical x.ai email body (synthetic)",
        ),
        ("ABCDEFGH", "ABCDEFGH", "8-char uppercase alnum, primary pattern 2"),
        ("your code is xai0x1", "XAI0X1", "lowercase still works (?i) flag"),
        ("验证码 ABCDEF", "ABCDEF", "Chinese keyword + 6-char code"),
    ]

    all_cases = spec_cases + extra_cases
    fails = 0
    for text, want, desc in all_cases:
        got = extract_xai_code(text)
        ok = got == want
        if not ok:
            fails += 1
        print("  [%s] %s" % ("PASS" if ok else "FAIL", desc))
        if not ok:
            print("       input    = %r" % (text,))
            print("       got      = %r" % (got,))
            print("       expected = %r" % (want,))

    # Re-assert the spec cases as bare asserts so a failure is loud.
    assert extract_xai_code("Your code is XAI0X1") == "XAI0X1"
    assert extract_xai_code("Subject: xAI verification\n\nXAI0X1") == "XAI0X1"
    assert extract_xai_code("123456") is None
    assert extract_xai_code("ABCDEF") == "ABCDEF"
    assert extract_xai_code("AB12CD34EF") == "AB12CD34"
    assert extract_xai_code("") is None

    print()
    if fails:
        print("MAILBOX SELFTEST: %d FAILURE(S)" % fails)
        return 1
    print("MAILBOX SELFTEST: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_unit_tests())
