# -*- coding: utf-8 -*-
"""CLI for Grok Build account checking."""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .billing import format_usd_cents
from .probe import check_one


def collect_paths(inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for raw in inputs:
        p = Path(raw).expanduser()
        candidates: list[Path] = []
        if any(ch in raw for ch in "*?[]"):
            # glob relative to cwd
            candidates = sorted(Path().glob(raw))
        elif p.is_dir():
            # both naming styles
            candidates = sorted(
                set(p.glob("*.json"))
                | set(p.glob("xai-*.json"))
                | set(p.glob("xai*.json"))
                | set(p.glob("account_*.json"))
            )
        elif p.is_file():
            candidates = [p]
        else:
            print(f"warn: not found: {raw}", file=sys.stderr)
            continue
        for c in candidates:
            if not c.is_file() or c.suffix.lower() != ".json":
                continue
            if c.name == ".DS_Store":
                continue
            key = str(c.resolve())
            if key in seen:
                continue
            # skip obvious non-auth dumps
            try:
                head = c.read_text(encoding="utf-8", errors="ignore")[:200].lower()
            except Exception:
                continue
            if (
                "access_token" not in head
                and "oauth_access_token" not in head
                and "cliproxyapi_auth" not in head
            ):
                # still allow if full file has token (small files)
                try:
                    full = c.read_text(encoding="utf-8", errors="ignore").lower()
                except Exception:
                    continue
                if (
                    "access_token" not in full
                    and "oauth_access_token" not in full
                    and "cliproxyapi_auth" not in full
                ):
                    continue
            seen.add(key)
            paths.append(c)
    return paths


def fmt_ttl(sec: int | None) -> str:
    if sec is None:
        return "--"
    if sec < 0:
        return "expired"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def print_table(results: list[dict[str, Any]]) -> None:
    if not results:
        print("No auth JSON files found.")
        return

    usable_n = sum(1 for r in results if r.get("usable"))
    refreshed_n = sum(1 for r in results if r.get("refreshed"))
    queued_n = sum(1 for r in results if r.get("refresh_queued"))
    print(
        f"checked={len(results)} usable={usable_n} unusable={len(results) - usable_n}"
        f"  refreshed={refreshed_n} queued={queued_n}"
    )
    print("-" * 88)
    for r in results:
        flag = "OK " if r.get("usable") else "NO "
        label = r.get("email_masked") or r.get("email") or r.get("file")
        status = r.get("status", "-")
        rem = r.get("remaining_tokens", "--")
        lim = r.get("limit_tokens", "--")
        req = r.get("remaining_requests", "--")
        req_lim = r.get("limit_requests", "--")
        ttl = fmt_ttl((r.get("jwt") or {}).get("ttl_sec"))
        reasons = ", ".join(r.get("reasons") or [])
        plan = r.get("plan") or "unknown"
        tier = r.get("tier_display") or r.get("subscription_tiers") or "-"
        print(
            f"[{flag}] {label}  plan={plan}({tier})  status={status}  "
            f"tokens={rem}/{lim}  req={req}/{req_lim}  jwt_ttl={ttl}"
        )
        print(f"       file={r.get('file')}  reasons={reasons}")
        if r.get("refresh_queued") and not r.get("refreshed"):
            print(
                "       refresh_queued=yes"
                + (
                    f" reason={r.get('refresh_queue_reason')}"
                    if r.get("refresh_queue_reason")
                    else ""
                )
            )
        if r.get("refreshed"):
            persist = "persisted" if r.get("refresh_persisted") else "memory-only"
            print(f"       refreshed=yes ({persist})")
        if r.get("refresh_error"):
            print(f"       refresh_error={str(r['refresh_error'])[:160]}")
        if r.get("probe_error") and not r.get("usable"):
            print(f"       probe_error={r['probe_error'][:160]}")
        # Weekly SuperGrok product split (/v1/billing?format=credits)
        # + monthly included credit in USD cents (/v1/billing, CPA SKU thresholds).
        if (
            r.get("billing_status") is not None
            or r.get("credit_usage_percent") is not None
            or r.get("monthly_limit") is not None
        ):
            parts: list[str] = []
            if r.get("credit_usage_percent") is not None:
                parts.append(f"total={r['credit_usage_percent']}%")
            if r.get("api_usage_percent") is not None:
                parts.append(f"api={r['api_usage_percent']}%")
            if r.get("build_usage_percent") is not None:
                parts.append(f"build={r['build_usage_percent']}%")
            if r.get("chat_usage_percent") is not None:
                parts.append(f"chat={r['chat_usage_percent']}%")
            if r.get("monthly_limit") is not None:
                used = r.get("monthly_used")
                limit_usd = format_usd_cents(r["monthly_limit"])
                if used is not None:
                    parts.append(
                        f"monthly={format_usd_cents(used)}/{limit_usd}"
                        f" ({int(r['monthly_used']) if float(r['monthly_used']).is_integer() else r['monthly_used']}"
                        f"/{int(r['monthly_limit']) if float(r['monthly_limit']).is_integer() else r['monthly_limit']}¢)"
                    )
                else:
                    parts.append(
                        f"monthlyLimit={limit_usd}"
                        f" ({int(r['monthly_limit']) if float(r['monthly_limit']).is_integer() else r['monthly_limit']}¢)"
                    )

            period = ""
            if r.get("usage_period_start") or r.get("usage_period_end"):
                period = (
                    f"  period={r.get('usage_period_start', '?')}..{r.get('usage_period_end', '?')}"
                )
            if parts:
                print(f"       billing={r.get('billing_status', '-')} " + " ".join(parts) + period)
            elif r.get("billing_error"):
                print(
                    f"       billing={r.get('billing_status', '-')} "
                    f"error={str(r['billing_error'])[:120]}"
                )
            else:
                print(
                    f"       billing={r.get('billing_status', '-')} (no productUsage bars){period}"
                )

        models = r.get("models") or {}
        if models.get("status") is not None:
            sample = models.get("sample") or []
            print(
                f"       models={models.get('status')} "
                f"count={models.get('model_count', '-')} "
                f"sample={sample}"
            )
        elif models.get("error"):
            print(f"       models_error={models['error'][:120]}")
    print("-" * 88)
    print(f"summary: {usable_n}/{len(results)} usable")


def _should_queue_refresh(result: dict[str, Any]) -> bool:
    """True when probe found auth expiry/401 and a refresh_token is available."""
    if result.get("disabled") or result.get("chat_endpoint_denied"):
        return False
    if not result.get("has_refresh_token"):
        return False
    if result.get("status") == 401:
        return True
    jwt = result.get("jwt") or {}
    if jwt.get("expired"):
        return True
    reasons = set(result.get("reasons") or [])
    return bool(
        reasons
        & {
            "access_token_expired",
            "auth_rejected_401",
            "missing_access_token",
        }
    )


def _queue_reason(result: dict[str, Any]) -> str:
    if result.get("status") == 401:
        return "probe_401"
    jwt = result.get("jwt") or {}
    if jwt.get("expired"):
        return "jwt_expired"
    reasons = set(result.get("reasons") or [])
    if "missing_access_token" in reasons:
        return "missing_access_token"
    if "access_token_expired" in reasons:
        return "access_token_expired"
    if "auth_rejected_401" in reasons:
        return "auth_rejected_401"
    return "unknown"


def _safe_check_one(
    path: Path,
    *,
    timeout: float,
    check_models: bool,
    check_billing: bool,
    check_plan: bool,
    refresh: bool,
) -> dict[str, Any]:
    """Thread worker: own Session; never raise out of the pool."""
    try:
        return check_one(
            path,
            timeout=timeout,
            check_models=check_models,
            check_billing=check_billing,
            check_plan=check_plan,
            session=None,  # per-thread session
            refresh=refresh,
        )
    except Exception as exc:
        return {
            "file": path.name,
            "path": str(path),
            "usable": False,
            "reasons": ["check_exception"],
            "error": f"{type(exc).__name__}: {exc}",
            "refreshed": False,
            "refresh_persisted": False,
            "has_refresh_token": False,
        }


def _run_parallel_checks(
    paths: list[Path],
    *,
    workers: int,
    timeout: float,
    check_models: bool,
    check_billing: bool,
    check_plan: bool,
    refresh: bool,
) -> list[dict[str, Any]]:
    """Check paths with a thread pool; return results in input order."""
    if not paths:
        return []
    workers = max(1, min(int(workers), len(paths), 32))
    if workers == 1:
        return [
            _safe_check_one(
                p,
                timeout=timeout,
                check_models=check_models,
                check_billing=check_billing,
                check_plan=check_plan,
                refresh=refresh,
            )
            for p in paths
        ]

    by_path: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(
                _safe_check_one,
                p,
                timeout=timeout,
                check_models=check_models,
                check_billing=check_billing,
                check_plan=check_plan,
                refresh=refresh,
            ): p
            for p in paths
        }
        for fut in as_completed(futs):
            path = futs[fut]
            by_path[str(path)] = fut.result()
    return [by_path[str(p)] for p in paths]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check whether Grok Build auth JSON accounts are usable"
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="auth files, directories, or globs (default: ./cliproxyapi_auth)",
    )
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument(
        "-j",
        "--workers",
        type=int,
        default=4,
        help="parallel probe threads (default 4, max 32)",
    )
    parser.add_argument(
        "--no-models",
        action="store_true",
        help="Skip GET /v1/models smoke check",
    )
    parser.add_argument(
        "--no-billing",
        action="store_true",
        help="Skip GET /v1/billing?format=credits weekly usage breakdown",
    )
    parser.add_argument(
        "--no-plan",
        action="store_true",
        help="Skip Free/SuperGrok plan detection via /v1/settings and /v1/user",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Do not refresh expired/401 access tokens via refresh_token",
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Also check files marked disabled=true",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON results")
    parser.add_argument(
        "--only-usable",
        action="store_true",
        help="Only print usable accounts (table mode)",
    )
    parser.add_argument(
        "--only-unusable",
        action="store_true",
        help="Only print unusable accounts (table mode)",
    )
    args = parser.parse_args(argv)

    inputs = list(args.paths) if args.paths else ["cliproxyapi_auth"]
    paths = collect_paths(inputs)
    if not paths:
        print("No matching auth JSON files.", file=sys.stderr)
        return 2

    # Optional disabled filter before network.
    if not args.include_disabled:
        filtered: list[Path] = []
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("disabled") is True:
                    continue
            except Exception:
                pass
            filtered.append(path)
        paths = filtered

    workers = max(1, min(int(args.workers or 4), 32))
    do_refresh = not args.no_refresh
    check_models = not args.no_models
    check_billing = not args.no_billing
    check_plan = not args.no_plan

    # Phase 1: parallel probe only — never refresh mid-scan (keeps other accounts moving).
    if not args.json:
        print(
            f"phase1 probe: {len(paths)} files, workers={workers}, refresh=deferred",
            flush=True,
        )
    results = _run_parallel_checks(
        paths,
        workers=workers,
        timeout=args.timeout,
        check_models=check_models,
        check_billing=check_billing,
        check_plan=check_plan,
        refresh=False,
    )

    # Phase 2: queue 401 / expired JWT, refresh+reprobe at the end (also parallel).
    if do_refresh:
        queue_paths: list[Path] = []
        queue_idx: dict[str, int] = {}
        for i, r in enumerate(results):
            if not _should_queue_refresh(r):
                continue
            reason = _queue_reason(r)
            r["refresh_queued"] = True
            r["refresh_queue_reason"] = reason
            if "refresh_queued" not in (r.get("reasons") or []):
                r.setdefault("reasons", []).append("refresh_queued")
            p = Path(r.get("path") or r.get("auth_file") or "")
            if not p.is_file():
                continue
            key = str(p)
            if key in queue_idx:
                continue
            queue_idx[key] = i
            queue_paths.append(p)

        if queue_paths:
            if not args.json:
                print(
                    f"phase2 refresh queue: {len(queue_paths)} accounts, "
                    f"workers={min(workers, len(queue_paths))}",
                    flush=True,
                )
            refreshed = _run_parallel_checks(
                queue_paths,
                workers=workers,
                timeout=args.timeout,
                check_models=check_models,
                check_billing=check_billing,
                check_plan=check_plan,
                refresh=True,  # allow refresh now that probe pass finished
            )
            for r2 in refreshed:
                key = str(r2.get("path") or "")
                idx = queue_idx.get(key)
                if idx is None:
                    continue
                # Preserve that it came from the deferred queue.
                r2["refresh_queued"] = True
                r2["refresh_queue_reason"] = results[idx].get("refresh_queue_reason")
                if r2.get("refreshed") and "access_token_refreshed" not in (
                    r2.get("reasons") or []
                ):
                    r2.setdefault("reasons", []).insert(0, "access_token_refreshed")
                results[idx] = r2

    if args.only_usable:
        results = [r for r in results if r.get("usable")]
    elif args.only_unusable:
        results = [r for r in results if not r.get("usable")]

    if args.json:
        # never dump tokens
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print_table(results)

    # exit 0 if any usable, 1 if all unusable, 2 if none checked
    if not results:
        return 2
    return 0 if any(r.get("usable") for r in results) else 1
