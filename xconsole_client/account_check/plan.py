# -*- coding: utf-8 -*-
"""Account plan classification (Free / SuperGrok family)."""

from __future__ import annotations

import re
from typing import Any

import requests

from .authio import _cli_proxy_base
from .billing import _as_float, format_usd_cents
from .constants import DEFAULT_BASE_URL

# Account plan / subscription class (Grok CLI chat-proxy).
# Mirrors CPA management panel labels where known.
PLAN_FREE = "free"
PLAN_SUPERGROK_LITE = "supergrok_lite"
PLAN_SUPERGROK = "supergrok"
PLAN_SUPERGROK_HEAVY = "supergrok_heavy"
PLAN_PAID_OTHER = "paid_other"
PLAN_UNKNOWN = "unknown"

# CPA management.html (xAI quota card) — monthly included credit thresholds.
# Unit: USD cents (panel formats as currency with value/100).
# Period: calendar month (billingPeriodStart..End), NOT weekly credits window.
#   CD = 15e3  (15000 cents  = $150.00)  -> plan_supergrok
#   wD = 15e4  (150000 cents = $1500.00) -> plan_supergrok_heavy
# Lite is shown in grok.com pricing UI but not labeled in CPA panel yet.
CPA_MONTHLY_LIMIT_SUPERGROK = 15_000  # $150.00 / month included (USD cents)
CPA_MONTHLY_LIMIT_SUPERGROK_HEAVY = 150_000  # $1500.00 / month included (USD cents)


# Observed mappings (2026-07-17):
#   Free:      settings.subscription_tier_display="Free", user.subscriptionTier=null
#   SuperGrok: settings.subscription_tier_display="SuperGrok", user.subscriptionTier="GrokPro"
#              monthlyLimit.val = 15000 cents ($150/mo)  — CPA panel
#   SuperGrok Heavy: monthlyLimit.val = 150000 cents ($1500/mo) — CPA panel
#                    (display may still say SuperGrok)
#   SuperGrok Lite:  pricing UI only so far; CPA panel has no label yet
# JWT may carry numeric "tier" on paid accounts; free often omits it.

# JWT may carry numeric "tier" on paid accounts; free often omits it.
_SUPERGROK_LABELS = {
    "supergrok",
    "super grok",
    "grokpro",
    "grok pro",
    "grok_pro",
    "pro",
}
_SUPERGROK_LITE_LABELS = {
    "supergroklite",
    "super grok lite",
    "supergrok lite",
    "groklite",
    "grok lite",
    "lite",
}
_SUPERGROK_HEAVY_LABELS = {
    "supergrokheavy",
    "super grok heavy",
    "supergrok heavy",
    "grokheavy",
    "grok heavy",
    "heavy",
}
_FREE_LABELS = {
    "free",
    "grokfree",
    "grok free",
    "grok_free",
    "none",
    "null",
    "",
}


def _norm_label(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", (value or "").strip().lower())


def classify_plan_from_monthly_limit(monthly_limit: Any) -> dict[str, Any] | None:
    """CPA management panel discriminator on monthly included credit.

    Input unit: **USD cents** from GET /v1/billing `config.monthlyLimit.val`.
    Period: **monthly** included allowance (not weekly productUsage).

    management.html exact match:
      15000  cents ($150)  -> plan_supergrok
      150000 cents ($1500) -> plan_supergrok_heavy
    """
    limit = _as_float(monthly_limit)
    if limit is None:
        return None
    # Ignore free/zero allowances.
    if limit <= 0:
        return None
    # Exact match like CPA panel TD().
    if (
        int(limit) == CPA_MONTHLY_LIMIT_SUPERGROK_HEAVY
        or abs(limit - CPA_MONTHLY_LIMIT_SUPERGROK_HEAVY) < 0.5
    ):
        return {
            "plan": PLAN_SUPERGROK_HEAVY,
            "plan_reason": (
                f"billing.monthlyLimit={int(limit)} cents "
                f"({format_usd_cents(limit)}/mo, CPA SuperGrok Heavy)"
            ),
        }
    if int(limit) == CPA_MONTHLY_LIMIT_SUPERGROK or abs(limit - CPA_MONTHLY_LIMIT_SUPERGROK) < 0.5:
        return {
            "plan": PLAN_SUPERGROK,
            "plan_reason": (
                f"billing.monthlyLimit={int(limit)} cents "
                f"({format_usd_cents(limit)}/mo, CPA SuperGrok)"
            ),
        }
    # Unknown positive monthly allowance — paid, but not a known SuperGrok SKU.
    return {
        "plan": PLAN_PAID_OTHER,
        "plan_reason": (f"billing.monthlyLimit={limit} cents ({format_usd_cents(limit)}/mo)"),
    }


def classify_plan(
    *,
    tier_display: str | None = None,
    subscription_tiers: str | None = None,
    jwt_tier: Any = None,
    has_product_usage: bool | None = None,
    credit_usage_percent: float | None = None,
    monthly_limit: Any = None,
) -> dict[str, Any]:
    """Classify Free / SuperGrok Lite / SuperGrok / SuperGrok Heavy / other.

    1) billing.monthlyLimit exact thresholds in USD cents
       (CPA: 15000=$150/mo SuperGrok, 150000=$1500/mo Heavy)

    2) settings.subscription_tier_display (Free / SuperGrok / SuperGrok Lite / Heavy)
    3) user.subscriptionTier (API enum, e.g. GrokPro)
    4) billing productUsage / creditUsagePercent (paid signal)
    5) JWT tier claim (weak)
    """
    display = str(tier_display or "").strip()
    tiers = str(subscription_tiers or "").strip()
    display_n = _norm_label(display)
    tiers_n = _norm_label(tiers)

    plan = PLAN_UNKNOWN
    reason = "unknown"

    # 1) CPA monthlyLimit thresholds win for paid SuperGrok SKU split.
    by_limit = classify_plan_from_monthly_limit(monthly_limit)
    if by_limit is not None:
        plan = by_limit["plan"]
        reason = by_limit["plan_reason"]
        # If display explicitly says Lite/Heavy and conflicts, prefer more specific display.
        if display_n in {_norm_label(x) for x in _SUPERGROK_LITE_LABELS} and plan == PLAN_SUPERGROK:
            plan, reason = (
                PLAN_SUPERGROK_LITE,
                f"settings.subscription_tier_display={display}",
            )
        elif display_n in {_norm_label(x) for x in _SUPERGROK_HEAVY_LABELS}:
            plan, reason = (
                PLAN_SUPERGROK_HEAVY,
                f"settings.subscription_tier_display={display}",
            )
    elif display and display_n in {_norm_label(x) for x in _FREE_LABELS if x}:
        plan, reason = PLAN_FREE, "settings.subscription_tier_display=Free"
    elif display_n in {_norm_label(x) for x in _SUPERGROK_LITE_LABELS}:
        plan, reason = (
            PLAN_SUPERGROK_LITE,
            f"settings.subscription_tier_display={display}",
        )
    elif display_n in {_norm_label(x) for x in _SUPERGROK_HEAVY_LABELS}:
        plan, reason = (
            PLAN_SUPERGROK_HEAVY,
            f"settings.subscription_tier_display={display}",
        )
    elif display_n in {_norm_label(x) for x in _SUPERGROK_LABELS}:
        plan, reason = PLAN_SUPERGROK, f"settings.subscription_tier_display={display}"
    elif display:
        plan, reason = PLAN_PAID_OTHER, f"settings.subscription_tier_display={display}"
    elif tiers_n in {_norm_label(x) for x in _SUPERGROK_HEAVY_LABELS}:
        plan, reason = PLAN_SUPERGROK_HEAVY, f"user.subscriptionTier={tiers}"
    elif tiers_n in {_norm_label(x) for x in _SUPERGROK_LITE_LABELS}:
        plan, reason = PLAN_SUPERGROK_LITE, f"user.subscriptionTier={tiers}"
    elif tiers_n in {_norm_label(x) for x in _SUPERGROK_LABELS} or "pro" in tiers_n:
        plan, reason = PLAN_SUPERGROK, f"user.subscriptionTier={tiers}"
    elif tiers and tiers_n not in {_norm_label(x) for x in _FREE_LABELS if x}:
        plan, reason = PLAN_PAID_OTHER, f"user.subscriptionTier={tiers}"
    elif has_product_usage or credit_usage_percent is not None:
        plan, reason = (
            (PLAN_SUPERGROK if has_product_usage else PLAN_PAID_OTHER),
            "billing.productUsage",
        )
    elif jwt_tier is not None and str(jwt_tier).strip() not in {"", "0", "None"}:
        plan, reason = PLAN_PAID_OTHER, f"jwt.tier={jwt_tier}"
    else:
        plan, reason = PLAN_FREE, "default_free_no_paid_markers"

    return {
        "plan": plan,
        "plan_reason": reason,
        "tier_display": display or None,
        "subscription_tiers": tiers or None,
        "jwt_tier": jwt_tier,
        "monthly_limit": _as_float(monthly_limit),
    }


def probe_strategy_for_plan(plan: str) -> dict[str, Any]:
    """Which quota signals matter for this plan class."""
    plan = (plan or PLAN_UNKNOWN).lower()
    if plan == PLAN_FREE:
        return {
            "primary": "responses_ratelimit",
            "secondary": ["responses_429_actual_limit"],
            "want_billing": False,  # no productUsage bars; period only
            "want_responses": True,
            "notes": "Free Build rolling window via x-ratelimit-* / 429 actual/limit",
        }
    if plan in {
        PLAN_SUPERGROK,
        PLAN_SUPERGROK_LITE,
        PLAN_SUPERGROK_HEAVY,
    }:
        return {
            "primary": "billing_product_usage",
            "secondary": ["responses_usability", "build_balance_402", "monthly_limit"],
            "want_billing": True,
            "want_responses": True,
            "notes": (
                "SuperGrok family: weekly productUsage + monthly included credit "
                "in USD cents (CPA $150/$1500); 402 = Build balance"
            ),
        }
    if plan == PLAN_PAID_OTHER:
        return {
            "primary": "billing_product_usage",
            "secondary": ["responses_usability", "spending_limit", "monthly_limit"],
            "want_billing": True,
            "want_responses": True,
            "notes": "Other paid tier: billing first; watch spending-limit on paid API",
        }
    return {
        "primary": "both",
        "secondary": [],
        "want_billing": True,
        "want_responses": True,
        "notes": "Unknown plan: run both detectors",
    }


def fetch_plan_info(
    sess: requests.Session,
    headers: dict[str, str],
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 15.0,
    jwt_tier: Any = None,
) -> dict[str, Any]:
    """Detect Free vs SuperGrok/other via /v1/settings + /v1/user."""
    base = _cli_proxy_base(base_url)
    out: dict[str, Any] = {
        "plan": PLAN_UNKNOWN,
        "plan_reason": "not_fetched",
        "settings_status": None,
        "user_status": None,
    }
    tier_display: str | None = None
    subscription_tiers: str | None = None

    # 1) settings — human display label (Free / SuperGrok / ...)
    settings_url = f"{base}/settings"
    out["settings_url"] = settings_url
    try:
        sresp = sess.get(settings_url, headers=headers, timeout=timeout)
        out["settings_status"] = sresp.status_code
        if sresp.status_code == 200:
            try:
                sdata = sresp.json()
            except Exception:
                sdata = None
            if isinstance(sdata, dict):
                raw = sdata.get("subscription_tier_display")
                if raw is not None:
                    tier_display = str(raw).strip()
                out["allow_access"] = sdata.get("allow_access")
                out["default_model"] = sdata.get("default_model")
        elif sresp.status_code != 401:
            out["settings_error"] = (sresp.text or "")[:200]
    except Exception as exc:
        out["settings_error"] = f"{type(exc).__name__}: {exc}"

    # 2) user?include=subscription — API enum (GrokPro / null / ...)
    user_url = f"{base}/user?include=subscription"
    out["user_url"] = user_url
    try:
        uresp = sess.get(user_url, headers=headers, timeout=timeout)
        out["user_status"] = uresp.status_code
        if uresp.status_code == 200:
            try:
                udata = uresp.json()
            except Exception:
                udata = None
            if isinstance(udata, dict):
                raw = udata.get("subscriptionTier")
                if raw is not None:
                    subscription_tiers = str(raw).strip()
                out["has_grok_code_access"] = udata.get("hasGrokCodeAccess")
                out["user_blocked_reason"] = udata.get("userBlockedReason")
                out["team_blocked_reasons"] = udata.get("teamBlockedReasons")
        elif uresp.status_code != 401:
            out["user_error"] = (uresp.text or "")[:200]
    except Exception as exc:
        out["user_error"] = f"{type(exc).__name__}: {exc}"

    classified = classify_plan(
        tier_display=tier_display,
        subscription_tiers=subscription_tiers,
        jwt_tier=jwt_tier,
    )
    out.update(classified)
    out["strategy"] = probe_strategy_for_plan(out["plan"])
    return out
