# -*- coding: utf-8 -*-
"""Build endpoint probe: check_one and response summarization."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from .authio import (
    build_headers,
    jwt_meta,
    mask_email,
    normalize_base_url,
    refresh_auth_record,
    resolve_auth_record,
    _proxy_from_env,
)
from .billing import fetch_billing_usage
from .constants import DEFAULT_BASE_URL, PROBE_BODY
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
    PLAN_FREE,
    PLAN_PAID_OTHER,
    PLAN_SUPERGROK,
    PLAN_SUPERGROK_HEAVY,
    PLAN_SUPERGROK_LITE,
    PLAN_UNKNOWN,
    classify_plan,
    fetch_plan_info,
    probe_strategy_for_plan,
)


def header_int(headers: Any, name: str) -> int | None:
    raw = headers.get(name) if headers is not None else None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_exhausted(body: str) -> tuple[int | None, int | None]:
    m = re.search(r"tokens\s*\(actual/limit\)\s*:\s*(\d+)\s*/\s*(\d+)", body or "", re.I)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def summarize_response(resp: requests.Response) -> dict[str, Any]:
    out: dict[str, Any] = {"status": resp.status_code}
    for key, header in (
        ("limit_tokens", "x-ratelimit-limit-tokens"),
        ("remaining_tokens", "x-ratelimit-remaining-tokens"),
        ("limit_requests", "x-ratelimit-limit-requests"),
        ("remaining_requests", "x-ratelimit-remaining-requests"),
    ):
        val = header_int(resp.headers, header)
        if val is not None:
            out[key] = val

    body_text = resp.text or ""
    if resp.status_code == 429:
        actual, limit = parse_exhausted(body_text)
        if actual is not None and limit is not None:
            out.update(
                {
                    "code": "subscription:free-usage-exhausted",
                    "actual_tokens": actual,
                    "limit_tokens": limit,
                    "remaining_tokens": max(0, limit - actual),
                    "reset": "rolling 24h window",
                }
            )
        else:
            out["error"] = body_text[:300]
        return out

    data: Any = None
    try:
        data = resp.json()
    except Exception:
        if body_text:
            out["error"] = body_text[:300]
        if is_chat_endpoint_denied(resp.status_code, body=body_text, error=out.get("error")):
            out["code"] = CHAT_ENDPOINT_DENIED
        elif is_build_usage_balance_exhausted(
            resp.status_code, body=body_text, error=out.get("error")
        ):
            out["code"] = BUILD_USAGE_BALANCE_EXHAUSTED
        elif is_spending_limit_exhausted(resp.status_code, body=body_text, error=out.get("error")):
            out["code"] = SPENDING_LIMIT_EXHAUSTED
        return out

    err_obj: Any = None
    top_code: str | None = None
    if isinstance(data, dict):
        if model := data.get("model"):
            out["model"] = model
        usage = data.get("usage")
        if isinstance(usage, dict):
            total = usage.get("total_tokens") or usage.get("totalTokens")
            if total is not None:
                out["probe_total_tokens"] = total
        err_obj = data.get("error")
        if err_obj is not None and resp.status_code >= 400:
            out["error"] = format_probe_error(err_obj, body_text)
        raw_code = data.get("code")
        if isinstance(raw_code, str) and raw_code.strip():
            top_code = raw_code.strip()
            # Keep upstream code unless we later map to a more specific label.
            out.setdefault("upstream_code", top_code)

    err_for_match = err_obj if err_obj is not None else out.get("error")
    if is_chat_endpoint_denied(
        resp.status_code,
        body=body_text,
        error=err_for_match,
        code=top_code,
    ):
        out["code"] = CHAT_ENDPOINT_DENIED
    elif is_build_usage_balance_exhausted(
        resp.status_code,
        body=body_text,
        error=err_for_match,
        code=top_code,
    ):
        out["code"] = BUILD_USAGE_BALANCE_EXHAUSTED
        # No rate-limit headers on 402; mark remaining as zero when known exhausted.
        out.setdefault("remaining_tokens", 0)
    elif is_spending_limit_exhausted(
        resp.status_code,
        body=body_text,
        error=err_for_match,
        code=top_code,
    ):
        out["code"] = SPENDING_LIMIT_EXHAUSTED
    elif top_code and "code" not in out:
        out["code"] = top_code
    return out


def _apply_probe_summary(out: dict[str, Any], summary: dict[str, Any]) -> None:
    out["probe"] = summary
    out["status"] = summary.get("status")
    for k in (
        "remaining_tokens",
        "limit_tokens",
        "remaining_requests",
        "limit_requests",
        "model",
        "code",
        "actual_tokens",
        "reset",
        "probe_total_tokens",
    ):
        if k in summary:
            out[k] = summary[k]
        elif k in out and k in {
            "remaining_tokens",
            "limit_tokens",
            "remaining_requests",
            "limit_requests",
            "model",
            "code",
            "actual_tokens",
            "reset",
            "probe_total_tokens",
        }:
            # Clear stale fields from a previous probe attempt.
            out.pop(k, None)
    if summary.get("error"):
        out["probe_error"] = summary["error"]
    else:
        out.pop("probe_error", None)


def check_one(
    path: Path,
    *,
    timeout: float = 45.0,
    check_models: bool = True,
    check_billing: bool = True,
    check_plan: bool = True,
    session: requests.Session | None = None,
    refresh: bool = True,
    proxy: str = "",
) -> dict[str, Any]:
    now = int(time.time())
    out: dict[str, Any] = {
        "file": path.name,
        "path": str(path),
        "usable": False,
        "reasons": [],
        "refreshed": False,
        "refresh_persisted": False,
    }
    try:
        auth = resolve_auth_record(path)
    except Exception as exc:
        out["error"] = f"load_failed: {type(exc).__name__}: {exc}"
        out["reasons"] = ["load_failed"]
        return out

    email = str(auth.get("email") or "")
    token = str(auth.get("access_token") or "").strip()
    refresh_token = str(auth.get("refresh_token") or "").strip()
    out["email"] = email
    out["email_masked"] = mask_email(email)
    out["disabled"] = bool(auth.get("disabled"))
    out["auth_file"] = auth.get("_auth_file") or str(path)
    out["has_refresh_token"] = bool(refresh_token)

    if out["disabled"]:
        out["reasons"].append("disabled")

    meta = (
        jwt_meta(token, now=now)
        if token
        else {
            "valid_jwt": False,
            "expired": True,
            "ttl_sec": None,
            "scope": None,
            "iss": None,
        }
    )
    out["jwt"] = {
        "valid": meta.get("valid_jwt"),
        "expired": meta.get("expired"),
        "ttl_sec": meta.get("ttl_sec"),
        "scope": meta.get("scope"),
        "iss": meta.get("iss"),
        "tier": meta.get("tier"),
        "team_id": meta.get("team_id"),
    }

    did_refresh = False
    proxy = proxy or _proxy_from_env()

    def _maybe_refresh(reason: str) -> bool:
        nonlocal auth, token, refresh_token, meta, did_refresh, now
        if not refresh or did_refresh:
            return False
        if not refresh_token:
            out["reasons"].append("missing_refresh_token")
            return False
        result = refresh_auth_record(auth, timeout=min(timeout, 30.0), proxy=proxy)
        if not result.get("ok"):
            out["refresh_error"] = result.get("error") or "refresh_failed"
            out["reasons"].append(f"refresh_failed:{reason}")
            return False
        did_refresh = True
        out["refreshed"] = True
        out["refresh_persisted"] = bool(result.get("persisted"))
        # Drop prior failed-refresh noise once a later attempt succeeds.
        out["reasons"] = [
            r
            for r in out["reasons"]
            if not str(r).startswith("refresh_failed:")
            and r
            not in {
                "access_token_expired",
                "missing_refresh_token",
                "missing_access_token",
            }
        ]
        out["reasons"].append("access_token_refreshed")
        if result.get("error"):
            out["refresh_error"] = result["error"]
        else:
            out.pop("refresh_error", None)
        auth = result["auth"]
        token = str(auth.get("access_token") or "").strip()
        refresh_token = str(auth.get("refresh_token") or "").strip()
        now = int(time.time())
        meta = jwt_meta(token, now=now)
        out["jwt"] = {
            "valid": meta.get("valid_jwt"),
            "expired": meta.get("expired"),
            "ttl_sec": meta.get("ttl_sec"),
            "scope": meta.get("scope"),
            "iss": meta.get("iss"),
            "tier": meta.get("tier"),
            "team_id": meta.get("team_id"),
        }
        return True

    # Proactive refresh when access token is missing/expired.
    if not token:
        if not _maybe_refresh("missing_access_token"):
            out["reasons"].append("missing_access_token")
            out["error"] = "missing access_token / oauth_access_token"
            return out
    elif meta.get("expired"):
        if not _maybe_refresh("jwt_expired"):
            out["reasons"].append("access_token_expired")

    if not token:
        out["reasons"].append("missing_access_token")
        out["error"] = "missing access_token / oauth_access_token"
        return out

    base_url = normalize_base_url(str(auth.get("base_url") or DEFAULT_BASE_URL))
    headers = build_headers(auth)
    sess = session or requests.Session()

    # 0) plan detection (Free / SuperGrok / other paid) — drives which quota path we trust
    plan_info: dict[str, Any] = {
        "plan": PLAN_UNKNOWN,
        "plan_reason": "skipped",
        "strategy": probe_strategy_for_plan(PLAN_UNKNOWN),
    }
    if check_plan and token:
        plan_info = fetch_plan_info(
            sess,
            headers,
            base_url=base_url,
            timeout=min(timeout, 15.0),
            jwt_tier=meta.get("tier"),
        )
    elif not check_plan:
        plan_info["plan_reason"] = "check_plan_disabled"
    out["plan_info"] = plan_info
    out["plan"] = plan_info.get("plan") or PLAN_UNKNOWN
    out["plan_reason"] = plan_info.get("plan_reason")
    out["tier_display"] = plan_info.get("tier_display")
    out["subscription_tiers"] = plan_info.get("subscription_tiers")
    strategy = plan_info.get("strategy") or probe_strategy_for_plan(out["plan"])
    out["probe_strategy"] = strategy
    if out.get("plan") and out["plan"] != PLAN_UNKNOWN:
        out["reasons"].append(f"plan={out['plan']}")

    # Whether to hit billing: caller flag AND plan strategy (free skips by default).
    want_billing = bool(check_billing)
    if out["plan"] == PLAN_FREE:
        want_billing = False
    elif out["plan"] in (
        PLAN_SUPERGROK,
        PLAN_SUPERGROK_LITE,
        PLAN_SUPERGROK_HEAVY,
        PLAN_PAID_OTHER,
        PLAN_UNKNOWN,
    ):
        want_billing = bool(check_billing)

    # 1) responses probe
    # Free: authoritative for rolling free-token window (headers / 429 actual/limit)
    # SuperGrok: still needed for usability + 402 build-balance signal
    responses_url = urljoin(base_url.rstrip("/") + "/", "responses")
    out["responses_url"] = responses_url
    try:
        resp = sess.post(responses_url, headers=headers, json=PROBE_BODY, timeout=timeout)
        summary = summarize_response(resp)
        _apply_probe_summary(out, summary)
    except Exception as exc:
        out["status"] = None
        out["probe_error"] = f"{type(exc).__name__}: {exc}"
        out["reasons"].append("probe_network_error")

    # Reactive refresh once on 401 (token rejected even if JWT looked fresh).
    if out.get("status") == 401 and refresh and not did_refresh and refresh_token:
        if _maybe_refresh("probe_401"):
            headers = build_headers(auth)
            # Re-detect plan after refresh (tier labels can only be read with valid token).
            if check_plan:
                plan_info = fetch_plan_info(
                    sess,
                    headers,
                    base_url=base_url,
                    timeout=min(timeout, 15.0),
                    jwt_tier=meta.get("tier"),
                )
                out["plan_info"] = plan_info
                out["plan"] = plan_info.get("plan") or PLAN_UNKNOWN
                out["plan_reason"] = plan_info.get("plan_reason")
                out["tier_display"] = plan_info.get("tier_display")
                out["subscription_tiers"] = plan_info.get("subscription_tiers")
                strategy = plan_info.get("strategy") or probe_strategy_for_plan(out["plan"])
                out["probe_strategy"] = strategy
                want_billing = bool(check_billing) and out["plan"] != PLAN_FREE
            try:
                resp = sess.post(responses_url, headers=headers, json=PROBE_BODY, timeout=timeout)
                summary = summarize_response(resp)
                _apply_probe_summary(out, summary)
                # Drop prior network/auth noise after successful re-probe path.
                out["reasons"] = [
                    r
                    for r in out["reasons"]
                    if r
                    not in {
                        "probe_network_error",
                        "access_token_expired",
                        "auth_rejected_401",
                    }
                    and not str(r).startswith("refresh_failed:")
                ]
                if out.get("plan") and f"plan={out['plan']}" not in out["reasons"]:
                    out["reasons"].insert(0, f"plan={out['plan']}")

            except Exception as exc:
                out["status"] = None
                out["probe_error"] = f"{type(exc).__name__}: {exc}"
                out["reasons"].append("probe_network_error")

    # 2) weekly SuperGrok/paid usage breakdown (precise product %).
    # Source: GET cli-chat-proxy /v1/billing?format=credits
    # Free accounts: skip (no productUsage bars). SuperGrok/paid: primary quota view.
    if want_billing and token and out.get("status") not in (401,):
        billing = fetch_billing_usage(
            sess,
            headers,
            base_url=base_url,
            timeout=min(timeout, 20.0),
        )
        out["billing"] = billing
        for k in (
            "billing_status",
            "billing_error",
            "credit_usage_percent",
            "api_usage_percent",
            "build_usage_percent",
            "chat_usage_percent",
            "product_usage",
            "usage_period_type",
            "usage_period_start",
            "usage_period_end",
            "prepaid_balance",
            "on_demand_used",
            "on_demand_cap",
            "is_unified_billing_user",
            "billing_url",
            "billing_monthly_url",
            "billing_monthly_status",
            "monthly_limit",
            "monthly_limit_cents",
            "monthly_limit_usd",
            "monthly_used",
            "monthly_used_cents",
            "monthly_used_usd",
            "billing_period_start",
            "billing_period_end",
            "billing_monthly",
        ):
            if k in billing:
                out[k] = billing[k]
        # Refine plan from monthlyLimit (CPA SuperGrok vs Heavy) and productUsage.
        refined = classify_plan(
            tier_display=out.get("tier_display"),
            subscription_tiers=out.get("subscription_tiers"),
            jwt_tier=meta.get("tier"),
            has_product_usage=bool(billing.get("product_usage")),
            credit_usage_percent=billing.get("credit_usage_percent"),
            monthly_limit=billing.get("monthly_limit"),
        )
        if refined.get("plan") and refined["plan"] != out.get("plan"):
            out["plan"] = refined["plan"]
            out["plan_reason"] = refined.get("plan_reason")
            out["plan_info"] = {**(out.get("plan_info") or {}), **refined}
            out["probe_strategy"] = probe_strategy_for_plan(out["plan"])
            out["reasons"] = [r for r in out["reasons"] if not str(r).startswith("plan=")]
            out["reasons"].insert(0, f"plan={out['plan']}")
        elif refined.get("monthly_limit") is not None and out.get("monthly_limit") is None:
            out["monthly_limit"] = refined["monthly_limit"]

    # 3) optional /models smoke (uses final token)
    if check_models:
        models_url = urljoin(base_url.rstrip("/") + "/", "models")
        try:
            mresp = sess.get(models_url, headers=headers, timeout=timeout)
            models_info: dict[str, Any] = {"status": mresp.status_code}
            if mresp.ok:
                try:
                    data = mresp.json()
                    ids: list[str] = []
                    if isinstance(data, dict) and isinstance(data.get("data"), list):
                        for item in data["data"]:
                            if isinstance(item, dict) and item.get("id"):
                                ids.append(str(item["id"]))
                    models_info["model_count"] = len(ids)
                    models_info["sample"] = ids[:8]
                except Exception:
                    models_info["body_prefix"] = (mresp.text or "")[:120]
            else:
                models_info["body_prefix"] = (mresp.text or "")[:120]
            out["models"] = models_info
        except Exception as exc:
            out["models"] = {"error": f"{type(exc).__name__}: {exc}"}

    # verdict — use final JWT meta (post-refresh if any)
    status = out.get("status")
    if out["disabled"]:
        out["usable"] = False
    elif meta.get("expired"):
        out["usable"] = False
        if "access_token_expired" not in out["reasons"]:
            out["reasons"].append("access_token_expired")
    elif status == 200:
        out["usable"] = True
        out["reasons"].append("responses_ok")
        if out.get("remaining_tokens") is not None:
            out["reasons"].append(f"remaining_tokens={out['remaining_tokens']}")
        if out.get("build_usage_percent") is not None:
            out["reasons"].append(f"build_usage={out['build_usage_percent']}%")
        if out.get("credit_usage_percent") is not None:
            out["reasons"].append(f"credit_usage={out['credit_usage_percent']}%")
    elif status == 429:
        out["usable"] = False
        if out.get("code") == BUILD_USAGE_BALANCE_EXHAUSTED or is_build_usage_balance_exhausted(
            status,
            body=str(out.get("probe_error") or ""),
            error=out.get("probe_error"),
            code=str(out.get("code") or "") or None,
        ):
            out["code"] = BUILD_USAGE_BALANCE_EXHAUSTED
            out["reasons"].append(BUILD_USAGE_BALANCE_EXHAUSTED)
        else:
            out["reasons"].append("quota_exhausted_or_rate_limited")
            if out.get("code"):
                out["reasons"].append(str(out["code"]))
    elif status == 402 or out.get("code") == BUILD_USAGE_BALANCE_EXHAUSTED:
        out["usable"] = False
        if out.get("code") != BUILD_USAGE_BALANCE_EXHAUSTED and is_build_usage_balance_exhausted(
            status,
            body=str(out.get("probe_error") or ""),
            error=out.get("probe_error"),
            code=str(out.get("code") or "") or None,
        ):
            out["code"] = BUILD_USAGE_BALANCE_EXHAUSTED
        if out.get("code") == BUILD_USAGE_BALANCE_EXHAUSTED:
            out["reasons"].append(BUILD_USAGE_BALANCE_EXHAUSTED)
        else:
            out["reasons"].append(f"http_{status}")
            if out.get("probe_error"):
                out["reasons"].append(str(out["probe_error"])[:120])
    elif status in (401, 403):
        out["usable"] = False
        if out.get("code") == CHAT_ENDPOINT_DENIED or is_chat_endpoint_denied(
            status,
            body=str((out.get("probe") or {}).get("error") or out.get("probe_error") or ""),
            error=(out.get("probe") or {}).get("error") or out.get("probe_error"),
            code=str(out.get("code") or "") or None,
        ):
            out["chat_endpoint_denied"] = True
            out["code"] = CHAT_ENDPOINT_DENIED
            out["reasons"].append(CHAT_ENDPOINT_DENIED)
        elif out.get("code") == SPENDING_LIMIT_EXHAUSTED or is_spending_limit_exhausted(
            status,
            body=str(out.get("probe_error") or ""),
            error=out.get("probe_error"),
            code=str(out.get("code") or "") or None,
        ):
            out["code"] = SPENDING_LIMIT_EXHAUSTED
            out["reasons"].append(SPENDING_LIMIT_EXHAUSTED)
        else:
            out["reasons"].append(f"auth_rejected_{status}")
    elif status is None:
        out["usable"] = False
        if "probe_network_error" not in out["reasons"]:
            out["reasons"].append("probe_failed")
    else:
        out["usable"] = False
        # Catch balance text even if status is unexpected.
        if is_build_usage_balance_exhausted(
            status,
            body=str(out.get("probe_error") or ""),
            error=out.get("probe_error"),
            code=str(out.get("code") or "") or None,
        ):
            out["code"] = BUILD_USAGE_BALANCE_EXHAUSTED
            out["reasons"].append(BUILD_USAGE_BALANCE_EXHAUSTED)
        else:
            out["reasons"].append(f"http_{status}")
            if out.get("probe_error"):
                out["reasons"].append(str(out["probe_error"])[:120])

    if not out["reasons"]:
        out["reasons"] = ["unknown"]
    return out
