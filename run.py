#!/usr/bin/env python3
"""grok-build-auth — 一键注册 x.ai 账号 + SSO + Grok Build OAuth（CLIProxyAPI 可用）

流程:
  1) 协议注册（邮箱验证 + Turnstile + create_account）
  2) 提取 SSO
  3) xAI OAuth PKCE（含 grok-cli:access）
  4) 导出 CLIProxyAPI auth：cli-chat-proxy.grok.com + grok-cli headers
     → 可直接用 grok-4.5 走 Build/CLI 编码通道

环境变量（按需设置）:
    TURNSTILE_SOLVER       auto|drission|browser (default auto → DrissionPage+turnstilePatch)
    TURNSTILE_HEADLESS     drission 默认 0（有头自动点）；browser/playwright 默认 1
    TURNSTILE_BROWSER_CHANNEL  playwright only (chrome auto when available)
    TURNSTILE_INTERACTIVE  1=手动点 Turnstile（仅 playwright；强制有头）
    TURNSTILE_BROWSER_REUSE    1=keep per-thread browser warm (default 1)
    TURNSTILE_TIMEOUT      hard wall-clock seconds per Turnstile solve (default 30)
    TURNSTILE_PARALLEL     concurrent Turnstile mints (default 2; Drission thread-local Chrome)
    TEMPMAIL_API_KEY       Optional Tempmail.lol Plus/Ultra key (free tier works without)
    MAIL_CODE_TIMEOUT     Seconds to wait for verification code before rotating inbox (default 30)
    MAIL_MAX_ATTEMPTS     Fresh inboxes to try when mail is silent (default 3)
    CLOUDFLARE_API_TOKEN   Cloudflare API token (alias_mail 邮箱后端)
    CLIPROXYAPI_AUTH_DIR   CLIProxyAPI data/auth 目录（可选）
    HTTPS_PROXY / HTTP_PROXY  代理（OAuth 换 token / browser）

CLI 常用:
    -n / -t                账号数 / 并发（注册+协议 OAuth 并发；Turnstile 默认 2 并行）
    --check-quota          OAuth 后探测额度，无额度移到 failed 目录（默认关）
    --failed-auth-dir      无额度 auth 目录（默认 <auth-dir>_failed）
    --check-quota-timeout  单号探测超时秒数
"""
from __future__ import annotations

import sys
import os
import uuid
import json
import base64
import time
import threading
import argparse
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

# Load local .env if present (optional dependency).
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except Exception:
    pass

from xconsole_client import XConsoleAuthClient, config as C
from xconsole_client.solver import resolve_turnstile_solver
from xconsole_client.xai_oauth import (
    CLIPROXYAPI_GROK_BASE_URL,
    complete_build_oauth,
    default_cliproxyapi_auth_dir,
)
from xconsole_client.oauth_protocol import extract_cookies_from_auth_client

# -- secrets from environment only ---------------------------------------
TEMPMAIL_KEY = os.environ.get("TEMPMAIL_API_KEY", "")
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""


def _mail_code_timeout() -> float:
    """Seconds to wait for a verification code before rotating the inbox."""
    raw = (os.environ.get("MAIL_CODE_TIMEOUT") or "30").strip()
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 30.0


def _mail_max_attempts() -> int:
    """How many fresh inboxes to try when mail is slow/silent."""
    raw = (os.environ.get("MAIL_MAX_ATTEMPTS") or "3").strip()
    try:
        return max(1, min(int(raw), 10))
    except ValueError:
        return 3

_results_lock = threading.Lock()
_cf_lock = threading.Lock()
# Only Playwright/browser OAuth needs serialization (Playwright sync is TLS-bound
# and multi-headed Chrome is heavy). Pure HTTP protocol OAuth is concurrent-safe.
_oauth_browser_lock = threading.Lock()
# Turnstile mint concurrency. Default 2: Drission uses thread-local Chrome so
# two slots do not share cookies/session. Higher values open more Chromes.
def _turnstile_parallel() -> int:
    raw = (os.environ.get("TURNSTILE_PARALLEL") or "2").strip()
    try:
        return max(1, min(int(raw), 8))
    except ValueError:
        return 2


_turnstile_lock = threading.Semaphore(_turnstile_parallel())
_results: list[dict] = []
_done = 0
_total = 0
_t0 = 0.0


def _log(i: int, msg: str):
    elapsed = time.time() - _t0
    bar = f"[{_done}/{_total}]" if _total > 1 else ""
    print(f"  {bar} [#{i}] {msg}  ({elapsed:.0f}s)")


def _make_email_provider(backend: str):
    """Return (email, receiver) — receiver has .wait_for_code(timeout)."""
    if backend == "tempmail":
        from xconsole_client.tempmail_transport import TempmailInbox
        # Free tier: no API key. Optional TEMPMAIL_API_KEY for Plus/Ultra rate limits.
        inbox = TempmailInbox(api_key=TEMPMAIL_KEY or "", prefix="xai", debug=False)
        email = inbox.create()
        return email, inbox
    elif backend == "cloudflare":
        from xconsole_client.mailbox import AliasMailAccount, AliasMailCodeReceiver
        with _cf_lock:
            cf = AliasMailAccount.ensure_cf()
            alloc = AliasMailAccount(cf)
            address = alloc.create(prefix="xai")
        receiver = AliasMailCodeReceiver(cf, address=address, timeout=120, interval=3, since_now=True)
        return address, receiver
    else:
        raise ValueError(f"unknown email backend: {backend}")


def _save_account_bundle(result: dict, output_dir: Path) -> Path:
    """Persist a combined signup+oauth record for later tooling (opt-in)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    email = str(result.get("email") or "unknown")
    safe = "".join(ch if ch.isalnum() or ch in "._-@" else "_" for ch in email) or "unknown"
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = output_dir / f"account_{safe}_{ts}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _default_failed_auth_dir(auth_dir: str | Path) -> Path:
    """Sibling folder of the auth dir, e.g. cliproxyapi_auth → cliproxyapi_auth_failed."""
    p = Path(auth_dir)
    return p.parent / f"{p.name}_failed"


def _move_auth_to_failed(src: Path, failed_dir: Path) -> Path:
    """Move an unusable auth JSON into the failed folder (unique name)."""
    failed_dir.mkdir(parents=True, exist_ok=True)
    dest = failed_dir / src.name
    if dest.exists():
        stem, suf = src.stem, src.suffix
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        dest = failed_dir / f"{stem}_{ts}{suf}"
    return Path(shutil.move(str(src), str(dest)))


def _check_and_gate_auth(
    auth_path: Path,
    *,
    failed_dir: Path,
    timeout: float = 45.0,
    index: int = 0,
) -> dict:
    """Probe Build quota; keep usable files, move unusable ones to failed_dir.

    Uses the same ``check_accounts.check_one`` as the CLI. Fresh OAuth tokens
    sometimes return a transient 401/403 on the first /responses hit; wait briefly
    and retry those statuses before moving the file to failed_dir.

    Returns a small dict for the result record:
      usable, remaining_tokens, status, reasons, path (final location), error?
    """
    # Lazy import so --check-quota-off path never pays the cost.
    from check_accounts import check_one

    # Propagation delay after OAuth write (observed: immediate probe → 403,
    # re-check a few seconds later → 200 + full free quota).
    time.sleep(2.0)

    probe: dict = {}
    attempts = 3
    for attempt in range(1, attempts + 1):
        probe = check_one(auth_path, timeout=timeout, check_models=False)
        if probe.get("usable"):
            break
        status = probe.get("status")
        # Only retry auth-rejection / network flakiness — not real 429 exhaustion.
        if status not in (401, 403, None) or attempt >= attempts:
            break
        wait_s = 2.0 * attempt  # 2s, 4s
        _log(
            index,
            f"quota probe attempt {attempt}/{attempts} status={status}; "
            f"retry in {wait_s:.0f}s",
        )
        time.sleep(wait_s)

    usable = bool(probe.get("usable"))
    rem = probe.get("remaining_tokens")
    status = probe.get("status")
    reasons = ",".join(probe.get("reasons") or []) or "?"

    if usable:
        _log(
            index,
            f"quota OK  status={status} remaining_tokens={rem if rem is not None else '--'}  "
            f"({reasons})",
        )
        return {
            "usable": True,
            "remaining_tokens": rem,
            "status": status,
            "reasons": reasons,
            "path": str(auth_path),
            "error": None,
        }

    dest = _move_auth_to_failed(auth_path, failed_dir)
    err = f"quota unusable: status={status} reasons={reasons}"
    _log(index, f"quota FAIL → moved to {dest.name}  ({err})")
    return {
        "usable": False,
        "remaining_tokens": rem,
        "status": status,
        "reasons": reasons,
        "path": str(dest),
        "error": err,
    }


def register_one(
    index: int,
    email_backend: str = "tempmail",
    *,
    do_oauth: bool = True,
    oauth_headless: bool = True,
    oauth_timeout: float = 180.0,
    oauth_interactive_fallback: bool = False,
    oauth_protocol: bool = True,
    oauth_debug: bool = False,
    cliproxyapi_auth_dir: Optional[str | Path] = None,
    cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
    accounts_output_dir: Optional[str | Path] = None,
    check_quota: bool = False,
    failed_auth_dir: Optional[str | Path] = None,
    check_quota_timeout: float = 45.0,
) -> dict:
    """Run signup (+ optional Build OAuth export). Thread-safe."""
    # Turnstile: local browser only.
    try:
        turnstile_solver = resolve_turnstile_solver(
            proxy=PROXY,
            debug=oauth_debug,
        )
    except Exception as exc:
        return {
            "email": "",
            "password": "",
            "sso": None,
            "oauth_access_token": None,
            "cliproxyapi_auth": None,
            "error": f"Turnstile solver unavailable: {exc}",
        }

    # Per-client signup_url — never mutate global C.SIGNUP_URL under concurrency.
    c = XConsoleAuthClient(debug=False, signup_url=SIGNUP_URL)
    email = ""
    password = ""
    sso = None

    try:
        # 1. warm-up + scrape
        c.visit_home()
        c.load_signup_page()
        _log(index, "cookie + scrape OK")

        # 2. email + Turnstile.
        # Design (fast under -t N):
        #   - Turnstile runs on THIS worker thread (Playwright sync is TLS-bound;
        #     side-thread solves deadlock / hang under concurrency).
        #   - Mail poll runs on a side thread and rotates inbox every
        #     MAIL_CODE_TIMEOUT (default 30s) without blocking the solve.
        password = f"Pw{os.urandom(6).hex()}!a#A"
        mail_timeout = _mail_code_timeout()
        mail_attempts = _mail_max_attempts()

        code_box: dict = {"code": None, "email": "", "err": None, "tries": 0}
        stop_mail = threading.Event()

        def _mail_loop() -> None:
            last_err: Optional[BaseException] = None
            for attempt in range(1, mail_attempts + 1):
                if stop_mail.is_set():
                    return
                try:
                    addr, receiver = _make_email_provider(email_backend)
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    code_box["err"] = exc
                    _log(index, f"mail create failed ({exc})")
                    return
                code_box["email"] = addr
                code_box["tries"] = attempt
                tag = f" (try {attempt}/{mail_attempts})" if mail_attempts > 1 else ""
                _log(index, f"email: {addr}{tag}")
                try:
                    c.create_email_validation_code(addr)
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    code_box["err"] = exc
                    _log(index, f"send code failed ({exc})")
                    if attempt >= mail_attempts:
                        return
                    continue
                try:
                    code_box["code"] = receiver.wait_for_code(timeout=mail_timeout)
                    code_box["err"] = None
                    return
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    code_box["err"] = exc
                    if attempt >= mail_attempts or stop_mail.is_set():
                        return
                    _log(
                        index,
                        f"mail silent {mail_timeout:.0f}s; rotate inbox",
                    )
            if last_err is not None:
                code_box["err"] = last_err

        mail_thread = threading.Thread(
            target=_mail_loop, name=f"mail-{index}", daemon=True
        )
        mail_thread.start()

        ts_t0 = time.time()
        try:
            # Bound concurrent Turnstile mints (default 2; see TURNSTILE_PARALLEL).
            with _turnstile_lock:
                turnstile = turnstile_solver.solve_turnstile(
                    website_url=SIGNUP_URL,
                    website_key=C.TURNSTILE_SITEKEY,
                    premium=True,
                )
        except Exception as ts_exc:
            # Let mail finish its current attempt, then surface.
            mail_thread.join(timeout=mail_timeout + 5)
            stop_mail.set()
            if code_box.get("code"):
                _log(index, f"Turnstile first try failed ({ts_exc}); retry after mail")
                with _turnstile_lock:
                    turnstile = turnstile_solver.solve_turnstile(
                        website_url=SIGNUP_URL,
                        website_key=C.TURNSTILE_SITEKEY,
                        premium=True,
                    )
                ts_t0 = time.time()
            else:
                raise

        # Wait for mail (started in parallel). Cap join so we never hang forever.
        mail_budget = mail_attempts * mail_timeout + 10
        mail_thread.join(timeout=max(5.0, mail_budget - (time.time() - ts_t0)))
        stop_mail.set()

        code = code_box.get("code")
        email = code_box.get("email") or email
        if not code:
            err = code_box.get("err")
            raise err or RuntimeError(
                f"email code timeout after {mail_attempts} inbox(es) "
                f"× {mail_timeout:.0f}s"
            )
        _log(index, f"code: {code}")
        c.verify_email_validation_code(email, code)
        c.validate_password(email, password)
        _log(index, "email verified")

        # Token TTL safety: if mint finished long before mail, re-solve.
        age = time.time() - ts_t0
        if age > 240:
            _log(index, f"Turnstile token age {age:.0f}s; re-solve")
            with _turnstile_lock:
                turnstile = turnstile_solver.solve_turnstile(
                    website_url=SIGNUP_URL,
                    website_key=C.TURNSTILE_SITEKEY,
                    premium=True,
                )
        _log(
            index,
            f"Turnstile {len(turnstile)} chars via {type(turnstile_solver).__name__}",
        )

        # 4. create account
        res = c.create_account(
            email=email, given_name="Test", family_name="User",
            password=password, email_validation_code=code,
            turnstile_token=turnstile, castle_request_token="",
            conversion_id=str(uuid.uuid4()),
        )
        if not res.ok:
            # One free retry with a fresh token (expired / race)
            _log(index, f"create_account HTTP {res.http_status}; retry Turnstile once")
            with _turnstile_lock:
                turnstile = turnstile_solver.solve_turnstile(
                    website_url=SIGNUP_URL,
                    website_key=C.TURNSTILE_SITEKEY,
                    premium=True,
                )
            res = c.create_account(
                email=email, given_name="Test", family_name="User",
                password=password, email_validation_code=code,
                turnstile_token=turnstile, castle_request_token="",
                conversion_id=str(uuid.uuid4()),
            )
        if not res.ok:
            _log(index, f"FAIL create_account HTTP {res.http_status}")
            return {
                "email": email,
                "password": password,
                "sso": None,
                "oauth_access_token": None,
                "cliproxyapi_auth": None,
                "error": f"HTTP {res.http_status}",
            }
        _log(index, "account created")

        # 5. SSO (retries + RSC chain + grok.com fallback inside client)
        sso = c.fetch_sso_token(email=email, password=password, save=True, retries=3)
        if not sso:
            _log(index, "FAIL SSO extraction")
            return {
                "email": email,
                "password": password,
                "sso": None,
                "oauth_access_token": None,
                "cliproxyapi_auth": None,
                "error": "SSO failed",
            }
        payload = json.loads(base64.urlsafe_b64decode(sso.split(".")[1] + "=="))
        _log(index, f"SSO saved  session_id={payload.get('session_id', '?')[:12]}...")

        result = {
            "email": email,
            "password": password,
            "sso": sso,
            "oauth_access_token": None,
            "oauth_refresh_token": None,
            "oauth_record": None,
            "cliproxyapi_auth": None,
            "build_base_url": cliproxyapi_base_url,
            "error": None,
        }

        # 6. OAuth → CLIProxyAPI Grok Build path (coding-ready)
        if do_oauth:
            auth_dir = Path(cliproxyapi_auth_dir) if cliproxyapi_auth_dir else default_cliproxyapi_auth_dir()
            # Reuse signup session cookies so OAuth can skip password login when possible.
            session_cookies = extract_cookies_from_auth_client(c)
            # Grok SSO JWT (from fetch_sso_token) also works as accounts.x.ai `sso` cookie.
            if sso:
                session_cookies = dict(session_cookies or {})
                session_cookies.setdefault("sso", sso)
            _log(index, f"OAuth Build path → {auth_dir}  (cookies={len(session_cookies)})")
            # Protocol OAuth is pure HTTP → concurrent. Only serialize Playwright
            # browser fallback (Playwright sync + multi-Chrome is heavy/TLS-bound).
            oauth = None
            protocol_err: Optional[BaseException] = None
            if oauth_protocol:
                try:
                    from xconsole_client.oauth_protocol import login_with_protocol
                    oauth = login_with_protocol(
                        email,
                        password,
                        proxy=PROXY,
                        debug=oauth_debug,
                        cliproxyapi_auth_dir=str(auth_dir),
                        cliproxyapi_base_url=cliproxyapi_base_url,
                        session_cookies=session_cookies,
                        auth_client=c,
                    )
                except Exception as exc:  # noqa: BLE001
                    protocol_err = exc
                    _log(index, f"protocol OAuth failed ({exc}); browser fallback")
            if oauth is None:
                with _oauth_browser_lock:
                    oauth = complete_build_oauth(
                        email,
                        password,
                        cliproxyapi_auth_dir=auth_dir,
                        cliproxyapi_base_url=cliproxyapi_base_url,
                        headless=oauth_headless,
                        timeout=oauth_timeout,
                        proxy=PROXY,
                        interactive_fallback=oauth_interactive_fallback,
                        # Skip re-trying protocol under the lock if it already failed.
                        protocol=False if protocol_err is not None else oauth_protocol,
                        debug=oauth_debug,
                        session_cookies=session_cookies,
                        auth_client=c,
                    )
            result["oauth_access_token"] = oauth.access_token
            result["oauth_refresh_token"] = oauth.refresh_token
            result["oauth_record"] = str(oauth.path) if oauth.path else None
            result["cliproxyapi_auth"] = str(oauth.cliproxyapi_path) if oauth.cliproxyapi_path else None
            _log(
                index,
                f"Build OAuth OK  access={oauth.access_token[:20]}...  "
                f"cliproxy={oauth.cliproxyapi_path.name if oauth.cliproxyapi_path else '?'}",
            )

            # Optional: only keep auth files that still have Build free quota.
            if check_quota and oauth.cliproxyapi_path:
                fail_dir = (
                    Path(failed_auth_dir)
                    if failed_auth_dir
                    else _default_failed_auth_dir(auth_dir)
                )
                gate = _check_and_gate_auth(
                    Path(oauth.cliproxyapi_path),
                    failed_dir=fail_dir,
                    timeout=check_quota_timeout,
                    index=index,
                )
                result["quota_usable"] = gate["usable"]
                result["quota_remaining_tokens"] = gate.get("remaining_tokens")
                result["quota_status"] = gate.get("status")
                result["quota_reasons"] = gate.get("reasons")
                if gate["usable"]:
                    result["cliproxyapi_auth"] = gate["path"]
                else:
                    # Unusable → removed from live auth dir; surface as failure.
                    result["cliproxyapi_auth_failed"] = gate["path"]
                    result["cliproxyapi_auth"] = None
                    result["error"] = gate.get("error") or "quota unusable"
        else:
            _log(index, "OAuth skipped (--no-oauth)")

        if accounts_output_dir:
            bundle = _save_account_bundle(result, Path(accounts_output_dir))
            result["account_bundle"] = str(bundle)

        return result

    except Exception as e:
        _log(index, f"ERROR: {e}")
        return {
            "email": email,
            "password": password,
            "sso": sso,
            "oauth_access_token": None,
            "cliproxyapi_auth": None,
            "error": str(e),
        }
    finally:
        c.close()
        with _results_lock:
            global _done
            _done += 1


def main():
    global _total, _t0
    default_auth = str(default_cliproxyapi_auth_dir())
    p = argparse.ArgumentParser(
        description="grok-build-auth: x.ai register + SSO + Grok Build OAuth (CLIProxyAPI-ready)",
    )
    p.add_argument("-n", "--count", type=int, default=1, help="账号数量")
    p.add_argument("-t", "--threads", type=int, default=1, help="并发线程数（注册+协议 OAuth 并发；浏览器 OAuth 回退串行）")
    p.add_argument(
        "-e", "--email",
        choices=["tempmail", "cloudflare"],
        default="tempmail",
        help="邮箱后端: tempmail | cloudflare",
    )
    p.add_argument(
        "--no-oauth",
        action="store_true",
        help="只注册+SSO，不走 Build OAuth / CLIProxyAPI 导出",
    )
    p.add_argument(
        "--cliproxyapi-auth-dir",
        default=default_auth,
        help=f"CLIProxyAPI auth 目录（默认: {default_auth}）",
    )
    p.add_argument(
        "--cliproxyapi-base-url",
        default=CLIPROXYAPI_GROK_BASE_URL,
        help="Build 上游 base_url（默认 cli-chat-proxy.grok.com/v1）",
    )
    p.add_argument(
        "--oauth-headed",
        action="store_true",
        help="Playwright 有头模式（仅非协议回退时使用）",
    )
    p.add_argument(
        "--oauth-timeout",
        type=float,
        default=180.0,
        help="OAuth 等待超时秒数",
    )
    p.add_argument(
        "--no-oauth-protocol",
        action="store_true",
        help="禁用纯协议 OAuth（改走 Playwright 登录回退）",
    )
    p.add_argument(
        "--oauth-interactive-fallback",
        action="store_true",
        help="协议/Playwright 失败时回退到系统浏览器手动登录",
    )
    p.add_argument(
        "--oauth-debug",
        action="store_true",
        help="打印协议 OAuth 调试日志",
    )
    p.add_argument(
        "--accounts-output-dir",
        default="",
        help="可选：写合并台账 accounts_output（默认不写；传目录路径开启）",
    )
    p.add_argument(
        "--check-quota",
        action="store_true",
        help="OAuth 成功后用 check_accounts 探测 Build 额度；有额度才保留在 cliproxyapi_auth（默认关闭）",
    )
    p.add_argument(
        "--failed-auth-dir",
        default="",
        help="额度不可用时 auth 文件移入的目录（默认: <cliproxyapi-auth-dir>_failed）",
    )
    p.add_argument(
        "--check-quota-timeout",
        type=float,
        default=45.0,
        help="额度探测超时秒数（默认 45）",
    )
    args = p.parse_args()

    _total = args.count
    _t0 = time.time()
    threads = min(args.threads, args.count)
    do_oauth = not args.no_oauth
    check_quota = bool(args.check_quota) and do_oauth
    if args.check_quota and not do_oauth:
        print("warn: --check-quota ignored with --no-oauth", file=sys.stderr)

    failed_auth_dir = (args.failed_auth_dir or "").strip()
    if check_quota and not failed_auth_dir:
        failed_auth_dir = str(_default_failed_auth_dir(args.cliproxyapi_auth_dir))

    solver_mode = (os.environ.get("TURNSTILE_SOLVER") or "browser").strip().lower()
    ts_label = solver_mode if solver_mode else "browser"
    print(
        f"grok-build-auth: {args.count} accounts, {threads} threads, email={args.email}, "
        f"oauth={'on' if do_oauth else 'off'}, turnstile={ts_label}"
        f", check-quota={'on' if check_quota else 'off'}"
    )
    if do_oauth:
        print(f"  cliproxyapi-auth-dir: {args.cliproxyapi_auth_dir}")
        print(f"  build-base-url:       {args.cliproxyapi_base_url}")
    if check_quota:
        print(f"  failed-auth-dir:      {failed_auth_dir}")
        print(f"  check-quota-timeout:  {args.check_quota_timeout}s")
    print()

    accounts_dir = (args.accounts_output_dir or "").strip() or None
    common_kwargs = dict(
        do_oauth=do_oauth,
        oauth_headless=not args.oauth_headed,
        oauth_timeout=args.oauth_timeout,
        oauth_interactive_fallback=args.oauth_interactive_fallback,
        oauth_protocol=not args.no_oauth_protocol,
        oauth_debug=args.oauth_debug,
        cliproxyapi_auth_dir=args.cliproxyapi_auth_dir,
        cliproxyapi_base_url=args.cliproxyapi_base_url,
        accounts_output_dir=accounts_dir,
        check_quota=check_quota,
        failed_auth_dir=failed_auth_dir or None,
        check_quota_timeout=args.check_quota_timeout,
    )

    if args.count == 1:
        result = register_one(1, email_backend=args.email, **common_kwargs)
        _results.append(result)
    else:
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futures = [
                ex.submit(register_one, i, args.email, **common_kwargs)
                for i in range(1, args.count + 1)
            ]
            for f in as_completed(futures):
                _results.append(f.result())

    # summary
    ok_sso = [r for r in _results if r.get("sso")]
    ok_build = [r for r in _results if r.get("cliproxyapi_auth")]
    quota_fail = [r for r in _results if r.get("cliproxyapi_auth_failed")]
    fail = [r for r in _results if r.get("error") and not r.get("cliproxyapi_auth")]
    print(f"\n{'=' * 50}")
    parts = [
        f"Done in {time.time() - _t0:.0f}s",
        f"SSO OK: {len(ok_sso)}",
        f"BUILD OK: {len(ok_build)}",
    ]
    if check_quota:
        parts.append(f"QUOTA FAIL: {len(quota_fail)}")
    parts.append(f"FAIL: {len(fail)}")
    print("  |  ".join(parts))
    print(f"{'=' * 50}")
    for r in _results:
        email = r.get("email") or "?"
        if r.get("cliproxyapi_auth"):
            rem = r.get("quota_remaining_tokens")
            extra = f"  tokens={rem}" if rem is not None else ""
            print(f"  {email:40s}  BUILD  {r['cliproxyapi_auth']}{extra}")
        elif r.get("cliproxyapi_auth_failed"):
            print(
                f"  {email:40s}  QUOTA-FAIL  {r['cliproxyapi_auth_failed']}  "
                f"({r.get('error', '?')})"
            )
        elif r.get("sso") and not do_oauth:
            print(f"  {email:40s}  SSO    {r['sso'][:36]}...")
        elif r.get("sso") and r.get("error"):
            print(f"  {email:40s}  SSO-ok OAuth-FAIL: {r.get('error')}")
        else:
            print(f"  {email:40s}  FAIL: {r.get('error', '?')}")


if __name__ == "__main__":
    main()
