# -*- coding: utf-8 -*-
"""Probe denial / exhaustion markers and matchers."""

from __future__ import annotations

from typing import Any


# Permanent account rejection (matches grokcli2api-go permanentChatDenialReason).
# Distinct from 429 free-usage / model quota exhaustion, and from transient 403.
CHAT_ENDPOINT_DENIED = "chat_endpoint_denied"

_CHAT_ENDPOINT_DENIED_MSG = "access to the chat endpoint is denied"

_GENERIC_ACCESS_DENIED = "access denied"


# SuperGrok / weekly Build balance exhausted (cli-chat-proxy returns HTTP 402).
# Distinct from free-tier 429 subscription:free-usage-exhausted.
BUILD_USAGE_BALANCE_EXHAUSTED = "build_usage_balance_exhausted"

_BUILD_USAGE_BALANCE_MSG = "grok build usage balance exhausted"


# Paid API / team credit or monthly spending limit (api.x.ai often HTTP 403).
SPENDING_LIMIT_EXHAUSTED = "spending_limit_exhausted"

_SPENDING_LIMIT_CODE = "permission-denied"

_SPENDING_LIMIT_MARKERS = (
    "monthly spending limit",
    "used all available credits",
    "personal-team-blocked:spending-limit",
    "spending-limit",
)


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


def is_build_usage_balance_exhausted(
    status: int | None,
    *,
    body: str = "",
    error: Any = None,
    code: str | None = None,
) -> bool:
    """True for SuperGrok/Build weekly balance exhaustion.

    Observed from cli-chat-proxy as HTTP 402 + "Grok Build usage balance exhausted".
    Bare 402 from that host is almost always this signal.
    """
    candidates: list[str] = []
    if code:
        candidates.append(str(code))
    candidates.extend(_error_text_parts(error))
    if body:
        candidates.append(body)
    joined = " ".join(candidates).lower()
    if BUILD_USAGE_BALANCE_EXHAUSTED in joined or _BUILD_USAGE_BALANCE_MSG in joined:
        return True
    if "usage balance exhausted" in joined and "build" in joined:
        return True
    # Bare 402 from cli-chat-proxy is almost always this balance signal.
    if status == 402:
        return True
    return False


def is_spending_limit_exhausted(
    status: int | None,
    *,
    body: str = "",
    error: Any = None,
    code: str | None = None,
) -> bool:
    """True for paid API / team credit / monthly spending limit blocks."""
    candidates: list[str] = []
    if code:
        candidates.append(str(code))
    candidates.extend(_error_text_parts(error))
    if body:
        candidates.append(body)
    joined = " ".join(candidates).lower()
    if _SPENDING_LIMIT_CODE in joined:
        # permission-denied alone is too broad; require spending markers when present.
        if any(m in joined for m in _SPENDING_LIMIT_MARKERS):
            return True
    return any(m in joined for m in _SPENDING_LIMIT_MARKERS)
