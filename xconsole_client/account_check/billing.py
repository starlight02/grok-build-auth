# -*- coding: utf-8 -*-
"""cli-chat-proxy billing fetch/parse helpers."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import requests

from .authio import _cli_proxy_base, normalize_base_url
from .constants import DEFAULT_BASE_URL


def billing_url(base_url: str = DEFAULT_BASE_URL) -> str:
    """Weekly SuperGrok/Build usage lives on cli-chat-proxy /v1/billing."""
    base = normalize_base_url(base_url or DEFAULT_BASE_URL)
    # Force Build host: api.x.ai has no equivalent weekly product split.
    if "api.x.ai" in base:
        base = DEFAULT_BASE_URL
    return urljoin(base.rstrip("/") + "/", "billing?format=credits")


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _billing_val(value: Any) -> float | None:
    """Unwrap cli-chat-proxy `{val: N}` wrappers (CPA panel Ng())."""
    if value is None:
        return None
    if isinstance(value, dict) and "val" in value:
        return _as_float(value.get("val"))
    return _as_float(value)


def parse_billing_payload(data: Any) -> dict[str, Any]:
    """Normalize cli-chat-proxy /v1/billing JSON (credits + monthly).

    Two different billing views share this parser:

    1) Weekly credits — GET /v1/billing?format=credits
       SuperGrok example (observed 2026-07-17):
         config.creditUsagePercent = 100.0
         config.productUsage = [
           {product: Api, usagePercent: 96.0},
           {product: GrokBuild, usagePercent: 2.0},
           {product: GrokChat, usagePercent: 2.0},
         ]
         config.currentPeriod = {type: USAGE_PERIOD_TYPE_WEEKLY, start, end}

    2) Monthly included allowance — GET /v1/billing (no format=)
       SuperGrok example (same account):
         config.monthlyLimit.val = 15000   # USD cents = $150.00 / month
         config.used.val         = 10700   # USD cents = $107.00 used this month
         config.billingPeriodStart/End     # calendar month window

       Unit: **USD cents** (integer). CPA panel formats with `value/100` as USD.
       Period: **monthly** included credit, NOT weekly quota.

       CPA management panel SKU thresholds (exact match on monthlyLimit cents):
         15000  ($150)  -> SuperGrok
         150000 ($1500) -> SuperGrok Heavy
       Lite is not labeled in CPA panel yet.

    Free accounts often return 200 with period only (no productUsage bars,
    monthlyLimit=0).
    """

    out: dict[str, Any] = {}
    if not isinstance(data, dict):
        return out
    cfg = data.get("config") if isinstance(data.get("config"), dict) else data
    if not isinstance(cfg, dict):
        return out

    total = _as_float(cfg.get("creditUsagePercent"))
    if total is not None:
        out["credit_usage_percent"] = total

    products: dict[str, float] = {}
    raw_products = cfg.get("productUsage")
    if isinstance(raw_products, list):
        for item in raw_products:
            if not isinstance(item, dict):
                continue
            name = str(item.get("product") or "").strip()
            pct = _as_float(item.get("usagePercent"))
            if not name or pct is None:
                continue
            products[name] = pct
            key = name.lower().replace(" ", "")
            if key in {"api", "xaiapi"}:
                out["api_usage_percent"] = pct
            elif key in {"grokbuild", "build"}:
                out["build_usage_percent"] = pct
            elif key in {"grokchat", "chat"}:
                out["chat_usage_percent"] = pct
    if products:
        out["product_usage"] = products

    period = cfg.get("currentPeriod")
    if isinstance(period, dict):
        out["usage_period_type"] = period.get("type")
        out["usage_period_start"] = period.get("start")
        out["usage_period_end"] = period.get("end")
    if cfg.get("billingPeriodStart"):
        out.setdefault("usage_period_start", cfg.get("billingPeriodStart"))
        out["billing_period_start"] = cfg.get("billingPeriodStart")
    if cfg.get("billingPeriodEnd"):
        out.setdefault("usage_period_end", cfg.get("billingPeriodEnd"))
        out["billing_period_end"] = cfg.get("billingPeriodEnd")

    if "isUnifiedBillingUser" in cfg:
        out["is_unified_billing_user"] = bool(cfg.get("isUnifiedBillingUser"))

    prepaid = cfg.get("prepaidBalance")
    if isinstance(prepaid, dict) and "val" in prepaid:
        out["prepaid_balance"] = prepaid.get("val")
    on_demand_used = cfg.get("onDemandUsed")
    if isinstance(on_demand_used, dict) and "val" in on_demand_used:
        out["on_demand_used"] = on_demand_used.get("val")
    on_demand_cap = cfg.get("onDemandCap")
    if isinstance(on_demand_cap, dict) and "val" in on_demand_cap:
        out["on_demand_cap"] = on_demand_cap.get("val")

    # Monthly included allowance in USD cents (CPA panel plan discriminator).
    # Example: 15000 cents = $150.00 / month included credit.
    monthly_limit = _billing_val(cfg.get("monthlyLimit") or cfg.get("monthly_limit"))
    monthly_used = _billing_val(cfg.get("used"))
    if monthly_limit is not None:
        out["monthly_limit"] = monthly_limit
        out["monthly_limit_cents"] = (
            int(monthly_limit) if float(monthly_limit).is_integer() else monthly_limit
        )
        out["monthly_limit_usd"] = float(monthly_limit) / 100.0
    if monthly_used is not None:
        out["monthly_used"] = monthly_used
        out["monthly_used_cents"] = (
            int(monthly_used) if float(monthly_used).is_integer() else monthly_used
        )
        out["monthly_used_usd"] = float(monthly_used) / 100.0

    return out


def fetch_billing_usage(
    sess: requests.Session,
    headers: dict[str, str],
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """GET weekly + monthly billing from cli-chat-proxy.

    - GET /v1/billing?format=credits -> weekly productUsage % (quota bars)
    - GET /v1/billing -> monthlyLimit/used in **USD cents** (month included credit;
      CPA SuperGrok $150 vs Heavy $1500 discriminator)
    """
    base = _cli_proxy_base(base_url)
    credits_url = billing_url(base_url)
    monthly_url = f"{base}/billing"
    out: dict[str, Any] = {
        "billing_url": credits_url,
        "billing_monthly_url": monthly_url,
    }

    # 1) weekly credits breakdown
    try:
        resp = sess.get(credits_url, headers=headers, timeout=timeout)
    except Exception as exc:
        out["billing_status"] = None
        out["billing_error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["billing_status"] = resp.status_code
    body_text = resp.text or ""
    if resp.status_code != 200:
        out["billing_error"] = body_text[:300] if body_text else f"http_{resp.status_code}"
        # still try monthly below if possible
    else:
        try:
            data = resp.json()
            parsed = parse_billing_payload(data)
            out.update(parsed)
            out["billing"] = parsed
        except Exception:
            out["billing_error"] = body_text[:300] or "invalid_json"

    # 2) monthly limit (plan SKU discriminator used by CPA panel)
    try:
        mresp = sess.get(monthly_url, headers=headers, timeout=min(timeout, 15.0))
        out["billing_monthly_status"] = mresp.status_code
        if mresp.status_code == 200:
            try:
                mdata = mresp.json()
                mparsed = parse_billing_payload(mdata)
                for k in (
                    "monthly_limit",
                    "monthly_limit_cents",
                    "monthly_limit_usd",
                    "monthly_used",
                    "monthly_used_cents",
                    "monthly_used_usd",
                    "billing_period_start",
                    "billing_period_end",
                    "on_demand_cap",
                    "on_demand_used",
                ):
                    if k in mparsed and mparsed[k] is not None:
                        out[k] = mparsed[k]
                # Keep a nested snapshot for JSON consumers.
                out["billing_monthly"] = mparsed
            except Exception as exc:
                out["billing_monthly_error"] = f"{type(exc).__name__}: {exc}"
        elif mresp.status_code != 401:
            out["billing_monthly_error"] = (mresp.text or "")[:200]
    except Exception as exc:
        out["billing_monthly_error"] = f"{type(exc).__name__}: {exc}"

    return out


def format_usd_cents(cents: Any) -> str:
    """Format USD cents as `$150.00`. Returns `--` for missing values."""
    value = _as_float(cents)
    if value is None:
        return "--"
    return f"${value / 100.0:,.2f}"
