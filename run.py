#!/usr/bin/env python3
"""grok-build-auth — 一键注册 x.ai 账号 + SSO + Grok Build OAuth（CLIProxyAPI 可用）

流程:
  1) 协议注册（邮箱验证 + Turnstile + create_account）
  2) 提取 SSO 后立刻释放注册线程
  3) 独立 OAuth worker 池：优先协议 SSO session-reuse 快路径；失败再回退 Device Flow
  4) 导出 CLIProxyAPI auth：cli-chat-proxy.grok.com + grok-cli headers

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
    YYDS_API_KEY / YYDS_JWT  YYDS 邮箱（-e yyds|auto；二选一）
    YYDS_API_BASE            默认 https://maliapi.215.im/v1
    YYDS_DOMAINS             可选域名白名单（空=全部已验证域名，负载均衡）
    MAIL_BACKENDS           多渠道列表（如 tempmail,yyds）；空则看 -e
    MAIL_POOL               邮箱预创建池（默认开；0=关）
    MAIL_POOL_SIZE/TARGET/MINTERS  同 turnstile 池语义（默认随 -t）
    MAIL_POOL_MAX_AGE       池内邮箱最大年龄秒（默认 600）
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
    OAUTH_ASYNC              1=SSO 后交独立 OAuth 池（默认 1）；0=注册线程内串行 OAuth
    OAUTH_WORKERS            OAuth 池线程数（默认 = max(-t, 2)）
    OAUTH_TRANSPORT_RETRIES  快路径 session-reuse 传输重试次数（默认 3）
    OAUTH_ALLOW_DEVICE       1=快路径失败后回退 Device Flow（默认 1）
    TRANSPORT_RETRY          无代理池时注册传输重试（默认 3）
    VISIT_HOME               1=注册前 visit console home（默认 0 跳过）

CLI 常用:
    -n / -t                注册并发（默认 -t 4；池 size/minters 随 -t 自动）
    --oauth-workers        OAuth 池线程数（默认与 -t 相同，至少 2）
    --no-oauth-async       关闭 OAuth 异步池，SSO 后同线程串行 Build
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
import signal
import threading
import queue
import argparse
import shutil
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from typing import Any, Optional

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
from xconsole_client.mailbox_pool import MailboxPool, suggest_mail_pool_params
from xconsole_client.mail_channels import (
    ChannelRouter,
    build_router,
    cli_email_choices,
    resolve_channels,
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
_stop = threading.Event()
_sigint_count = 0


def request_stop(reason: str = "stop") -> None:
    """Cooperative cancel: first Ctrl+C stops pools and closes browsers.

    Full stats are printed only at process end (or second Ctrl+C),
    so they stay at the bottom of the log — not buried by worker FAIL lines.
    """
    first = not _stop.is_set()
    if first:
        with _results_lock:
            n = len(_results)
        print(
            f"\n  [{reason}] 正在停止… 已完成 {n}/{_total or '?'}，收尾后打印统计；再按一次 Ctrl+C 强制退出并立即打印统计",
            flush=True,
        )
    _stop.set()
    try:
        from xconsole_client.drission_solver import _close_all_drission_browsers

        _close_all_drission_browsers()
    except Exception:
        pass
    try:
        from xconsole_client import solver as _solver_mod

        close_fn = getattr(_solver_mod, "_close_thread_browsers", None)
        if callable(close_fn):
            close_fn()
    except Exception:
        pass
    pool = _token_pool
    if pool is not None:
        try:
            pool.stop(wait=0.2)
        except Exception:
            pass
    mpool = _mail_pool
    if mpool is not None:
        try:
            mpool.stop(wait=0.2)
        except Exception:
            pass


def _install_sigint_handler() -> None:
    def _handler(signum, frame):  # noqa: ARG001
        global _sigint_count
        _sigint_count += 1
        if _sigint_count >= 2:
            print("\n  [SIGINT] 强制退出 — 统计如下（应在日志最底部）", flush=True)
            _stop.set()
            try:
                # brief pause so in-flight prints that already started can finish
                time.sleep(0.15)
            except Exception:
                pass
            try:
                _print_run_summary(
                    check_quota=bool(_summary_ctx.get("check_quota")),
                    do_oauth=bool(_summary_ctx.get("do_oauth", True)),
                    title="Forced stop",
                )
            except Exception:
                pass
            try:
                import sys as _sys

                _sys.stdout.flush()
                _sys.stderr.flush()
            except Exception:
                pass
            os._exit(130)
        request_stop("SIGINT")

    try:
        signal.signal(signal.SIGINT, _handler)
    except Exception:
        pass
    try:
        signal.signal(signal.SIGTERM, _handler)
    except Exception:
        pass


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
_mail_pool: Optional[MailboxPool] = None
_mail_router: Optional[ChannelRouter] = None
_shared_solver = None
_proxy_pool: Optional[ProxyPool] = None
_results: list[dict] = []
_done = 0  # attempts finished
_ok = 0  # successes toward -n target
_total = 0  # target success count
_t0 = 0.0
_summary_ctx: dict = {"check_quota": False, "do_oauth": True}

_oauth_q: Optional[queue.Queue] = None
_oauth_inflight = 0
_oauth_inflight_lock = threading.Lock()
_oauth_workers: list[threading.Thread] = []


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
    elapsed = time.time() - _t0 if _t0 else 0
    with _results_lock:
        ok, done, total = _ok, _done, _total
    if total > 1:
        bar = f"[ok {ok}/{total} att {done}]"
    elif total == 1:
        bar = f"[ok {ok}/{total}]"
    else:
        bar = ""
    print(f"  {bar} [#{i}] {msg}  ({elapsed:.0f}s)", flush=True)


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
        item = pool.acquire(
            timeout=max(120.0, float(os.environ.get("TURNSTILE_TIMEOUT") or 60) * 4)
        )
        _log(
            index,
            f"Turnstile {len(item.token)} chars from pool (age={item.age:.0f}s q={pool.qsize()})",
        )
        return item.token, item.minted_at
    with _turnstile_lock:
        token = turnstile_solver.solve_turnstile(
            website_url=SIGNUP_URL,
            website_key=C.TURNSTILE_SITEKEY,
            premium=True,
        )
    return token, time.time()


def _mail_pool_enabled() -> bool:
    raw = (os.environ.get("MAIL_POOL") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    # Default ON — pre-create inboxes in parallel with turnstile mint.
    return True


def _build_mail_router(channels: list[str], *, log=None) -> ChannelRouter:
    """Build router from registry (creators resolved per ChannelSpec)."""
    return build_router(channels, log=log)


def _acquire_email(backend: str = "auto", index: int = 0) -> tuple[str, object, str]:
    """Return (email, receiver, channel). Pool preferred; else router/single."""
    global _mail_pool, _mail_router
    pool = _mail_pool
    if pool is not None:
        item = pool.acquire(
            timeout=max(120.0, float(os.environ.get("MAIL_CODE_TIMEOUT") or 30) * 4)
        )
        if index:
            _log(
                index,
                f"email from pool [{item.channel}]: {item.email} "
                f"(age={item.age:.0f}s q={pool.qsize()})",
            )
        return item.email, item.receiver, item.channel

    router = _mail_router
    if router is not None:
        box = router.create()
        if index:
            _log(index, f"email via {box.channel}: {box.email}")
        return box.email, box.receiver, box.channel

    # Ad-hoc: resolve + create once (single = direct; multi = ephemeral router).
    from xconsole_client.mail_channels import create_mailbox

    box = create_mailbox(backend)
    if index:
        _log(index, f"email via {box.channel}: {box.email}")
    return box.email, box.receiver, box.channel


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
            f"quota probe attempt {attempt}/{attempts} status={status}; retry in {wait_s:.0f}s",
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
    oauth_async: bool = True,
    oauth_protocol: bool = True,
    oauth_debug: bool = False,
    cliproxyapi_auth_dir: Optional[str | Path] = None,
    cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
    accounts_output_dir: Optional[str | Path] = None,
    check_quota: bool = False,
    failed_auth_dir: Optional[str | Path] = None,
    check_quota_timeout: float = 45.0,
) -> dict:
    if _stop.is_set():
        return {"error": "stopped", "email": None}

    """Run signup (+ optional Build OAuth export). Thread-safe.

    On proxy transport failures (timeout / CONNECT abort), mark the proxy bad
    and rotate to another from the pool (PROXY_RETRY times).
    """
    # Transport flakiness (SSL EOF / timeout) dominates long runs. Retry a few
    # times even without a proxy pool; with a pool, also rotate exit IPs.
    if _proxy_pool is not None:
        max_attempts = proxy_retry_limit()
    else:
        try:
            max_attempts = max(1, min(8, int(os.environ.get("TRANSPORT_RETRY") or "3")))
        except ValueError:
            max_attempts = 3
    last: dict = {
        "email": "",
        "password": "",
        "sso": None,
        "oauth_access_token": None,
        "cliproxyapi_auth": None,
        "error": "transport retries exhausted",
    }
    try:
        for attempt in range(1, max_attempts + 1):
            if _stop.is_set():
                last = {"error": "stopped", "email": None}
                break
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
                why = "proxy" if _proxy_pool is not None else "transport"
                _log(index, f"retry ({why} {attempt}/{max_attempts})")
                time.sleep(min(2.0, 0.25 * attempt))

            result = _register_one_attempt(
                index,
                email_backend,
                proxy=proxy,
                do_oauth=do_oauth,
                oauth_async=oauth_async,
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

            # SSO already minted but OAuth died: do not burn another full signup
            # in this worker; surface for offline retry_oauth_from_sso.
            if result.get("sso") and err and "SSO" not in str(err):
                last = result
                if is_proxy_transport_error(Exception(str(err))) and attempt < max_attempts:
                    # One more OAuth-only style full attempt is expensive; prefer
                    # returning partial so pool capacity goes to new signups.
                    # Optional: set OAUTH_FULL_RETRY=1 to re-run whole attempt.
                    if (os.environ.get("OAUTH_FULL_RETRY") or "").strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }:
                        if _proxy_pool is not None:
                            _mark_proxy_bad(proxy, str(err))
                        continue
                return result

            if is_proxy_transport_error(Exception(str(err))):
                if _proxy_pool is not None:
                    _mark_proxy_bad(proxy, str(err))
                last = result
                if attempt < max_attempts and (_proxy_pool is None or _proxy_pool.size > 0):
                    continue
            return result
        return last
    finally:
        with _results_lock:
            global _done, _ok
            _done += 1


def _oauth_async_enabled(do_oauth: bool) -> bool:
    """SSO then hand off Build OAuth to a dedicated pool (default on)."""
    if not do_oauth:
        return False
    raw = (os.environ.get("OAUTH_ASYNC") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _oauth_allow_device() -> bool:
    """Device Flow fallback after fast-path failure (default on)."""
    raw = (os.environ.get("OAUTH_ALLOW_DEVICE") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _oauth_transport_retries() -> int:
    try:
        return max(1, min(8, int(os.environ.get("OAUTH_TRANSPORT_RETRIES") or "3")))
    except ValueError:
        return 3


def _finish_build_oauth(
    *,
    index: int,
    email: str,
    password: str,
    sso: str,
    session_cookies: Optional[dict[str, str]],
    proxy: str,
    oauth_protocol: bool = True,
    oauth_debug: bool = False,
    cliproxyapi_auth_dir: Optional[str | Path] = None,
    cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
    check_quota: bool = False,
    failed_auth_dir: Optional[str | Path] = None,
    check_quota_timeout: float = 45.0,
    auth_client: Any = None,
) -> dict:
    """SSO → CLIProxyAPI Build. Prefer protocol session-reuse; optional Device fallback.

    Fast path must work with *session_cookies* alone (async OAuth has no live client).
    """
    auth_dir = (
        Path(cliproxyapi_auth_dir) if cliproxyapi_auth_dir else default_cliproxyapi_auth_dir()
    )
    cookies = dict(session_cookies or {})
    if sso:
        cookies.setdefault("sso", sso)
    if not cookies.get("sso"):
        return {
            "email": email,
            "password": password,
            "sso": sso,
            "oauth_access_token": None,
            "cliproxyapi_auth": None,
            "error": "Build OAuth needs sso cookie for session-reuse",
        }

    _log(
        index,
        f"OAuth Build path → {auth_dir}  (cookies={len(cookies)}, "
        f"fast={'on' if oauth_protocol else 'off'})",
    )

    oauth: Optional[OAuthLoginResult] = None
    protocol_err: Optional[BaseException] = None

    if oauth_protocol:
        from xconsole_client.oauth_protocol import login_with_protocol

        tries = _oauth_transport_retries()
        for o_try in range(1, tries + 1):
            try:
                # allow_create_session=False keeps this on pure session-reuse
                # (no CreateSession+Turnstile). auth_client only when inline.
                oauth = login_with_protocol(
                    email,
                    password,
                    proxy=proxy,
                    debug=oauth_debug,
                    cliproxyapi_auth_dir=str(auth_dir),
                    cliproxyapi_base_url=cliproxyapi_base_url,
                    session_cookies=cookies,
                    auth_client=auth_client,
                    allow_create_session=False,
                )
                protocol_err = None
                break
            except Exception as exc:  # noqa: BLE001
                protocol_err = exc
                if is_proxy_transport_error(exc) and o_try < tries:
                    _log(
                        index,
                        f"session-reuse transport fail ({o_try}/{tries}): {exc}; retry",
                    )
                    time.sleep(0.35 * o_try)
                    continue
                _log(index, f"session-reuse failed ({exc})")
                break

    if oauth is None:
        if not _oauth_allow_device() and oauth_protocol:
            return {
                "email": email,
                "password": password,
                "sso": sso,
                "session_cookies": cookies,
                "oauth_access_token": None,
                "cliproxyapi_auth": None,
                "error": (
                    "session-reuse OAuth failed"
                    + (f": {protocol_err}" if protocol_err else "")
                    + " (device fallback disabled)"
                ),
            }
        if not sso:
            return {
                "email": email,
                "password": password,
                "sso": None,
                "oauth_access_token": None,
                "cliproxyapi_auth": None,
                "error": "OAuth needs SSO"
                + (f"; protocol: {protocol_err}" if protocol_err else ""),
            }
        if protocol_err is not None:
            _log(index, f"fallback Device Flow after session-reuse: {protocol_err}")
        else:
            _log(index, "Device Flow (protocol OAuth disabled)")
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
            return {
                "email": email,
                "password": password,
                "sso": sso,
                "session_cookies": cookies,
                "oauth_access_token": None,
                "cliproxyapi_auth": None,
                "error": f"sso2auth failed: {mint.get('error')}"
                + (f"; protocol: {protocol_err}" if protocol_err else ""),
            }
        _mint_token = mint.get("token")
        token: dict[str, Any] = (
            _mint_token
            if isinstance(_mint_token, dict)
            else {
                "access_token": mint.get("access_token") or "",
                "refresh_token": mint.get("refresh_token") or "",
            }
        )
        _mint_userinfo = mint.get("userinfo")
        userinfo: dict[str, Any] = _mint_userinfo if isinstance(_mint_userinfo, dict) else {}
        cpa_path = Path(str(mint["path"])) if mint.get("path") else None
        oauth = OAuthLoginResult(
            token=token,
            userinfo=userinfo,
            id_token_payload=None,
            path=None,
            cliproxyapi_path=cpa_path,
        )

    result: dict[str, Any] = {
        "email": email,
        "password": password,
        "sso": sso,
        "session_cookies": cookies,
        "oauth_access_token": oauth.access_token,
        "oauth_refresh_token": oauth.refresh_token,
        "oauth_record": str(oauth.path) if oauth.path else None,
        "cliproxyapi_auth": (str(oauth.cliproxyapi_path) if oauth.cliproxyapi_path else None),
        "error": None,
    }
    _log(
        index,
        f"Build OAuth OK  access={oauth.access_token[:20]}...  "
        f"cliproxy={oauth.cliproxyapi_path.name if oauth.cliproxyapi_path else '?'}",
    )

    if check_quota and oauth.cliproxyapi_path:
        fail_dir = Path(failed_auth_dir) if failed_auth_dir else _default_failed_auth_dir(auth_dir)
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
            result["cliproxyapi_auth_failed"] = gate["path"]
            result["cliproxyapi_auth"] = None
            result["error"] = gate.get("error") or "quota unusable"
    return result


def _enqueue_oauth_job(job: dict) -> None:
    global _oauth_inflight
    q = _oauth_q
    if q is None:
        raise RuntimeError("OAuth queue not started")
    with _oauth_inflight_lock:
        _oauth_inflight += 1
    q.put(job)


def _oauth_worker_loop(record_fn) -> None:
    """Consume SSO jobs; always prefer session-reuse fast path."""
    global _oauth_inflight
    q = _oauth_q
    assert q is not None
    while True:
        try:
            job = q.get(timeout=0.4)
        except queue.Empty:
            if _stop.is_set():
                with _oauth_inflight_lock:
                    # exit when no more work and stop requested
                    if q.empty() and _oauth_inflight <= 0:
                        return
            continue
        if job is None:
            q.task_done()
            return
        try:
            result = _finish_build_oauth(
                index=int(job.get("index") or 0),
                email=str(job.get("email") or ""),
                password=str(job.get("password") or ""),
                sso=str(job.get("sso") or ""),
                session_cookies=job.get("session_cookies") or {},
                proxy=str(job.get("proxy") or ""),
                oauth_protocol=bool(job.get("oauth_protocol", True)),
                oauth_debug=bool(job.get("oauth_debug", False)),
                cliproxyapi_auth_dir=job.get("cliproxyapi_auth_dir"),
                cliproxyapi_base_url=str(
                    job.get("cliproxyapi_base_url") or CLIPROXYAPI_GROK_BASE_URL
                ),
                check_quota=bool(job.get("check_quota", False)),
                failed_auth_dir=job.get("failed_auth_dir"),
                check_quota_timeout=float(job.get("check_quota_timeout") or 45.0),
                auth_client=None,  # async: cookies-only fast path
            )
            if job.get("email_channel"):
                result.setdefault("email_channel", job.get("email_channel"))
            record_fn(result)
        except Exception as exc:  # noqa: BLE001
            record_fn(
                {
                    "email": job.get("email"),
                    "password": job.get("password"),
                    "sso": job.get("sso"),
                    "oauth_access_token": None,
                    "cliproxyapi_auth": None,
                    "error": f"oauth worker: {exc}",
                }
            )
        finally:
            with _oauth_inflight_lock:
                _oauth_inflight = max(0, _oauth_inflight - 1)
            q.task_done()


def _register_one_attempt(
    index: int,
    email_backend: str,
    *,
    proxy: str,
    do_oauth: bool = True,
    oauth_async: bool = True,
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

    # HTTP transport is independent of Turnstile solver.
    # Safari only serializes token mint (HID); registration threads still use -t.
    # Default curl_cffi for all solvers; override with XCONSOLE_TRANSPORT=urllib|auto.
    transport = (os.environ.get("XCONSOLE_TRANSPORT") or "").strip().lower()
    if not transport:
        transport = "curl_cffi"
    if transport not in {"curl_cffi", "urllib", "auto"}:
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
        # 1. scrape signup (visit_home is optional; default skip — saves 1 RTT
        # and avoids an extra SSL failure surface that does not set signup cookies).
        if (os.environ.get("VISIT_HOME") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
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

        code_box: dict = {
            "code": None,
            "email": "",
            "channel": "",
            "err": None,
            "tries": 0,
        }
        stop_mail = threading.Event()

        def _mail_loop() -> None:
            last_err: Optional[BaseException] = None
            for attempt in range(1, mail_attempts + 1):
                if stop_mail.is_set():
                    return
                try:
                    addr, receiver, channel = _acquire_email(email_backend, index)
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    code_box["err"] = exc
                    _log(index, f"mail create failed ({exc})")
                    return
                code_box["email"] = addr
                code_box["channel"] = channel
                code_box["tries"] = attempt
                tag = f" (try {attempt}/{mail_attempts})" if mail_attempts > 1 else ""
                _log(index, f"email [{channel}]: {addr}{tag}")
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

        mail_thread = threading.Thread(target=_mail_loop, name=f"mail-{index}", daemon=True)
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
                f"email code timeout after {mail_attempts} inbox(es) × {mail_timeout:.0f}s"
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
        sso = c.fetch_sso_token(email=email, password=password, save=True, retries=2)
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
            "email_channel": code_box.get("channel") or "",
            "oauth_access_token": None,
            "oauth_refresh_token": None,
            "cliproxyapi_auth": None,
            "error": None,
        }

        # 6. Build OAuth — async pool (default) or inline same thread.
        if do_oauth:
            session_cookies = extract_cookies_from_auth_client(c)
            if sso:
                session_cookies = dict(session_cookies or {})
                session_cookies.setdefault("sso", sso)

            if oauth_async and _oauth_q is not None:
                _log(index, f"SSO ready → OAuth queue (cookies={len(session_cookies or {})})")
                _enqueue_oauth_job(
                    {
                        "index": index,
                        "email": email,
                        "password": password,
                        "sso": sso,
                        "session_cookies": session_cookies,
                        "proxy": proxy,
                        "oauth_protocol": oauth_protocol,
                        "oauth_debug": oauth_debug,
                        "cliproxyapi_auth_dir": (
                            str(cliproxyapi_auth_dir) if cliproxyapi_auth_dir else None
                        ),
                        "cliproxyapi_base_url": cliproxyapi_base_url,
                        "check_quota": check_quota,
                        "failed_auth_dir": (str(failed_auth_dir) if failed_auth_dir else None),
                        "check_quota_timeout": check_quota_timeout,
                        "email_channel": code_box.get("channel") or "",
                    }
                )
                result["oauth_queued"] = True
                # Registration thread frees here; OAuth worker records final result.
                return result

            # Inline (OAUTH_ASYNC=0): same thread, keep live auth_client for cookies.
            built = _finish_build_oauth(
                index=index,
                email=email,
                password=password,
                sso=sso,
                session_cookies=session_cookies,
                proxy=proxy,
                oauth_protocol=oauth_protocol,
                oauth_debug=oauth_debug,
                cliproxyapi_auth_dir=cliproxyapi_auth_dir,
                cliproxyapi_base_url=cliproxyapi_base_url,
                check_quota=check_quota,
                failed_auth_dir=failed_auth_dir,
                check_quota_timeout=check_quota_timeout,
                auth_client=c,
            )
            result.update(built)
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


def _result_is_success(r: dict, *, do_oauth: bool) -> bool:
    """Success for -n target: BUILD export if oauth on, else SSO cookie."""
    if do_oauth:
        return bool(r and r.get("cliproxyapi_auth"))
    return bool(r and r.get("sso"))


def _print_run_summary(
    *, check_quota: bool = False, do_oauth: bool = True, title: str = "Done"
) -> None:
    """Print per-account lines first, aggregate stats LAST (always at log bottom)."""
    with _results_lock:
        rows = list(_results)
    ok_sso = [r for r in rows if r.get("sso")]
    ok_build = [r for r in rows if r.get("cliproxyapi_auth")]
    denied = [r for r in rows if r.get("chat_endpoint_denied")]
    quota_fail = [
        r for r in rows if r.get("cliproxyapi_auth_failed") and not r.get("chat_endpoint_denied")
    ]
    fail = [
        r for r in rows if r.get("error") and not r.get("cliproxyapi_auth") and not r.get("sso")
    ]
    stopped = [r for r in rows if str(r.get("error") or "").startswith("stopped")]
    elapsed = time.time() - _t0 if _t0 else 0.0

    # Successes first (what user cares about), then failures (truncated errors).
    print(f"\n----- 账号明细 ({len(rows)}) -----", flush=True)
    if ok_build:
        print(f"  [BUILD OK] {len(ok_build)}", flush=True)
        for r in ok_build:
            rem = r.get("quota_remaining_tokens")
            extra = f"  tokens={rem}" if rem is not None else ""
            print(
                f"    + {r.get('email') or '?':40s}  {r.get('cliproxyapi_auth')}{extra}",
                flush=True,
            )
    if ok_sso and not ok_build:
        print(f"  [SSO only] {len(ok_sso)}", flush=True)
        for r in ok_sso:
            print(f"    + {r.get('email') or '?'}", flush=True)
    elif ok_sso:
        sso_only = [r for r in ok_sso if not r.get("cliproxyapi_auth")]
        if sso_only:
            print(f"  [SSO only / OAuth 未完成] {len(sso_only)}", flush=True)
            for r in sso_only:
                err = r.get("error")
                if err:
                    print(f"    ~ {r.get('email') or '?':40s}  {err}", flush=True)
                else:
                    print(f"    + {r.get('email') or '?'}", flush=True)
    if denied:
        print(f"  [DENIED] {len(denied)}", flush=True)
        for r in denied[:20]:
            print(
                f"    - {r.get('email') or '?':40s}  {r.get('error', '?')}",
                flush=True,
            )
        if len(denied) > 20:
            print(f"    ... +{len(denied) - 20} more", flush=True)
    if quota_fail:
        print(f"  [QUOTA FAIL] {len(quota_fail)}", flush=True)
        for r in quota_fail[:20]:
            print(
                f"    - {r.get('email') or '?':40s}  {r.get('error', '?')}",
                flush=True,
            )
        if len(quota_fail) > 20:
            print(f"    ... +{len(quota_fail) - 20} more", flush=True)

    # Collapse identical FAIL reasons so 98 lines don't bury the footer.
    fail_rows = [r for r in fail if not str(r.get("error") or "").startswith("stopped")]
    if fail_rows:
        from collections import Counter

        reasons: Counter[str] = Counter()
        for r in fail_rows:
            msg = str(r.get("error") or "?")
            # keep first line / first 120 chars as bucket key
            key = msg.split("\n", 1)[0].strip()
            if len(key) > 120:
                key = key[:117] + "..."
            reasons[key] += 1
        print(f"  [FAIL] {len(fail_rows)}", flush=True)
        for reason, n in reasons.most_common(12):
            print(f"    x{n:3d}  {reason}", flush=True)
        if len(reasons) > 12:
            print(f"    ... +{len(reasons) - 12} more reason buckets", flush=True)

    parts = [
        f"{title} in {elapsed:.0f}s",
        f"ok={len(ok_build) if _summary_ctx.get('do_oauth') else len(ok_sso)}/{_total or '?'}",
        f"attempts={len(rows)}",
        f"SSO OK: {len(ok_sso)}",
        f"BUILD OK: {len(ok_build)}",
    ]
    if check_quota:
        parts.append(f"DENIED: {len(denied)}")
        parts.append(f"QUOTA FAIL: {len(quota_fail)}")
    parts.append(f"FAIL: {len(fail_rows)}")
    if stopped:
        parts.append(f"STOPPED: {len(stopped)}")
    print(f"\n{'=' * 50}", flush=True)
    print("  |  ".join(parts), flush=True)
    print(f"{'=' * 50}", flush=True)
    queued = [r for r in rows if r.get("oauth_queued") and not r.get("cliproxyapi_auth")]
    if ok_build:
        print(f"  已注册 BUILD: {len(ok_build)}  （明细见上）", flush=True)
    elif ok_sso:
        print(f"  已拿到 SSO: {len(ok_sso)}（尚未 BUILD 导出）", flush=True)
    else:
        print("  本轮无成功账号", flush=True)
    if queued:
        print(f"  OAuth 队列残留(未完成): {len(queued)}", flush=True)


def main():
    global _total, _t0
    default_auth = str(default_cliproxyapi_auth_dir())
    p = argparse.ArgumentParser(
        description="grok-build-auth: x.ai register + SSO + Grok Build OAuth (CLIProxyAPI-ready)",
    )
    p.add_argument(
        "-n",
        "--count",
        type=int,
        default=1,
        help="目标成功账号数（一直尝试直到成功数达标或 Ctrl+C；非尝试次数）",
    )
    p.add_argument(
        "-t",
        "--threads",
        type=int,
        default=4,
        help="注册并发线程数（默认 4；OAuth 另见 --oauth-workers）",
    )
    p.add_argument(
        "--oauth-workers",
        type=int,
        default=0,
        help="Build OAuth 池线程数（默认 max(-t,2)；OAUTH_ASYNC=0 时忽略）",
    )
    p.add_argument(
        "--no-oauth-async",
        action="store_true",
        help="关闭 SSO→OAuth 拆分，注册线程内串行 Build",
    )
    p.add_argument(
        "-e",
        "--email",
        choices=cli_email_choices(),
        default="auto",
        help="邮箱渠道: auto=所有已配置渠道(优先+溢出) | 单渠道名(见 registry)",
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
    _install_sigint_handler()

    _total = max(1, int(args.count))
    _t0 = time.time()
    threads = max(1, int(args.threads))
    do_oauth = not args.no_oauth
    if args.no_oauth_async:
        os.environ["OAUTH_ASYNC"] = "0"
    oauth_async = _oauth_async_enabled(do_oauth)
    check_quota = bool(args.check_quota) and do_oauth
    _summary_ctx["check_quota"] = check_quota
    _summary_ctx["do_oauth"] = do_oauth
    oauth_workers_n = int(args.oauth_workers or 0)
    if oauth_workers_n <= 0:
        oauth_workers_n = max(2, threads) if oauth_async else 0
    if args.check_quota and not do_oauth:
        print("warn: --check-quota ignored with --no-oauth", file=sys.stderr)

    failed_auth_dir = (args.failed_auth_dir or "").strip()
    if check_quota and not failed_auth_dir:
        failed_auth_dir = str(_default_failed_auth_dir(args.cliproxyapi_auth_dir))

    solver_mode = (os.environ.get("TURNSTILE_SOLVER") or "auto").strip().lower()
    ts_label = solver_mode if solver_mode else "auto"
    use_pool = _pool_enabled(solver_mode)
    pool_size, pool_target, pool_minters = suggest_pool_params(threads, solver_mode=solver_mode)
    use_mail_pool = _mail_pool_enabled()
    mail_channels = resolve_channels(args.email)
    mail_size, mail_target, mail_minters = suggest_mail_pool_params(threads)
    print(
        f"grok-build-auth: target {_total} success, reg-threads={threads}, email={args.email}, "
        f"oauth={'on' if do_oauth else 'off'}"
        f", oauth-async={'on' if oauth_async else 'off'}"
        f"{('/workers=' + str(oauth_workers_n)) if oauth_async else ''}"
        f", turnstile={ts_label}"
        f", pool={'on' if use_pool else 'off'}"
        f", mail-pool={'on' if use_mail_pool else 'off'}"
        f", check-quota={'on' if check_quota else 'off'}"
    )
    if len(mail_channels) == 1:
        print(f"  mail-channels:        {mail_channels[0]} (solo, full wait/retry)")
    else:
        print(
            f"  mail-channels:        {','.join(mail_channels)} "
            f"(prefer+overflow; weights via MAIL_CHANNEL_WEIGHTS)"
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
    if use_mail_pool:
        print(
            f"  mail-pool:            size={mail_size} target={mail_target} "
            f"minters={mail_minters} "
            f"max_age={os.environ.get('MAIL_POOL_MAX_AGE') or '600'}s "
            f"(auto from -t={threads})"
        )
    if not use_pool:
        par = _turnstile_parallel(threads)
        print(f"  turnstile-parallel:   {par} (pool off; auto from -t={threads})")

    global _token_pool, _mail_pool, _mail_router, _shared_solver, _turnstile_lock, _proxy_pool

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
        print("  proxy:                single (HTTPS_PROXY/HTTP_PROXY)")
    print()

    _token_pool = None
    _mail_pool = None
    _mail_router = None
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

    def _mail_log(msg: str) -> None:
        print(f"  [mail] {msg}", flush=True)

    _mail_router = _build_mail_router(mail_channels, log=_mail_log)
    if use_mail_pool:
        _mail_pool = MailboxPool(
            _mail_router,
            size=mail_size,
            target=mail_target,
            minters=mail_minters,
            log=lambda m: print(f"  [mail-pool] {m}", flush=True),
        )
        _mail_pool.start()
    try:
        accounts_dir = (args.accounts_output_dir or "").strip() or None
        common_kwargs: dict[str, Any] = dict(
            do_oauth=do_oauth,
            oauth_async=oauth_async,
            oauth_protocol=not args.no_oauth_protocol,
            oauth_debug=args.oauth_debug,
            cliproxyapi_auth_dir=args.cliproxyapi_auth_dir,
            cliproxyapi_base_url=args.cliproxyapi_base_url,
            accounts_output_dir=accounts_dir,
            check_quota=check_quota,
            failed_auth_dir=failed_auth_dir or None,
            check_quota_timeout=args.check_quota_timeout,
        )

        def _record(result: dict) -> None:
            global _ok
            # Queued handoff is not a terminal row — OAuth worker records the real one.
            if result and result.get("oauth_queued") and not result.get("cliproxyapi_auth"):
                return
            with _results_lock:
                _results.append(result)
                if _result_is_success(result, do_oauth=do_oauth):
                    _ok += 1
                    ok_now = _ok
                else:
                    ok_now = _ok
            if ok_now >= _total and not _stop.is_set():
                request_stop("target-reached")

        global _oauth_q, _oauth_workers
        _oauth_q = None
        _oauth_workers = []
        if oauth_async:
            _oauth_q = queue.Queue()
            for wi in range(oauth_workers_n):
                t = threading.Thread(
                    target=_oauth_worker_loop,
                    args=(_record,),
                    name=f"oauth-{wi + 1}",
                    daemon=True,
                )
                t.start()
                _oauth_workers.append(t)
            print(
                f"  oauth-pool:            workers={oauth_workers_n} (session-reuse first)",
                flush=True,
            )

        next_index = 1
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futures: dict = {}

            def _submit_one() -> None:
                nonlocal next_index
                if _stop.is_set():
                    return
                with _results_lock:
                    if _ok >= _total:
                        return
                idx = next_index
                next_index += 1
                futures[ex.submit(register_one, idx, args.email, **common_kwargs)] = idx

            for _ in range(threads):
                _submit_one()

            while futures:
                if _stop.is_set():
                    for f in list(futures):
                        f.cancel()
                    done, _not_done = wait(set(futures), timeout=2.0)
                    for f in done:
                        futures.pop(f, None)
                        try:
                            _record(f.result(timeout=0))
                        except Exception as exc:
                            _record({"error": f"stopped: {exc}", "email": None})
                    break
                done, _pending = wait(set(futures), timeout=0.5, return_when=FIRST_COMPLETED)
                for f in done:
                    futures.pop(f, None)
                    try:
                        _record(f.result())
                    except Exception as exc:
                        _record({"error": str(exc), "email": None})
                    with _results_lock:
                        need_more = _ok < _total and not _stop.is_set()
                    if need_more:
                        _submit_one()
    finally:
        # Drain OAuth pool: wait for queued SSO→Build, then stop workers.
        if _oauth_q is not None:
            if not _stop.is_set() or _ok < _total:
                # Normal finish or target-reached: finish in-flight OAuth.
                deadline = time.time() + 180.0
                while time.time() < deadline:
                    with _oauth_inflight_lock:
                        inflight = _oauth_inflight
                        empty = _oauth_q.empty()
                    if empty and inflight <= 0:
                        break
                    time.sleep(0.2)
            for _ in _oauth_workers:
                _oauth_q.put(None)
            for t in _oauth_workers:
                t.join(timeout=30.0)
            _oauth_workers = []
            _oauth_q = None
        if _mail_pool is not None:
            _mail_pool.stop(wait=2.0)
            _mail_pool = None
        _mail_router = None
        if _token_pool is not None:
            _token_pool.stop(wait=2.0)
            _token_pool = None

    # summary (also printed on Ctrl+C via request_stop / forced exit)
    title = "Done"
    if _stop.is_set():
        # target-reached is success stop; Ctrl+C / other reasons stay Stopped
        # request_stop sets event only; distinguish via ok count
        title = "Done" if _ok >= _total else "Stopped"
    _print_run_summary(check_quota=check_quota, do_oauth=do_oauth, title=title)


if __name__ == "__main__":
    main()
