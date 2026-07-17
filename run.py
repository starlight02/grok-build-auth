#!/usr/bin/env python3
"""grok-build-auth — 一键注册 x.ai 账号 + SSO + Grok Build OAuth（CLIProxyAPI 可用）

流程:
  1) 协议注册（邮箱验证 + Turnstile + create_account）
  2) 提取 SSO
  3) Build OAuth（快路径：协议 SSO session-reuse；失败回退 sso2auth Device Flow）
  4) 导出 CLIProxyAPI auth：cli-chat-proxy.grok.com + grok-cli headers
     → 可直接用 grok-4.5 走 Build/CLI 编码通道

环境变量（按需设置）:
    TURNSTILE_SOLVER       auto|drission|browser|safari (default auto → DrissionPage+turnstilePatch)
    TURNSTILE_HEADLESS     drission 默认 0（有头自动点）；browser/playwright 默认 1
    TURNSTILE_BROWSER_CHANNEL  playwright only (chrome auto when available)
    TURNSTILE_INTERACTIVE  1=手动点 Turnstile（仅 playwright；强制有头）
    TURNSTILE_BROWSER_REUSE    1=keep per-thread browser warm (default 1)
    TURNSTILE_TIMEOUT      hard wall-clock seconds per Turnstile solve (default 60)
    TURNSTILE_PARALLEL     pool 关闭时 mint 并发（默认随 -t，上限 8）
    TURNSTILE_POOL         后台 token 池（默认开；0=关）
    TURNSTILE_POOL_SIZE    池硬上限（默认随 -t 自动；显式设置则固定）
    TURNSTILE_POOL_TARGET  空闲预存（默认 min(2,size)；够用即停产）
    TURNSTILE_POOL_MINTERS mint 线程（默认随 -t：1/2/3/4；Safari 固定 1）
    TURNSTILE_TOKEN_MAX_AGE  token 最大年龄秒（默认 200）
    TURNSTILE_PAUSE_FILE   存在则暂停 mint/HID 点击（默认 /tmp/grok-turnstile.pause）
    TEMPMAIL_API_KEY       Optional Tempmail.lol Plus/Ultra（免费层无需；提高限额）
    TEMPMAIL_FREE_CREATE_INTERVAL  无 key 时 create 最小间隔秒（默认 3 ≈20/min）
    MAIL_CODE_TIMEOUT     Seconds to wait for verification code before rotating inbox (default 30)
    MAIL_MAX_ATTEMPTS     Fresh inboxes to try when mail is silent (default 3)
    CLOUDFLARE_API_TOKEN   Cloudflare API token (alias_mail 邮箱后端)
    CLIPROXYAPI_AUTH_DIR   CLIProxyAPI data/auth 目录（可选）
    HTTPS_PROXY / HTTP_PROXY  单代理（无池文件时）
    PROXY_POOL_FILE          代理池文件（每行一个 URL；启动时探测出口地区）
    PROXY_POOL               内联列表（逗号/换行；大池请用 FILE）
    PROXY_REGION             目标地区码（如 us/jp/hk；探测后只保留该地区轮换）
    PROXY_POOL_SCOPE         same_region（默认）| all
    PROXY_GEO_WORKERS        并发探测数（默认 16）
    PROXY_GEO_CACHE          地区缓存 JSON（默认 ./.proxy_geo_cache.json）
    PROXY_PREFLIGHT          多代理默认开：启动 TCP+CONNECT 预检，剔除死代理
    PROXY_PREFLIGHT_WORKERS  预检并发（默认 32）
    PROXY_PREFLIGHT_TIMEOUT  单代理预检秒（默认 6）
    PROXY_RETRY              单号遇代理传输失败时换代理重试次数（默认 8）


CLI 常用:
    -n / -t                账号数 / 并发（默认 -t 4；池 size/minters 随 -t 自动）
    --no-oauth-protocol    跳过协议 OAuth，直接 sso2auth Device Flow
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
from xconsole_client.turnstile_pool import (
    TurnstileTokenPool,
    pause_file_path,
    suggest_pool_params,
)
from xconsole_client.proxy_pool import (
    ProxyPool,
    is_proxy_transport_error,
    proxy_retry_limit,
    single_proxy_from_env,
)
from xconsole_client.xai_oauth import (
    CLIPROXYAPI_GROK_BASE_URL,
    OAuthLoginResult,
    default_cliproxyapi_auth_dir,
)
from xconsole_client.oauth_protocol import extract_cookies_from_auth_client
from xconsole_client.sso2auth import mint_cpa_from_sso

# -- secrets from environment only ---------------------------------------
TEMPMAIL_KEY = os.environ.get("TEMPMAIL_API_KEY", "")
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
# Legacy single-proxy fallback (pool prefers PROXY_POOL / PROXY_POOL_FILE).
PROXY = single_proxy_from_env()


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


# Turnstile mint concurrency when pool is OFF. Env overrides; else follow -t.
def _turnstile_parallel(reg_threads: int = 2) -> int:
    raw = (os.environ.get("TURNSTILE_PARALLEL") or "").strip()
    if raw:
        try:
            return max(1, min(int(raw), 8))
        except ValueError:
            pass
    return max(1, min(8, int(reg_threads or 1)))


_turnstile_lock = threading.Semaphore(2)
_token_pool: Optional[TurnstileTokenPool] = None
_shared_solver = None
_proxy_pool: Optional[ProxyPool] = None
_results: list[dict] = []
_done = 0
_total = 0
_t0 = 0.0


def _acquire_proxy() -> str:
    """Next proxy URL for this registration (pool) or legacy single PROXY."""
    pool = _proxy_pool
    if pool is not None:
        return pool.acquire().url
    return PROXY


def _mark_proxy_bad(proxy: str, reason: str) -> None:
    pool = _proxy_pool
    if pool is None or not proxy:
        return
    if pool.mark_bad(proxy, reason):
        print(
            f"  [proxy-pool] disabled ({pool.disabled_count} dead, live={pool.size}): "
            f"{reason[:120]}",
            flush=True,
        )



def _log(i: int, msg: str):
    elapsed = time.time() - _t0
    bar = f"[{_done}/{_total}]" if _total > 1 else ""
    print(f"  {bar} [#{i}] {msg}  ({elapsed:.0f}s)")


def _pool_enabled(solver_mode: str) -> bool:
    raw = (os.environ.get("TURNSTILE_POOL") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    # Default ON for every solver — demand-driven pool matches free-tier reg pace.
    return True


def _acquire_turnstile(index: int, turnstile_solver) -> tuple[str, float]:
    """Return (token, minted_at). Uses background pool when enabled."""
    global _token_pool
    pool = _token_pool
    if pool is not None:
        item = pool.acquire(timeout=max(120.0, float(os.environ.get("TURNSTILE_TIMEOUT") or 60) * 4))
        _log(index, f"Turnstile {len(item.token)} chars from pool (age={item.age:.0f}s q={pool.qsize()})")
        return item.token, item.minted_at
    with _turnstile_lock:
        token = turnstile_solver.solve_turnstile(
            website_url=SIGNUP_URL,
            website_key=C.TURNSTILE_SITEKEY,
            premium=True,
        )
    return token, time.time()


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
        receiver = AliasMailCodeReceiver(
            cf, address=address, timeout=120, interval=3, since_now=True
        )
        return address, receiver
    else:
        raise ValueError(f"unknown email backend: {backend}")


def _save_account_bundle(result: dict, output_dir: Path) -> Path:
    """Persist a combined signup+oauth record for later tooling (opt-in)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    email = str(result.get("email") or "unknown")
    safe = (
        "".join(ch if ch.isalnum() or ch in "._-@" else "_" for ch in email)
        or "unknown"
    )
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

    Permanent ``chat_endpoint_denied`` (403 + denied text) is NOT retried — the
    account cannot use chat and should be moved out immediately.

    Returns a small dict for the result record:
      usable, remaining_tokens, status, reasons, path (final location), error?
    """
    # Lazy import so --check-quota-off path never pays the cost.
    from check_accounts import CHAT_ENDPOINT_DENIED, check_one

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
        reasons_list = list(probe.get("reasons") or [])
        permanent_denied = bool(
            probe.get("chat_endpoint_denied")
            or probe.get("code") == CHAT_ENDPOINT_DENIED
            or CHAT_ENDPOINT_DENIED in reasons_list
        )
        # Permanent chat denial: do not treat as transient 403.
        if permanent_denied:
            break
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
    reasons_list = list(probe.get("reasons") or [])
    reasons = ",".join(reasons_list) or "?"
    permanent_denied = bool(
        probe.get("chat_endpoint_denied")
        or probe.get("code") == CHAT_ENDPOINT_DENIED
        or CHAT_ENDPOINT_DENIED in reasons_list
    )

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
            "chat_endpoint_denied": False,
            "path": str(auth_path),
            "error": None,
        }

    dest = _move_auth_to_failed(auth_path, failed_dir)
    if permanent_denied:
        err = f"chat_endpoint_denied: status={status} reasons={reasons}"
        tag = "DENIED"
    else:
        err = f"quota unusable: status={status} reasons={reasons}"
        tag = "FAIL"
    _log(index, f"quota {tag} → moved to {dest.name}  ({err})")
    return {
        "usable": False,
        "remaining_tokens": rem,
        "status": status,
        "reasons": reasons,
        "chat_endpoint_denied": permanent_denied,
        "path": str(dest),
        "error": err,
    }



def register_one(
    index: int,
    email_backend: str = "tempmail",
    *,
    do_oauth: bool = True,
    oauth_protocol: bool = True,
    oauth_debug: bool = False,
    cliproxyapi_auth_dir: Optional[str | Path] = None,
    cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
    accounts_output_dir: Optional[str | Path] = None,
    check_quota: bool = False,
    failed_auth_dir: Optional[str | Path] = None,
    check_quota_timeout: float = 45.0,
) -> dict:
    """Run signup (+ optional Build OAuth export). Thread-safe.

    On proxy transport failures (timeout / CONNECT abort), mark the proxy bad
    and rotate to another from the pool (PROXY_RETRY times).
    """
    max_attempts = proxy_retry_limit() if _proxy_pool is not None else 1
    last: dict = {
        "email": "",
        "password": "",
        "sso": None,
        "oauth_access_token": None,
        "cliproxyapi_auth": None,
        "error": "proxy retries exhausted",
    }
    try:
        for attempt in range(1, max_attempts + 1):
            try:
                proxy = _acquire_proxy()
            except Exception as exc:
                last = {
                    "email": "",
                    "password": "",
                    "sso": None,
                    "oauth_access_token": None,
                    "cliproxyapi_auth": None,
                    "error": str(exc),
                }
                _log(index, f"ERROR: {exc}")
                break

            if attempt > 1:
                _log(index, f"retry with new proxy ({attempt}/{max_attempts})")

            result = _register_one_attempt(
                index,
                email_backend,
                proxy=proxy,
                do_oauth=do_oauth,
                oauth_protocol=oauth_protocol,
                oauth_debug=oauth_debug,
                cliproxyapi_auth_dir=cliproxyapi_auth_dir,
                cliproxyapi_base_url=cliproxyapi_base_url,
                accounts_output_dir=accounts_output_dir,
                check_quota=check_quota,
                failed_auth_dir=failed_auth_dir,
                check_quota_timeout=check_quota_timeout,
            )
            err = result.get("error")
            if not err:
                return result

            if _proxy_pool is not None and is_proxy_transport_error(Exception(str(err))):
                _mark_proxy_bad(proxy, str(err))
                last = result
                if attempt < max_attempts and _proxy_pool.size > 0:
                    continue
            return result
        return last
    finally:
        with _results_lock:
            global _done
            _done += 1


def _register_one_attempt(
    index: int,
    email_backend: str,
    *,
    proxy: str,
    do_oauth: bool = True,
    oauth_protocol: bool = True,
    oauth_debug: bool = False,
    cliproxyapi_auth_dir: Optional[str | Path] = None,
    cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
    accounts_output_dir: Optional[str | Path] = None,
    check_quota: bool = False,
    failed_auth_dir: Optional[str | Path] = None,
    check_quota_timeout: float = 45.0,
) -> dict:
    """Single registration attempt on a fixed proxy."""
    # Turnstile: local browser only (shared solver when pool is on).
    global _shared_solver
    try:
        turnstile_solver = _shared_solver or resolve_turnstile_solver(
            proxy=proxy,
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

    # curl_cffi is often CF-blocked on residential/datacenter IPs that Safari/urllib still pass.
    transport = (os.environ.get("XCONSOLE_TRANSPORT") or "").strip().lower()
    if not transport:
        solver = (os.environ.get("TURNSTILE_SOLVER") or "").strip().lower()
        transport = "urllib" if solver in {"safari", "webkit-system", "system-safari"} else "curl_cffi"
    if transport not in {"curl_cffi", "urllib"}:
        transport = "curl_cffi"
    c = XConsoleAuthClient(
        debug=False,
        signup_url=SIGNUP_URL,
        transport=transport,
        proxy=proxy or None,
    )
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
            turnstile, ts_t0 = _acquire_turnstile(index, turnstile_solver)
        except Exception as ts_exc:
            # Let mail finish its current attempt, then surface.
            mail_thread.join(timeout=mail_timeout + 5)
            stop_mail.set()
            if code_box.get("code"):
                _log(index, f"Turnstile first try failed ({ts_exc}); retry after mail")
                turnstile, ts_t0 = _acquire_turnstile(index, turnstile_solver)
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

        # Token TTL safety: if mint finished long before mail, re-acquire.
        age = time.time() - ts_t0
        max_age = float(getattr(_token_pool, "max_age", 200.0) if _token_pool else 240.0)
        if age > max_age:
            _log(index, f"Turnstile token age {age:.0f}s; re-acquire")
            turnstile, ts_t0 = _acquire_turnstile(index, turnstile_solver)
        if _token_pool is None:
            _log(
                index,
                f"Turnstile {len(turnstile)} chars via {type(turnstile_solver).__name__}",
            )

        # 4. create account
        res = c.create_account(
            email=email,
            given_name="Test",
            family_name="User",
            password=password,
            email_validation_code=code,
            turnstile_token=turnstile,
            castle_request_token="",
            conversion_id=str(uuid.uuid4()),
        )
        if not res.ok:
            # One free retry with a fresh token (expired / race)
            _log(index, f"create_account HTTP {res.http_status}; retry Turnstile once")
            turnstile, ts_t0 = _acquire_turnstile(index, turnstile_solver)
            res = c.create_account(
                email=email,
                given_name="Test",
                family_name="User",
                password=password,
                email_validation_code=code,
                turnstile_token=turnstile,
                castle_request_token="",
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
        # Fast: protocol session-reuse (pure HTTP). Fallback: sso2auth Device Flow.
        # No Playwright / system-browser OAuth in the register path.
        if do_oauth:
            auth_dir = (
                Path(cliproxyapi_auth_dir)
                if cliproxyapi_auth_dir
                else default_cliproxyapi_auth_dir()
            )
            session_cookies = extract_cookies_from_auth_client(c)
            if sso:
                session_cookies = dict(session_cookies or {})
                session_cookies.setdefault("sso", sso)
            _log(
                index,
                f"OAuth Build path → {auth_dir}  (cookies={len(session_cookies)})",
            )

            oauth: Optional[OAuthLoginResult] = None
            protocol_err: Optional[BaseException] = None

            if oauth_protocol:
                try:
                    from xconsole_client.oauth_protocol import login_with_protocol

                    oauth = login_with_protocol(
                        email,
                        password,
                        proxy=proxy,
                        debug=oauth_debug,
                        cliproxyapi_auth_dir=str(auth_dir),
                        cliproxyapi_base_url=cliproxyapi_base_url,
                        session_cookies=session_cookies,
                        auth_client=c,
                        allow_create_session=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    protocol_err = exc
                    _log(index, f"protocol OAuth failed ({exc}); sso2auth device flow")

            if oauth is None:
                if not sso:
                    raise RuntimeError(
                        "OAuth needs SSO for device flow"
                        + (f"; protocol: {protocol_err}" if protocol_err else "")
                    )
                mint = mint_cpa_from_sso(
                    sso,
                    email=email,
                    auth_dir=auth_dir,
                    proxy=proxy,
                    base_url=cliproxyapi_base_url,
                    skip_existing=False,
                    log=lambda m, i=index: _log(i, m),
                )
                if not mint.get("ok"):
                    raise RuntimeError(
                        f"sso2auth failed: {mint.get('error')}"
                        + (f"; protocol: {protocol_err}" if protocol_err else "")
                    )
                token = (
                    mint.get("token")
                    if isinstance(mint.get("token"), dict)
                    else {
                        "access_token": mint.get("access_token") or "",
                        "refresh_token": mint.get("refresh_token") or "",
                    }
                )
                userinfo = (
                    mint.get("userinfo")
                    if isinstance(mint.get("userinfo"), dict)
                    else {}
                )
                cpa_path = Path(str(mint["path"])) if mint.get("path") else None
                oauth = OAuthLoginResult(
                    token=token,
                    userinfo=userinfo,
                    id_token_payload=None,
                    path=None,
                    cliproxyapi_path=cpa_path,
                )

            result["oauth_access_token"] = oauth.access_token
            result["oauth_refresh_token"] = oauth.refresh_token
            result["oauth_record"] = str(oauth.path) if oauth.path else None
            result["cliproxyapi_auth"] = (
                str(oauth.cliproxyapi_path) if oauth.cliproxyapi_path else None
            )
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
                result["chat_endpoint_denied"] = bool(gate.get("chat_endpoint_denied"))
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


def main():
    global _total, _t0
    default_auth = str(default_cliproxyapi_auth_dir())
    p = argparse.ArgumentParser(
        description="grok-build-auth: x.ai register + SSO + Grok Build OAuth (CLIProxyAPI-ready)",
    )
    p.add_argument("-n", "--count", type=int, default=1, help="账号数量")
    p.add_argument(
        "-t",
        "--threads",
        type=int,
        default=4,
        help="并发线程数（注册 + OAuth；默认 4，对齐 Tempmail free 稳态上限）",
    )
    p.add_argument(
        "-e",
        "--email",
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
        "--no-oauth-protocol",
        action="store_true",
        help="禁用协议 session-reuse OAuth，直接走 sso2auth Device Flow",
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

    solver_mode = (os.environ.get("TURNSTILE_SOLVER") or "auto").strip().lower()
    ts_label = solver_mode if solver_mode else "auto"
    use_pool = _pool_enabled(solver_mode)
    pool_size, pool_target, pool_minters = suggest_pool_params(
        threads, solver_mode=solver_mode
    )
    print(
        f"grok-build-auth: {args.count} accounts, {threads} threads, email={args.email}, "
        f"oauth={'on' if do_oauth else 'off'}, turnstile={ts_label}"
        f", pool={'on' if use_pool else 'off'}"
        f", check-quota={'on' if check_quota else 'off'}"
    )
    if do_oauth:
        print(f"  cliproxyapi-auth-dir: {args.cliproxyapi_auth_dir}")
        print(f"  build-base-url:       {args.cliproxyapi_base_url}")
    if check_quota:
        print(f"  failed-auth-dir:      {failed_auth_dir}")
        print(f"  check-quota-timeout:  {args.check_quota_timeout}s")
    if use_pool:
        print(
            f"  turnstile-pool:       size={pool_size} target={pool_target} "
            f"minters={pool_minters} "
            f"max_age={os.environ.get('TURNSTILE_TOKEN_MAX_AGE') or '200'}s "
            f"(auto from -t={threads})"
        )
        print(f"  pause-file:           {pause_file_path()}  (touch=pause mint/click, rm=resume)")
    else:
        par = _turnstile_parallel(threads)
        print(f"  turnstile-parallel:   {par} (pool off; auto from -t={threads})")

    global _token_pool, _shared_solver, _turnstile_lock, _proxy_pool
    def _proxy_log(msg: str) -> None:
        print(f"  [proxy-pool] {msg}", flush=True)

    try:
        _proxy_pool = ProxyPool.from_env(log=_proxy_log)
    except Exception as exc:
        print(f"error: proxy pool failed: {exc}", file=sys.stderr)
        sys.exit(2)
    if _proxy_pool is not None:
        print(f"  proxy-pool:           {_proxy_pool.summary()}")
    elif PROXY:
        print(f"  proxy:                single (HTTPS_PROXY/HTTP_PROXY)")
    print()

    _token_pool = None
    _shared_solver = None
    # Shared turnstile minters stick to one proxy from the active region.
    mint_proxy = PROXY
    if _proxy_pool is not None:
        mint_proxy = _proxy_pool.acquire().url
    if use_pool:
        _shared_solver = resolve_turnstile_solver(
            proxy=mint_proxy,
            debug=args.oauth_debug,
        )

        def _pool_log(msg: str) -> None:
            print(f"  [ts-pool] {msg}", flush=True)

        _token_pool = TurnstileTokenPool(
            _shared_solver,
            website_url=SIGNUP_URL,
            website_key=C.TURNSTILE_SITEKEY,
            size=pool_size,
            target=pool_target,
            minters=pool_minters,
            log=_pool_log,
        )
        _token_pool.start()
    else:
        # Non-pool path: rebind mint semaphore to match registration concurrency.
        _turnstile_lock = threading.Semaphore(_turnstile_parallel(threads))

    try:
        accounts_dir = (args.accounts_output_dir or "").strip() or None
        common_kwargs = dict(
            do_oauth=do_oauth,
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
    finally:
        if _token_pool is not None:
            _token_pool.stop(wait=2.0)
            _token_pool = None

    # summary
    ok_sso = [r for r in _results if r.get("sso")]
    ok_build = [r for r in _results if r.get("cliproxyapi_auth")]
    denied = [r for r in _results if r.get("chat_endpoint_denied")]
    quota_fail = [
        r
        for r in _results
        if r.get("cliproxyapi_auth_failed") and not r.get("chat_endpoint_denied")
    ]
    fail = [r for r in _results if r.get("error") and not r.get("cliproxyapi_auth")]
    print(f"\n{'=' * 50}")
    parts = [
        f"Done in {time.time() - _t0:.0f}s",
        f"SSO OK: {len(ok_sso)}",
        f"BUILD OK: {len(ok_build)}",
    ]
    if check_quota:
        parts.append(f"DENIED: {len(denied)}")
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
        elif r.get("chat_endpoint_denied"):
            print(
                f"  {email:40s}  DENIED  {r.get('cliproxyapi_auth_failed') or '-'}  "
                f"({r.get('error', '?')})"
            )
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
