#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Retry Build/CPA auth for SSO records that never got cliproxyapi_auth JSON.

Uses pure HTTP OAuth Device Flow (same approach as grok_reg/sso2auth.py):
SSO cookie → device/code → verify/approve → token poll → CPA JSON.

No browser. No Playwright. No Turnstile.
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from xconsole_client.sso2auth import mint_cpa_from_sso  # noqa: E402
from xconsole_client.xai_oauth import default_cliproxyapi_auth_dir  # noqa: E402


def load_missing(list_path: Path, sso_dir: Path, auth_dir: Path) -> list[dict]:
    """Load SSO records that do not yet have a matching auth JSON."""
    auth_emails: set[str] = set()
    for p in auth_dir.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            e = (d.get("email") or p.stem).strip().lower()
            if e:
                auth_emails.add(e)
            # also index stem without xai- prefix
            stem = p.stem.lower()
            if stem.startswith("xai-"):
                auth_emails.add(stem[4:])
            auth_emails.add(stem)
        except Exception:
            auth_emails.add(p.stem.lower())

    items: list[dict] = []
    seen: set[str] = set()

    # Optional explicit list: [{email, password?, sso}]
    if list_path.is_file():
        try:
            raw = json.loads(list_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for it in raw:
                    if not isinstance(it, dict):
                        continue
                    email = str(it.get("email") or "").strip()
                    sso = str(it.get("sso") or it.get("token") or "").strip()
                    if not email or not sso:
                        continue
                    if email.lower() in auth_emails or email.lower() in seen:
                        continue
                    seen.add(email.lower())
                    items.append(
                        {
                            "email": email,
                            "password": str(it.get("password") or ""),
                            "sso": sso,
                        }
                    )
        except Exception as exc:
            print(f"warn: could not read {list_path}: {exc}", flush=True)

    for p in sorted(sso_dir.glob("*.json"), key=lambda x: x.stat().st_mtime):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        email = str(d.get("email") or "").strip()
        sso = str(d.get("sso") or d.get("token") or "").strip()
        password = str(d.get("password") or "")
        if not email or not sso:
            continue
        el = email.lower()
        if el in auth_emails or el in seen:
            continue
        seen.add(el)
        items.append({"email": email, "password": password, "sso": sso})

    return items


def retry_one(item: dict, auth_dir: Path, proxy: str) -> dict:
    email = item["email"]
    sso = item["sso"]
    t0 = time.time()
    logs: list[str] = []

    def _log(msg: str) -> None:
        logs.append(msg)

    try:
        r = mint_cpa_from_sso(
            sso,
            email=email,
            auth_dir=auth_dir,
            proxy=proxy,
            skip_existing=True,
            log=_log,
        )
        elapsed = round(time.time() - t0, 1)
        if r.get("ok"):
            return {
                "email": email,
                "ok": True,
                "skipped": bool(r.get("skipped")),
                "path": str(r.get("path") or ""),
                "elapsed": elapsed,
                "err": None,
                "detail": " | ".join(logs[-3:]),
            }
        return {
            "email": email,
            "ok": False,
            "skipped": False,
            "path": "",
            "elapsed": elapsed,
            "err": str(r.get("error") or "mint_failed"),
            "detail": " | ".join(logs[-3:]),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "email": email,
            "ok": False,
            "skipped": False,
            "path": "",
            "elapsed": round(time.time() - t0, 1),
            "err": f"{type(exc).__name__}: {exc}",
            "detail": " | ".join(logs[-3:]),
        }


def main() -> int:
    root = Path(__file__).resolve().parent
    list_path = root / "oauth_retry_list.json"
    sso_dir = root / "sso_output"
    auth_dir = default_cliproxyapi_auth_dir()
    proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("ALL_PROXY")
        or ""
    ).strip()

    items = load_missing(list_path, sso_dir, auth_dir)
    if not items:
        print("nothing to retry (no SSO-without-auth records)", flush=True)
        return 0

    raw_workers = (os.environ.get("SSO2AUTH_WORKERS") or "2").strip()
    try:
        workers = max(1, min(int(raw_workers), 8))
    except ValueError:
        workers = 2

    print(
        f"retry SSO→CPA (device flow, no browser) for {len(items)} accounts, "
        f"workers={workers} → {auth_dir}",
        flush=True,
    )
    ok = 0
    skip = 0
    fail = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(retry_one, it, auth_dir, proxy) for it in items]
        for fut in as_completed(futs):
            r = fut.result()
            if r["ok"]:
                if r.get("skipped"):
                    skip += 1
                    tag = "SKIP"
                else:
                    ok += 1
                    tag = "OK"
                print(
                    f"{tag}  {r['email']}  {r['elapsed']}s  {r['path'] or ''}",
                    flush=True,
                )
            else:
                fail += 1
                print(
                    f"FAIL {r['email']}  {r['elapsed']}s  {r['err']}"
                    + (f"  ({r['detail']})" if r.get("detail") else ""),
                    flush=True,
                )
    print(f"Done OK={ok} SKIP={skip} FAIL={fail}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
