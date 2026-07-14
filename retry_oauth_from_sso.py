#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Retry Build OAuth for SSO records that never got cliproxyapi_auth JSON."""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from xconsole_client.xai_oauth import complete_build_oauth, default_cliproxyapi_auth_dir


def load_missing(list_path: Path, sso_dir: Path, auth_dir: Path) -> list[dict]:
    if list_path.is_file():
        return json.loads(list_path.read_text())

    auth_emails: set[str] = set()
    for p in auth_dir.glob("*.json"):
        try:
            d = json.loads(p.read_text())
            e = (d.get("email") or p.stem).lower().strip()
            if e:
                auth_emails.add(e)
        except Exception:
            auth_emails.add(p.stem.lower())

    missing: list[dict] = []
    for p in sorted(sso_dir.glob("*.json"), key=lambda x: x.stat().st_mtime):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        e = (d.get("email") or "").lower().strip()
        if not e or e in auth_emails:
            continue
        sso = d.get("sso") or d.get("token") or ""
        password = d.get("password") or ""
        if not sso or not password:
            continue
        missing.append(
            {
                "file": str(p),
                "email": e,
                "password": password,
                "sso": sso,
            }
        )
    return missing


def retry_one(item: dict, auth_dir: Path) -> dict:
    email = item["email"]
    password = item["password"]
    sso = item["sso"]
    t0 = time.time()
    try:
        oauth = complete_build_oauth(
            email,
            password,
            cliproxyapi_auth_dir=str(auth_dir),
            protocol=True,
            debug=False,
            session_cookies={"sso": sso, "sso-rw": sso},
        )
        cand = auth_dir / f"{email}.json"
        path = str(cand) if cand.exists() else ""
        for attr in ("cliproxyapi_auth_path", "cliproxyapi_path", "auth_path"):
            val = getattr(oauth, attr, None)
            if val:
                path = str(val)
                break
        access = (getattr(oauth, "access_token", None) or "")[:28]
        return {
            "email": email,
            "ok": True,
            "path": path,
            "access": access,
            "elapsed": round(time.time() - t0, 1),
            "err": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "email": email,
            "ok": False,
            "path": "",
            "access": "",
            "elapsed": round(time.time() - t0, 1),
            "err": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    root = Path(__file__).resolve().parent
    list_path = root / "oauth_retry_list.json"
    sso_dir = root / "sso_output"
    auth_dir = default_cliproxyapi_auth_dir()
    items = load_missing(list_path, sso_dir, auth_dir)
    if not items:
        print("nothing to retry (no SSO-without-auth records)")
        return 0

    workers = 2
    print(f"retry OAuth for {len(items)} accounts, workers={workers} → {auth_dir}", flush=True)
    ok = 0
    fail = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(retry_one, it, auth_dir) for it in items]
        for fut in as_completed(futs):
            r = fut.result()
            if r["ok"]:
                ok += 1
                print(
                    f"OK  {r['email']}  {r['elapsed']}s  {r['path'] or r['access']}",
                    flush=True,
                )
            else:
                fail += 1
                print(f"FAIL {r['email']}  {r['elapsed']}s  {r['err']}", flush=True)
    print(f"Done OK={ok} FAIL={fail}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
