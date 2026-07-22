# -*- coding: utf-8 -*-
"""Grok Build account usability / quota checker."""

from __future__ import annotations

from .authio import (
    b64url_json,
    build_headers,
    jwt_meta,
    mask_email,
    normalize_base_url,
    persist_refreshed_tokens,
    refresh_auth_record,
    resolve_auth_record,
)
from .billing import (
    billing_url,
    fetch_billing_usage,
    format_usd_cents,
    parse_billing_payload,
)
from .cli import collect_paths, main, print_table
from .constants import DEFAULT_BASE_URL, DEFAULT_HEADERS, PROBE_BODY, PROBE_MODEL
from .errors import (
    BUILD_USAGE_BALANCE_EXHAUSTED,
    CHAT_ENDPOINT_DENIED,
    SPENDING_LIMIT_EXHAUSTED,
    format_probe_error,
    is_build_usage_balance_exhausted,
    is_chat_endpoint_denied,
    is_spending_limit_exhausted,
)
from .plan import (
    CPA_MONTHLY_LIMIT_SUPERGROK,
    CPA_MONTHLY_LIMIT_SUPERGROK_HEAVY,
    PLAN_FREE,
    PLAN_PAID_OTHER,
    PLAN_SUPERGROK,
    PLAN_SUPERGROK_HEAVY,
    PLAN_SUPERGROK_LITE,
    PLAN_UNKNOWN,
    classify_plan,
    classify_plan_from_monthly_limit,
    fetch_plan_info,
    probe_strategy_for_plan,
)
from .probe import check_one, header_int, parse_exhausted, summarize_response

__all__ = [
    "BUILD_USAGE_BALANCE_EXHAUSTED",
    "CHAT_ENDPOINT_DENIED",
    "CPA_MONTHLY_LIMIT_SUPERGROK",
    "CPA_MONTHLY_LIMIT_SUPERGROK_HEAVY",
    "DEFAULT_BASE_URL",
    "DEFAULT_HEADERS",
    "PLAN_FREE",
    "PLAN_PAID_OTHER",
    "PLAN_SUPERGROK",
    "PLAN_SUPERGROK_HEAVY",
    "PLAN_SUPERGROK_LITE",
    "PLAN_UNKNOWN",
    "PROBE_BODY",
    "PROBE_MODEL",
    "SPENDING_LIMIT_EXHAUSTED",
    "b64url_json",
    "billing_url",
    "build_headers",
    "check_one",
    "classify_plan",
    "classify_plan_from_monthly_limit",
    "collect_paths",
    "fetch_billing_usage",
    "fetch_plan_info",
    "format_probe_error",
    "format_usd_cents",
    "header_int",
    "is_build_usage_balance_exhausted",
    "is_chat_endpoint_denied",
    "is_spending_limit_exhausted",
    "jwt_meta",
    "main",
    "mask_email",
    "normalize_base_url",
    "parse_billing_payload",
    "parse_exhausted",
    "persist_refreshed_tokens",
    "print_table",
    "probe_strategy_for_plan",
    "refresh_auth_record",
    "resolve_auth_record",
    "summarize_response",
]
