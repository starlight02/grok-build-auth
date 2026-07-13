#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone Grok Build account checker.

Checks whether local auth JSON files can actually call the Build/CLI free
endpoint. Does NOT print tokens / passwords / SSO.

Accepts either:
  - CLIProxyAPI auth files (access_token, base_url, headers, email, disabled)
  - accounts_output bundles (oauth_access_token / cliproxyapi_auth path)

Examples:

  python check_accounts.py cliproxyapi_auth/
  python check_accounts.py accounts_output/account_*.json
  python check_accounts.py cliproxyapi_auth/user@example.com.json --json
  HTTPS_PROXY=http://127.0.0.1:7890 python check_accounts.py cliproxyapi_auth/
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
DEFAULT_HEADERS = {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": "0.2.93",
    "x-grok-client-identifier": "grok-shell",
}
PROBE_MODEL = "grok-4.5"
PROBE_BODY = {
    "model": PROBE_MODEL,
    "input": "Reply exactly: OK",
    "max_output_tokens": 8,
}


def mask_email(value: str) -> str:
    value = (value or "").strip()
    if "@" not in value:
        return value or "(unknown)"
    name, domain = value.split("@", 1)
    if len(name) <= 2:
        masked = name[:1] + "*"
    else:
        masked = name[:2] + "***" + name[-1:]
    return f"{masked}@{domain}"


def b64url_json(segment: str) -> dict[str, Any] | None:
    try:
        pad = "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(segment + pad)
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def jwt_meta(token: str, now: int | None = None) -> dict[str, Any]:
    now = int(time.time()) if now is None else now
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {"valid_jwt": False, "expired": True, "error": "not_jwt"}
    payload = b64url_json(parts[1])
    if not payload:
        return {"valid_jwt": False, "expired": True, "error": "bad_payload"}
    exp = payload.get("exp")
    iat = payload.get("iat")
    try:
        exp_i = int(exp) if exp is not None else None
    except (TypeError, ValueError):
        exp_i = None
    try:
        iat_i = int(iat) if iat is not None else None
    except (TypeError, ValueError):
        iat_i = None
    expired = bool(exp_i is not None and exp_i < now)
    return {
        "valid_jwt": True,
        "expired": expired,
        "exp": exp_i,
        "iat": iat_i,
        "ttl_sec": (exp_i - now) if exp_i is not None else None,
        "scope": payload.get("scope") or payload.get("scp"),
        "aud": payload.get("aud"),
        "iss": payload.get("iss"),
    }


def header_int(headers: Any, name: str) -> int | None:
    raw = headers.get(name) if headers is not None else None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_exhausted(body: str) -> tuple[int | None, int | None]:
    m = re.search(
        r"tokens\s*\(actual/limit\)\s*:\s*(\d+)\s*/\s*(\d+)", body or "", re.I
    )
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

    try:
        data = resp.json()
    except Exception:
        if body_text:
            out["error"] = body_text[:300]
        return out

    if isinstance(data, dict):
        if model := data.get("model"):
            out["model"] = model
        usage = data.get("usage")
        if isinstance(usage, dict):
            total = usage.get("total_tokens") or usage.get("totalTokens")
            if total is not None:
                out["probe_total_tokens"] = total
        err = data.get("error")
        if err and resp.status_code >= 400:
            out["error"] = str(err)[:300]
    return out


def resolve_auth_record(path: Path) -> dict[str, Any]:
    """Normalize cliproxyapi_auth / accounts_output into one auth record."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")

    # accounts_output bundle → prefer linked cliproxyapi auth file
    linked = str(data.get("cliproxyapi_auth") or "").strip()
    if linked:
        linked_path = Path(linked)
        if not linked_path.is_absolute():
            linked_path = (path.parent / linked_path).resolve()
        if linked_path.is_file():
            linked_data = json.loads(linked_path.read_text(encoding="utf-8"))
            if isinstance(linked_data, dict) and (
                linked_data.get("access_token") or linked_data.get("token")
            ):
                linked_data = dict(linked_data)
                linked_data.setdefault("email", data.get("email"))
                linked_data["_source"] = str(path)
                linked_data["_auth_file"] = str(linked_path)
                return linked_data

    token = (
        str(data.get("access_token") or "").strip()
        or str(data.get("oauth_access_token") or "").strip()
        or str(data.get("token") or "").strip()
    )
    base_url = (
        str(data.get("base_url") or "").strip()
        or str(data.get("build_base_url") or "").strip()
        or DEFAULT_BASE_URL
    )
    headers = data.get("headers") if isinstance(data.get("headers"), dict) else {}
    return {
        "email": data.get("email") or "",
        "access_token": token,
        "refresh_token": data.get("refresh_token")
        or data.get("oauth_refresh_token")
        or "",
        "base_url": base_url,
        "headers": headers,
        "disabled": bool(data.get("disabled")),
        "_source": str(path),
        "_auth_file": str(path),
    }


def build_headers(auth: dict[str, Any]) -> dict[str, str]:
    token = str(auth.get("access_token") or "").strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "grok-cli/0.2.93",
        **DEFAULT_HEADERS,
    }
    extra = auth.get("headers")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip():
                headers[k] = v
    return headers


def normalize_base_url(base_url: str) -> str:
    base = (base_url or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    # Build free quota is on cli-chat-proxy, not paid api.x.ai
    if "api.x.ai" in base:
        return DEFAULT_BASE_URL
    return base.rstrip("/")


def check_one(
    path: Path,
    *,
    timeout: float = 45.0,
    check_models: bool = True,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    out: dict[str, Any] = {
        "file": path.name,
        "path": str(path),
        "usable": False,
        "reasons": [],
    }
    try:
        auth = resolve_auth_record(path)
    except Exception as exc:
        out["error"] = f"load_failed: {type(exc).__name__}: {exc}"
        out["reasons"] = ["load_failed"]
        return out

    email = str(auth.get("email") or "")
    token = str(auth.get("access_token") or "").strip()
    out["email"] = email
    out["email_masked"] = mask_email(email)
    out["disabled"] = bool(auth.get("disabled"))
    out["auth_file"] = auth.get("_auth_file") or str(path)

    if out["disabled"]:
        out["reasons"].append("disabled")
    if not token:
        out["reasons"].append("missing_access_token")
        out["error"] = "missing access_token / oauth_access_token"
        return out

    meta = jwt_meta(token, now=now)
    out["jwt"] = {
        "valid": meta.get("valid_jwt"),
        "expired": meta.get("expired"),
        "ttl_sec": meta.get("ttl_sec"),
        "scope": meta.get("scope"),
        "iss": meta.get("iss"),
    }
    if meta.get("expired"):
        out["reasons"].append("access_token_expired")

    base_url = normalize_base_url(str(auth.get("base_url") or DEFAULT_BASE_URL))
    headers = build_headers(auth)
    sess = session or requests.Session()

    # 1) responses probe (authoritative for Build free quota)
    responses_url = urljoin(base_url.rstrip("/") + "/", "responses")
    out["responses_url"] = responses_url
    try:
        resp = sess.post(
            responses_url, headers=headers, json=PROBE_BODY, timeout=timeout
        )
        summary = summarize_response(resp)
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
        if summary.get("error"):
            out["probe_error"] = summary["error"]
    except Exception as exc:
        out["status"] = None
        out["probe_error"] = f"{type(exc).__name__}: {exc}"
        out["reasons"].append("probe_network_error")

    # 2) optional /models smoke
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

    # verdict
    status = out.get("status")
    if out["disabled"]:
        out["usable"] = False
    elif meta.get("expired"):
        out["usable"] = False
    elif status == 200:
        out["usable"] = True
        out["reasons"].append("responses_ok")
        if out.get("remaining_tokens") is not None:
            out["reasons"].append(f"remaining_tokens={out['remaining_tokens']}")
    elif status == 429:
        out["usable"] = False
        out["reasons"].append("quota_exhausted_or_rate_limited")
        if out.get("code"):
            out["reasons"].append(str(out["code"]))
    elif status in (401, 403):
        out["usable"] = False
        out["reasons"].append(f"auth_rejected_{status}")
    elif status is None:
        out["usable"] = False
        if "probe_network_error" not in out["reasons"]:
            out["reasons"].append("probe_failed")
    else:
        out["usable"] = False
        out["reasons"].append(f"http_{status}")
        if out.get("probe_error"):
            out["reasons"].append(str(out["probe_error"])[:120])

    if not out["reasons"]:
        out["reasons"] = ["unknown"]
    return out


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
            if "access_token" not in head and "oauth_access_token" not in head and "cliproxyapi_auth" not in head:
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
    print(f"checked={len(results)} usable={usable_n} unusable={len(results) - usable_n}")
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
        print(
            f"[{flag}] {label}  status={status}  tokens={rem}/{lim}  "
            f"req={req}/{req_lim}  jwt_ttl={ttl}"
        )
        print(f"       file={r.get('file')}  reasons={reasons}")
        if r.get("probe_error") and not r.get("usable"):
            print(f"       probe_error={r['probe_error'][:160]}")
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
        "--no-models",
        action="store_true",
        help="Skip GET /v1/models smoke check",
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

    session = requests.Session()
    results: list[dict[str, Any]] = []
    for path in paths:
        try:
            # pre-filter disabled without network if requested
            if not args.include_disabled:
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict) and raw.get("disabled") is True:
                        continue
                except Exception:
                    pass
            results.append(
                check_one(
                    path,
                    timeout=args.timeout,
                    check_models=not args.no_models,
                    session=session,
                )
            )
        except Exception as exc:
            results.append(
                {
                    "file": path.name,
                    "path": str(path),
                    "usable": False,
                    "reasons": ["check_exception"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

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


if __name__ == "__main__":
    raise SystemExit(main())
