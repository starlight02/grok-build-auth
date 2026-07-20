# -*- coding: utf-8 -*-
"""Turnstile solvers for the x.ai protocol client.

Local browser backend (same ``solve_turnstile`` surface):

**browser** (default) — Playwright launches Chromium/Chrome locally.
Terminal-friendly defaults: ``headless=new`` + system Chrome when available
(stock headless Chromium is often CF-blocked on accounts.x.ai).

Set ``TURNSTILE_HEADLESS=0`` for a visible window, or ``TURNSTILE_INTERACTIVE=1``
to click the checkbox manually.

Registration/OAuth HTTP stays pure protocol; only the widget is solved in a
browser page. Per-thread browser connections stay warm by default (reuse).

Env:

- ``TURNSTILE_SOLVER`` — browser | local | playwright | free | chromium | chrome
- ``TURNSTILE_HEADLESS`` — 1/true for headless (default 1; CF-friendly with Chrome)
- ``TURNSTILE_BROWSER_CHANNEL`` — e.g. chrome (auto-selected when headless + Chrome installed)
- ``TURNSTILE_TIMEOUT`` — seconds (default 60)
- ``TURNSTILE_INTERACTIVE`` — 1=wait for manual checkbox click (forces headed)
- ``TURNSTILE_BROWSER_REUSE`` — 1=keep per-thread browser warm (default 1)
- ``TURNSTILE_BROWSER_CONCURRENCY`` — secondary parallel-solve cap (default 4; ``run.py`` also serializes Turnstile)
- ``HTTPS_PROXY`` / ``HTTP_PROXY`` — browser proxy
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import sys
import threading
import time
from typing import Any, Optional, Protocol, runtime_checkable

_browser_solve_sem: Optional[threading.Semaphore] = None
_browser_sem_init = threading.Lock()
# Cold launch+close every local solve is the free-path bottleneck;
# keep one browser warm per worker thread by default.
_tls = threading.local()
_tls_registry_lock = threading.Lock()
_tls_browsers: list[Any] = []


def _browser_concurrency() -> int:
    """Max parallel Playwright Turnstile browsers (env TURNSTILE_BROWSER_CONCURRENCY)."""
    raw = (os.environ.get("TURNSTILE_BROWSER_CONCURRENCY") or "4").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 4
    return max(1, min(n, 32))


def _browser_reuse_enabled() -> bool:
    # Defined before _env_truthy exists at import time for call-sites only.
    raw = (os.environ.get("TURNSTILE_BROWSER_REUSE") or "1").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on", ""}:
        return True
    return True


def _get_browser_solve_sem() -> threading.Semaphore:
    """Process-wide semaphore so -t N can open N browsers (not forced serial)."""
    global _browser_solve_sem
    with _browser_sem_init:
        if _browser_solve_sem is None:
            _browser_solve_sem = threading.Semaphore(_browser_concurrency())
        return _browser_solve_sem


def _close_thread_browsers() -> None:
    with _tls_registry_lock:
        items = list(_tls_browsers)
        _tls_browsers.clear()
    for item in items:
        browser = item.get("browser")
        pw = item.get("pw")
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if pw is not None:
                pw.stop()
        except Exception:
            pass


atexit.register(_close_thread_browsers)


@runtime_checkable
class TurnstileSolver(Protocol):
    def solve_turnstile(
        self,
        website_url: str,
        website_key: str,
        *,
        premium: bool = False,
    ) -> str: ...


# --------------------------------------------------------------------------- #
# Local browser (free)
# --------------------------------------------------------------------------- #


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def _proxy_from_env(explicit: str = "") -> str:
    return (
        (explicit or "").strip()
        or (os.environ.get("HTTPS_PROXY") or "").strip()
        or (os.environ.get("HTTP_PROXY") or "").strip()
        or (os.environ.get("https_proxy") or "").strip()
        or (os.environ.get("http_proxy") or "").strip()
    )


def _playwright_proxy(proxy: str) -> Optional[dict[str, str]]:
    proxy = (proxy or "").strip()
    if not proxy:
        return None
    # Playwright wants server= scheme://host:port; optional user/pass.
    m = re.match(
        r"^(?P<scheme>https?|socks5)://(?:(?P<user>[^:@]+):(?P<pw>[^@]*)@)?"
        r"(?P<host>[^:/]+)(?::(?P<port>\d+))?/?$",
        proxy,
        re.I,
    )
    if not m:
        return {"server": proxy}
    scheme = m.group("scheme")
    host = m.group("host")
    port = m.group("port")
    server = f"{scheme}://{host}" + (f":{port}" if port else "")
    out: dict[str, str] = {"server": server}
    if m.group("user"):
        out["username"] = m.group("user")
        out["password"] = m.group("pw") or ""
    return out


def _system_chrome_available() -> bool:
    """True when Playwright channel=chrome is likely to work on this host."""
    if sys.platform == "darwin":
        return os.path.exists("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if sys.platform.startswith("linux"):
        return bool(
            shutil.which("google-chrome")
            or shutil.which("google-chrome-stable")
            or shutil.which("chromium-browser")
            or shutil.which("chromium")
        )
    if sys.platform == "win32":
        for base in (
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            if not base:
                continue
            candidate = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.isfile(candidate):
                return True
        return False
    return False


def _default_browser_channel(*, headless: bool) -> Optional[str]:
    """Prefer system Chrome for headless — stock Chromium is CF-blocked more often."""
    raw = os.environ.get("TURNSTILE_BROWSER_CHANNEL")
    if raw is not None:
        # Explicit empty disables channel; non-empty wins.
        return raw.strip() or None
    if headless and _system_chrome_available():
        return "chrome"
    return None


def _stealth_user_agent() -> str:
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )


def _stealth_init_script() -> str:
    # Minimal automation leak patch — enough for managed Turnstile on accounts.x.ai.
    return "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


def _chromium_launch_kwargs(
    *,
    headless: bool,
    channel: Optional[str],
    proxy: str,
) -> dict[str, Any]:
    args = ["--disable-blink-features=AutomationControlled"]
    if headless:
        # Playwright's default headless shell is CF-blocked; force Chrome's new headless.
        args.append("--headless=new")
    kwargs: dict[str, Any] = {
        "headless": bool(headless),
        "args": args,
        "ignore_default_args": ["--enable-automation"],
    }
    if channel:
        kwargs["channel"] = channel
    proxy_cfg = _playwright_proxy(proxy)
    if proxy_cfg:
        kwargs["proxy"] = proxy_cfg
    return kwargs


class LocalBrowserTurnstileSolver:
    """Solve Turnstile with a local Chromium/Chrome via Playwright.

    Browser is only used to mint the widget token; the rest of signup/OAuth
    stays on the HTTP protocol client.

    Performance: by default each worker thread keeps one browser connection
    warm (``TURNSTILE_BROWSER_REUSE=1``). Only the page is created/closed
    per solve.

    Headed / interactive:
      - ``TURNSTILE_HEADLESS=0`` → visible Chrome window
      - ``TURNSTILE_INTERACTIVE=1`` → wait for manual Turnstile click (forces headed)

    Strategies (in order):
      1) Load real website_url, force turnstile.render, poll token
      2) Fallback on-page widget poll
      3) If TURNSTILE_INTERACTIVE=1: leave window open for manual click
    """

    def __init__(
        self,
        *,
        engine: Optional[str] = None,
        headless: Optional[bool] = None,
        timeout: Optional[float] = None,
        proxy: str = "",
        channel: Optional[str] = None,
        debug: bool = False,
        interactive: Optional[bool] = None,
        **_ignored: Any,
    ):
        eng = (engine or "chromium").strip().lower()
        if eng in {"browser", "local", "playwright", "free", "chrome", "auto", ""}:
            eng = "chromium"
        if eng in {"obscura", "obscura-cdp", "cdp"}:
            raise ValueError(
                "TURNSTILE_SOLVER=obscura has been removed; use browser "
                "(Chrome headless=new, or TURNSTILE_HEADLESS=0 / TURNSTILE_INTERACTIVE=1)."
            )
        if eng != "chromium":
            raise ValueError(f"Unknown browser engine {engine!r}; use browser / chromium / chrome")
        self._engine = eng

        if headless is None:
            # Terminal-friendly default: headless Chrome (new) works on accounts.x.ai.
            headless = _env_truthy("TURNSTILE_HEADLESS", default=True)
        if timeout is None:
            try:
                # 60s is enough for managed Turnstile on accounts.x.ai; longer
                # values make concurrent workers pile up behind a stuck solve.
                timeout = float(os.environ.get("TURNSTILE_TIMEOUT") or "60")
            except ValueError:
                timeout = 60.0
        if interactive is None:
            interactive = _env_truthy("TURNSTILE_INTERACTIVE", default=False)

        self._headless = bool(headless)
        self._timeout = float(timeout)
        self._proxy = _proxy_from_env(proxy)
        # Explicit constructor channel wins; else env / auto (system Chrome when headless).
        if channel is not None:
            self._channel = (channel or "").strip() or None
        else:
            self._channel = _default_browser_channel(headless=self._headless)
        self._debug = debug
        # Interactive implies headed Chromium.
        self._interactive = bool(interactive)
        if self._interactive:
            self._headless = False
            # Re-resolve channel after forcing headed (auto-chrome only for headless).
            if channel is None and os.environ.get("TURNSTILE_BROWSER_CHANNEL") is None:
                self._channel = _default_browser_channel(headless=False)

    def _log(self, msg: str) -> None:
        if self._debug:
            print(f"  [BrowserTurnstile] {msg}")

    def _launch_fingerprint(self) -> tuple:
        return (
            "chromium",
            self._headless,
            self._proxy or "",
            self._channel or "",
        )

    def _drop_thread_browser(self) -> None:
        slot = getattr(_tls, "slot", None)
        if not slot:
            return
        _tls.slot = None
        with _tls_registry_lock:
            try:
                _tls_browsers.remove(slot)
            except ValueError:
                pass
        try:
            if slot.get("browser") is not None:
                slot["browser"].close()
        except Exception:
            pass
        try:
            if slot.get("pw") is not None:
                slot["pw"].stop()
        except Exception:
            pass

    def _new_context_kwargs(self) -> dict[str, Any]:
        """Realistic browser context — bare defaults trip CF headless detection."""
        return {
            "user_agent": _stealth_user_agent(),
            "viewport": {"width": 1280, "height": 800},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "color_scheme": "light",
        }

    def _prepare_page(self, page: Any, *, context: Any = None) -> Any:
        try:
            page.add_init_script(_stealth_init_script())
        except Exception as exc:
            self._log(f"init script skip: {exc}")
        # Stash context so callers can close it with the page (avoid warm-path leaks).
        if context is not None:
            try:
                page._xai_context = context  # type: ignore[attr-defined]
            except Exception:
                pass
        return page

    def _close_page(self, page: Any) -> None:
        ctx = getattr(page, "_xai_context", None)
        try:
            page.close()
        except Exception:
            pass
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass

    def _new_page(self, browser: Any) -> Any:
        """Create a stealth page; CDP browsers may lack browser.new_page()."""
        ctx_kwargs = self._new_context_kwargs()
        # Prefer a fresh context so each solve has clean cookies/storage.
        try:
            ctx = browser.new_context(**ctx_kwargs)
            return self._prepare_page(ctx.new_page(), context=ctx)
        except Exception as exc:
            self._log(f"new_context failed ({exc}); fallback")
        try:
            page = browser.new_page(**ctx_kwargs)
            return self._prepare_page(page)
        except TypeError:
            # Older CDP bindings may not accept context kwargs on new_page.
            try:
                return self._prepare_page(browser.new_page())
            except Exception:
                pass
        except Exception:
            pass
        contexts = list(getattr(browser, "contexts", None) or [])
        if contexts:
            try:
                return self._prepare_page(contexts[0].new_page())
            except Exception:
                pass
        ctx = browser.new_context(**ctx_kwargs)
        return self._prepare_page(ctx.new_page(), context=ctx)

    def _launch_chromium(self, pw: Any) -> Any:
        """Launch Chromium/Chrome with terminal-friendly headless defaults."""
        launch_kwargs = _chromium_launch_kwargs(
            headless=self._headless,
            channel=self._channel,
            proxy=self._proxy,
        )
        try:
            browser = pw.chromium.launch(**launch_kwargs)
        except Exception as exc:
            if self._channel:
                self._log(f"channel={self._channel} failed ({exc}); retry bundled chromium")
                launch_kwargs = _chromium_launch_kwargs(
                    headless=self._headless,
                    channel=None,
                    proxy=self._proxy,
                )
                try:
                    browser = pw.chromium.launch(**launch_kwargs)
                except Exception:
                    raise RuntimeError(
                        "Failed to launch browser for Turnstile. "
                        "Install Google Chrome or run: playwright install chromium"
                    ) from exc
            else:
                raise RuntimeError(
                    "Failed to launch browser for Turnstile. "
                    "Install Google Chrome or run: playwright install chromium"
                ) from exc
        mode = "headless=new" if self._headless else "headed"
        ch = self._channel or "chromium"
        self._log(f"warm {ch} launched ({mode}, thread-local reuse)")
        return browser

    def _ensure_thread_browser(self) -> Any:
        """Return a warm browser for this thread (Playwright sync is TLS-bound)."""
        from playwright.sync_api import sync_playwright

        fp = self._launch_fingerprint()
        slot = getattr(_tls, "slot", None)
        if slot and slot.get("fp") == fp and slot.get("browser") is not None:
            try:
                _ = slot["browser"].contexts
                return slot["browser"]
            except Exception:
                self._log("warm browser dead; relaunching")
                self._drop_thread_browser()

        pw = sync_playwright().start()
        try:
            browser = self._launch_chromium(pw)
        except Exception:
            try:
                pw.stop()
            except Exception:
                pass
            raise

        slot = {"pw": pw, "browser": browser, "fp": fp, "engine": self._engine}
        _tls.slot = slot
        with _tls_registry_lock:
            _tls_browsers.append(slot)
        return browser

    def _mint_token_on_page(self, page: Any, url: str, key: str, timeout_ms: int) -> str:
        token = self._solve_force_render(page, url, key, timeout_ms)
        if not token:
            token = self._solve_on_target_page(page, url, key, timeout_ms)
        if not token and self._interactive:
            self._log(f"waiting for manual Turnstile click (up to {self._timeout:.0f}s)...")
            print("  [BrowserTurnstile] 请在弹出的浏览器里完成 Turnstile 验证…")
            token = self._wait_token(page, timeout_ms)
        return token or ""

    def solve_turnstile(
        self,
        website_url: str,
        website_key: str,
        *,
        premium: bool = False,  # noqa: ARG002 — protocol surface parity
    ) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Local Turnstile solver needs Playwright. Install free deps:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            ) from exc

        url = (website_url or "").strip()
        key = (website_key or "").strip()
        if not url or not key:
            raise ValueError("website_url and website_key are required")

        timeout_ms = int(max(self._timeout, 15.0) * 1000)
        proxy_cfg = _playwright_proxy(self._proxy)
        reuse = _browser_reuse_enabled()
        eng = self._engine
        self._log(
            f"open {url} sitekey={key[:16]}... engine={eng} "
            f"headless={self._headless} interactive={self._interactive} "
            f"proxy={'yes' if proxy_cfg else 'no'} "
            f"channel={self._channel or eng} reuse={reuse}"
        )

        with _get_browser_solve_sem():
            if reuse:
                # Warm path: one browser per worker thread, new page per solve.
                last_err: Optional[BaseException] = None
                for attempt in range(2):
                    try:
                        browser = self._ensure_thread_browser()
                        page = self._new_page(browser)
                        try:
                            token = self._mint_token_on_page(page, url, key, timeout_ms)
                        finally:
                            self._close_page(page)
                        if token:
                            self._log(f"token ok len={len(token)} (warm/{eng})")
                            return token
                        last_err = RuntimeError("empty token")
                    except Exception as exc:
                        last_err = exc
                        self._log(f"warm solve attempt {attempt + 1} failed: {exc}")
                        self._drop_thread_browser()
                hint = (
                    "Need Google Chrome for terminal headless. "
                    "Or: TURNSTILE_HEADLESS=0 TURNSTILE_BROWSER_CHANNEL=chrome, "
                    "TURNSTILE_INTERACTIVE=1, HTTPS_PROXY=…"
                )
                raise RuntimeError(
                    f"Local browser ({eng}) did not produce a Turnstile token (warm path). {hint}"
                ) from last_err

            # Cold path: launch + close every solve.
            with sync_playwright() as p:
                browser = self._launch_chromium(p)

                try:
                    page = self._new_page(browser)
                    try:
                        token = self._mint_token_on_page(page, url, key, timeout_ms)
                    finally:
                        self._close_page(page)
                    if not token:
                        raise RuntimeError(
                            f"Local browser ({eng}) did not produce a Turnstile token. "
                            "Need Google Chrome for terminal headless. "
                            "Or: TURNSTILE_HEADLESS=0, "
                            "TURNSTILE_INTERACTIVE=1, "
                            "TURNSTILE_BROWSER_CHANNEL=chrome, HTTPS_PROXY=…"
                        )
                    self._log(f"token ok len={len(token)} (cold/{eng})")
                    return token
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass

    def _extract_token_js(self) -> str:
        return """() => {
            const pick = (v) => (v && String(v).length > 20 ? String(v) : '');
            const names = [
              'input[name="cf-turnstile-response"]',
              'input[name="g-recaptcha-response"]',
              'textarea[name="cf-turnstile-response"]',
              'textarea[name="g-recaptcha-response"]',
            ];
            for (const sel of names) {
              for (const el of document.querySelectorAll(sel)) {
                const t = pick(el.value);
                if (t) return t;
              }
            }
            try {
              if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                const t = pick(window.turnstile.getResponse());
                if (t) return t;
                // multi-widget: try empty widget id
                try {
                  const t2 = pick(window.turnstile.getResponse(''));
                  if (t2) return t2;
                } catch (e) {}
              }
            } catch (e) {}
            // data-callback may have stashed token
            if (window.__xaiTsToken) return pick(window.__xaiTsToken);
            return '';
        }"""

    def _wait_token(self, page: Any, timeout_ms: int) -> str:
        deadline = time.time() + timeout_ms / 1000.0
        js = self._extract_token_js()
        while time.time() < deadline:
            try:
                token = page.evaluate(js) or ""
            except Exception:
                token = ""
            if token and len(token) > 20:
                return str(token)
            # Also scan child frames for response fields
            try:
                for frame in page.frames:
                    if frame == page.main_frame:
                        continue
                    try:
                        token = frame.evaluate(js) or ""
                    except Exception:
                        token = ""
                    if token and len(token) > 20:
                        return str(token)
            except Exception:
                pass
            page.wait_for_timeout(400)
        return ""

    def _click_turnstile_widget(self, page: Any) -> None:
        """Best-effort click on Turnstile checkbox (often inside iframe)."""
        # Outer hosts
        for sel in (
            ".cf-turnstile",
            "[data-sitekey]",
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[src*='turnstile']",
            "iframe[title*='Widget']",
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.scroll_into_view_if_needed(timeout=2000)
                box = loc.bounding_box()
                if box:
                    # Click center of widget / iframe
                    page.mouse.click(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
                    self._log(f"clicked host {sel}")
                    return
                loc.click(timeout=2000, force=True)
                self._log(f"clicked {sel}")
                return
            except Exception as exc:
                self._log(f"click {sel} skip: {exc}")

        # Frame locator checkbox (structure varies)
        try:
            fl = page.frame_locator(
                "iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile']"
            )
            for inner in (
                "input[type='checkbox']",
                "body",
                "#challenge-stage",
                "label",
            ):
                try:
                    fl.locator(inner).first.click(timeout=2000)
                    self._log(f"clicked frame {inner}")
                    return
                except Exception:
                    continue
        except Exception as exc:
            self._log(f"frame click skip: {exc}")

    def _solve_force_render(
        self, page: Any, website_url: str, website_key: str, timeout_ms: int
    ) -> str:
        """Load real origin, ensure turnstile API, explicit render + poll.

        Validated against accounts.x.ai: render() returns a widget id and
        getResponse()/callback yields ~800 char tokens via the local browser.
        """
        try:
            page.goto(
                website_url,
                wait_until="domcontentloaded",
                timeout=min(timeout_ms, 90000),
            )
        except Exception as exc:
            self._log(f"force-render goto failed: {exc}")
            return ""

        # Short settle only — long fixed sleeps were free-path dead weight.
        page.wait_for_timeout(400)
        # Cookie banners can cover widgets
        for sel in (
            "#onetrust-accept-btn-handler",
            "button#onetrust-accept-btn-handler",
        ):
            try:
                page.locator(sel).first.click(timeout=800)
                page.wait_for_timeout(200)
                break
            except Exception:
                pass

        # Only click email path if turnstile API is not already present.
        try:
            has_ts = bool(page.evaluate("() => !!(window.turnstile && window.turnstile.render)"))
        except Exception:
            has_ts = False
        if not has_ts:
            for text in ("使用邮箱注册", "Sign up with email", "Continue with email"):
                try:
                    page.get_by_text(text, exact=False).first.click(timeout=1200)
                    page.wait_for_timeout(400)
                    break
                except Exception:
                    pass

        # Wait for turnstile global (preloaded by the page or inject)
        try:
            page.wait_for_function(
                "() => !!(window.turnstile && window.turnstile.render)",
                timeout=min(20000, timeout_ms),
            )
        except Exception:
            self._log("turnstile global missing; inject api.js")
            try:
                page.evaluate(
                    """() => new Promise((resolve) => {
                      if (window.turnstile && window.turnstile.render) {
                        resolve(true); return;
                      }
                      const s = document.createElement('script');
                      s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
                      s.async = true;
                      s.onload = () => resolve(true);
                      s.onerror = () => resolve(false);
                      document.head.appendChild(s);
                    })"""
                )
                page.wait_for_timeout(1000)
            except Exception as exc:
                self._log(f"inject api.js failed: {exc}")
                return ""

        # Explicit render into a fixed host (proven path on accounts.x.ai)
        try:
            render_meta = page.evaluate(
                """(sitekey) => {
                  window.__xaiTsToken = '';
                  const hostId = 'xai-force-ts-host';
                  let host = document.getElementById(hostId);
                  if (!host) {
                    host = document.createElement('div');
                    host.id = hostId;
                    host.style.cssText =
                      'width:300px;height:80px;position:fixed;top:100px;left:20px;'
                      + 'z-index:999999;background:#fff;';
                    document.body.appendChild(host);
                  }
                  // hidden field some pages read
                  let inp = document.querySelector('input[name="cf-turnstile-response"]');
                  if (!inp) {
                    inp = document.createElement('input');
                    inp.type = 'hidden';
                    inp.name = 'cf-turnstile-response';
                    document.body.appendChild(inp);
                  }
                  try {
                    const id = window.turnstile.render(host, {
                      sitekey,
                      theme: 'light',
                      size: 'normal',
                      appearance: 'always',
                      callback: (tok) => {
                        window.__xaiTsToken = tok || '';
                        inp.value = tok || '';
                      },
                      'error-callback': () => {},
                      'expired-callback': () => { window.__xaiTsToken = ''; },
                    });
                    return { ok: true, id: String(id) };
                  } catch (e) {
                    return { ok: false, err: String(e) };
                  }
                }""",
                website_key,
            )
            self._log(f"force render: {render_meta}")
        except Exception as exc:
            self._log(f"force render evaluate failed: {exc}")
            return ""

        # Give managed challenge time to auto-pass (observed ~1-12s)
        page.wait_for_timeout(1500)

        # Prefer callback / getResponse polling
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            try:
                token = page.evaluate(
                    """() => {
                      const pick = (v) =>
                        v && String(v).length > 20 ? String(v) : '';
                      if (window.__xaiTsToken) return pick(window.__xaiTsToken);
                      try {
                        if (window.turnstile && window.turnstile.getResponse) {
                          const t = pick(window.turnstile.getResponse());
                          if (t) return t;
                        }
                      } catch (e) {}
                      const el = document.querySelector(
                        'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
                      );
                      if (el) return pick(el.value);
                      return '';
                    }"""
                )
            except Exception:
                token = ""
            if token and len(token) > 20:
                return str(token)
            page.wait_for_timeout(400)
        return ""

    def _solve_on_target_page(
        self, page: Any, website_url: str, website_key: str, timeout_ms: int
    ) -> str:
        try:
            page.goto(
                website_url,
                wait_until="domcontentloaded",
                timeout=min(timeout_ms, 90000),
            )
        except Exception as exc:
            self._log(f"target goto failed: {exc}")
            return ""

        # Wait a bit for CF scripts
        page.wait_for_timeout(1500)
        self._click_turnstile_widget(page)
        page.wait_for_timeout(800)
        self._click_turnstile_widget(page)

        # First poll (managed challenge may auto-pass)
        half = max(timeout_ms // 3, 15000)
        token = self._wait_token(page, half)
        if token:
            return token

        # Try explicit render if page never mounted widget
        try:
            page.evaluate(
                """(sitekey) => {
                  window.__xaiTsToken = '';
                  if (!window.turnstile || !sitekey) return 'no-turnstile';
                  let host = document.querySelector('#xai-ts-host');
                  if (!host) {
                    host = document.createElement('div');
                    host.id = 'xai-ts-host';
                    host.style.cssText = 'margin:16px;min-height:65px;';
                    document.body.prepend(host);
                  }
                  try {
                    window.turnstile.render('#xai-ts-host', {
                      sitekey,
                      callback: (t) => { window.__xaiTsToken = t || ''; },
                    });
                    return 'rendered';
                  } catch (e) {
                    return 'err:' + String(e);
                  }
                }""",
                website_key,
            )
            page.wait_for_timeout(1000)
            self._click_turnstile_widget(page)
        except Exception as exc:
            self._log(f"explicit render skip: {exc}")

        return self._wait_token(page, max(timeout_ms - half, 10000))


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def resolve_turnstile_solver(
    *,
    backend: Optional[str] = None,
    proxy: str = "",
    debug: bool = False,
    headless: Optional[bool] = None,
    timeout: Optional[float] = None,
    interactive: Optional[bool] = None,
    **_ignored: Any,
) -> TurnstileSolver:
    """Return a Turnstile solver.

    ``backend`` / ``TURNSTILE_SOLVER``:
      - auto (default) → DrissionPage+turnstilePatch if installed, else Playwright
      - drission|dp|clean → DrissionPage + turnstilePatch (terminal CLI; bulk path)
      - camoufox|camou → Camoufox anti-detect Firefox (optional)
      - safari|webkit-system → macOS system Safari via Apple Events
      - browser|local|playwright|free|chromium|chrome → Playwright

    The bulk registrations that produced sso_output/ used **drission** (or auto→drission).
    Playwright ``browser`` is kept as fallback; CF may reject it when bot-score is high.
    """
    mode = (backend or os.environ.get("TURNSTILE_SOLVER") or "auto").strip().lower()

    if mode in {"yescaptcha", "yes", "paid", "capsolver", "2captcha", "anticaptcha"}:
        raise ValueError(
            f"TURNSTILE_SOLVER={mode!r} is not supported; use auto/drission/browser "
            "(local solvers only)."
        )
    if mode in {"obscura", "obscura-cdp", "cdp"}:
        raise ValueError("TURNSTILE_SOLVER=obscura has been removed; use auto/drission/browser.")

    def _drission() -> TurnstileSolver:
        from xconsole_client.drission_solver import DrissionTurnstileSolver

        return DrissionTurnstileSolver(
            proxy=proxy,
            debug=debug,
            headless=headless,
            timeout=timeout,
        )

    def _camoufox() -> TurnstileSolver:
        from xconsole_client.camoufox_solver import CamoufoxTurnstileSolver

        return CamoufoxTurnstileSolver(
            proxy=proxy,
            debug=debug,
            headless=headless,
            timeout=timeout,
        )

    def _playwright() -> TurnstileSolver:
        return LocalBrowserTurnstileSolver(
            engine="chromium",
            headless=headless,
            timeout=timeout,
            proxy=proxy,
            debug=debug,
            interactive=interactive,
        )

    if mode in {"drission", "dp", "clean", "drissionpage"}:
        return _drission()
    if mode in {"camoufox", "camou", "camoufox-firefox"}:
        return _camoufox()
    if mode in {"safari", "webkit-system", "system-safari"}:
        from xconsole_client.safari_solver import SafariTurnstileSolver

        return SafariTurnstileSolver(
            proxy=proxy,
            debug=debug,
            headless=headless,
            timeout=timeout,
            interactive=interactive,
        )
    if mode in {
        "browser",
        "local",
        "playwright",
        "free",
        "chromium",
        "chrome",
    }:
        return _playwright()
    if mode in {"", "auto"}:
        # Prefer Drission when available — this is the path that bulk-registered.
        try:
            import DrissionPage  # noqa: F401

            return _drission()
        except Exception:
            return _playwright()
    raise ValueError(
        f"Unknown TURNSTILE_SOLVER={mode!r}; use auto | drission | camoufox | safari | browser"
    )


def create_solver(*_args: Any, **kwargs: Any) -> TurnstileSolver:
    """Factory for Turnstile solvers (multi-backend)."""
    debug = bool(kwargs.pop("debug", False))
    timeout = kwargs.pop("timeout", None)
    proxy = kwargs.pop("proxy", "") or ""
    backend = kwargs.pop("backend", None)
    headless = kwargs.pop("headless", None)
    interactive = kwargs.pop("interactive", None)
    engine = kwargs.pop("engine", None)
    # Drop unused legacy kwargs (older call sites may still pass them).
    kwargs.pop("api_key", None)
    kwargs.pop("endpoint", None)
    kwargs.pop("poll_interval", None)
    if engine and not backend:
        backend = engine
    return resolve_turnstile_solver(
        backend=backend,
        proxy=proxy,
        debug=debug,
        headless=headless,
        timeout=timeout,
        interactive=interactive,
    )
