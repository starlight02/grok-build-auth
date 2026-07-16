# -*- coding: utf-8 -*-
"""DrissionPage + turnstilePatch Turnstile solver (terminal-friendly).

Mirrors Safari's *warm page* mint model:
  - real Chrome via DrissionPage (not Playwright CDP headless shell)
  - load ``turnstilePatch/`` MV2 extension (stealth)
  - navigate signup URL **once** per worker; later mints only force-render
    a fresh widget + CDP click (no full page reload / re-download)
  - CDP ``Input.dispatchMouseEvent`` — no OS mouse/focus steal after launch

Default is **headed** Chrome (minimized + off-screen). Headless is often
CF-blocked; set ``TURNSTILE_HEADLESS=1`` only if your IP tolerates it.

Env extras:
  TURNSTILE_MINIMIZED / TURNSTILE_OFFSCREEN — quiet window (default ON)
  TURNSTILE_FORCE_POLL — seconds to wait after force-render (default 12)
  TURNSTILE_RELOAD_ON_FAIL — 1=drop warm page and reload after empty token

Browser lifecycle: thread-local warm Chrome + one warm mint tab.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

_EXT_DIR = Path(__file__).resolve().parent.parent / "turnstilePatch"

_CHROMIUM_SLIM_FLAGS = [
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--mute-audio",
    "--disable-background-networking",
    "--no-first-run",
    "--disable-blink-features=AutomationControlled",
    # Keep MV2 extensions loadable on newer Chrome.
    "--enable-features=ExtensionManifestV2Availability",
    "--disable-features=ExtensionManifestV2Unsupported,ExtensionManifestV2Disabled",
]

# Thread-local warm Chrome (one browser per concurrent mint slot).
# run.py gates concurrency with TURNSTILE_PARALLEL (default 2).
_tls = threading.local()
_all_browsers_lock = threading.Lock()
_all_browsers: set[Any] = set()
_solve_count = 0
_solve_count_lock = threading.Lock()



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


def _chrome_bin() -> Optional[str]:
    if os.path.exists("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    for cand in (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ):
        if os.path.isfile(cand):
            return cand
    return None


def _proxy_server_arg(proxy: str) -> Optional[str]:
    proxy = (proxy or "").strip()
    if not proxy:
        return None
    u = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    host = u.hostname or ""
    if not host:
        return None
    scheme = u.scheme or "http"
    port = u.port or (443 if scheme == "https" else 80)
    return f"{scheme}://{host}:{port}"


_READ_TOKEN_JS = """
const pick = (v) => (v && String(v).length >= 80 ? String(v) : '');
const names = [
  'input[name="cf-turnstile-response"]',
  'textarea[name="cf-turnstile-response"]',
  'input[name="g-recaptcha-response"]',
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
  }
} catch (e) {}
if (window.__xaiTsToken) return pick(window.__xaiTsToken);
return '';
"""

_FORCE_RENDER_JS = """
const sitekey = arguments[0];
window.__xaiTsToken = '';
window.__xaiTsErr = null;
window.__xaiTsPhase = 'render';
window.__xaiTsGen = (window.__xaiTsGen || 0) + 1;
const gen = window.__xaiTsGen;
if (!window.turnstile || !window.turnstile.render) {
  return { ok: false, err: 'no-turnstile-api' };
}
const hostId = 'xai-force-ts-host';
let host = document.getElementById(hostId);
if (host) {
  try {
    if (window.turnstile.remove) window.turnstile.remove(host);
  } catch (e) {}
  try { host.remove(); } catch (e) {}
}
// Drop any leftover tokens so warm-page reuse cannot re-read the previous mint.
for (const el of document.querySelectorAll(
  'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], input[name="g-recaptcha-response"]'
)) {
  try { el.value = ''; } catch (e) {}
}
host = document.createElement('div');
host.id = hostId;
host.style.cssText =
  'width:300px;height:80px;position:fixed;top:80px;left:20px;'
  + 'z-index:999999;background:#fff;border:1px solid #ccc;';
document.body.appendChild(host);
let inp = document.querySelector('input[name="cf-turnstile-response"]');
if (!inp) {
  inp = document.createElement('input');
  inp.type = 'hidden';
  inp.name = 'cf-turnstile-response';
  document.body.appendChild(inp);
}
inp.value = '';
try {
  const id = window.turnstile.render(host, {
    sitekey: sitekey,
    theme: 'light',
    size: 'normal',
    appearance: 'always',
    callback: (tok) => {
      if (gen !== window.__xaiTsGen) return;
      window.__xaiTsToken = tok || '';
      inp.value = tok || '';
      window.__xaiTsPhase = 'done';
    },
    'error-callback': (code) => {
      if (gen !== window.__xaiTsGen) return;
      window.__xaiTsErr = String(code);
    },
    'expired-callback': () => {
      if (gen !== window.__xaiTsGen) return;
      window.__xaiTsToken = '';
      inp.value = '';
    },
  });
  return { ok: true, id: String(id), gen: gen };
} catch (e) {
  return { ok: false, err: String(e) };
}
"""


class DrissionTurnstileSolver:
    """Solve Turnstile with DrissionPage + turnstilePatch (terminal CLI OK)."""

    def __init__(
        self,
        *,
        timeout: Optional[float] = None,
        headless: Optional[bool] = None,
        proxy: str = "",
        debug: bool = False,
        **_ignored: Any,
    ) -> None:
        if timeout is None:
            try:
                timeout = float(os.environ.get("TURNSTILE_TIMEOUT") or "30")
            except ValueError:
                timeout = 30.0
        # Headed by default: matches grok_reg_clean; headless is often CF-blocked.
        if headless is None:
            headless = _env_truthy("TURNSTILE_HEADLESS", default=False)
        self._timeout = max(float(timeout), 15.0)
        self._headless = bool(headless)
        # Headed window control (ignored when headless):
        # - minimized (default ON): Chrome stays in Dock, Dock-only window
        # - offscreen (default ON): park window off-screen as backup
        self._offscreen = _env_truthy("TURNSTILE_OFFSCREEN", default=True)
        self._minimized = _env_truthy("TURNSTILE_MINIMIZED", default=True)
        pos = (os.environ.get("TURNSTILE_WINDOW_POSITION") or "").strip()
        self._window_position = pos or "-32000,-3200"
        self._proxy = _proxy_from_env(proxy)
        self._debug = bool(debug)

    def _log(self, msg: str) -> None:
        if self._debug:
            print(f"  [DrissionTurnstile] {msg}")

    def _build_options(self) -> Any:
        from DrissionPage import ChromiumOptions

        opts = ChromiumOptions()
        try:
            opts.auto_port()
        except Exception:
            pass
        try:
            opts.set_timeouts(base=1)
        except Exception:
            pass
        for flag in _CHROMIUM_SLIM_FLAGS:
            try:
                opts.set_argument(flag)
            except Exception:
                pass
        if self._headless:
            try:
                opts.headless(True)
            except Exception:
                opts.set_argument("--headless=new")
            self._log("headless=True (CF may block)")
        else:
            try:
                opts.headless(False)
            except Exception:
                pass
            if self._offscreen:
                # Real headed Chrome (extensions + CF OK) but parked off-screen
                # so the window stays out of the way during navigation.
                try:
                    opts.set_argument(f"--window-position={self._window_position}")
                except Exception:
                    pass
                try:
                    opts.set_argument("--window-size=900,700")
                except Exception:
                    pass
                self._log(
                    f"headed Chrome off-screen ({self._window_position}); "
                    "set TURNSTILE_OFFSCREEN=0 to show window"
                )
            if self._minimized:
                self._log(
                    "headed Chrome will stay minimized "
                    "(TURNSTILE_MINIMIZED=1; set 0 to show window)"
                )
            if not self._offscreen and not self._minimized:
                self._log("headed Chrome on-screen (auto-click, no manual input)")

        chrome = _chrome_bin()
        if chrome:
            try:
                opts.set_browser_path(chrome)
            except Exception:
                try:
                    opts.set_paths(browser_path=chrome)
                except Exception:
                    pass

        if _EXT_DIR.is_dir():
            try:
                opts.add_extension(str(_EXT_DIR))
                self._log(f"extension loaded: {_EXT_DIR}")
            except Exception as exc:
                self._log(f"add_extension failed: {exc}")
        else:
            self._log(f"turnstilePatch missing at {_EXT_DIR}")

        proxy_arg = _proxy_server_arg(self._proxy)
        if proxy_arg:
            try:
                opts.set_argument(f"--proxy-server={proxy_arg}")
                self._log(f"proxy-server={proxy_arg}")
            except Exception as exc:
                self._log(f"set proxy failed: {exc}")
        return opts

    def _fingerprint(self) -> tuple:
        return (
            bool(self._headless),
            bool(self._offscreen) if not self._headless else False,
            bool(self._minimized) if not self._headless else False,
            self._window_position if (not self._headless and self._offscreen) else "",
            self._proxy or "",
        )

    def _focus_guard_enabled(self) -> bool:
        return (not self._headless) and (self._offscreen or self._minimized)

    def _frontmost_app_name(self) -> str:
        try:
            proc = subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    'tell application "System Events" to get name of first application process whose frontmost is true',
                ],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            return (proc.stdout or "").strip()
        except Exception:
            return ""

    def _restore_frontmost(self, name: str) -> None:
        """Give focus back after Chrome briefly steals it on launch."""
        name = (name or "").strip()
        if not name or name.lower() in {"google chrome", "chrome", "chromium"}:
            return
        try:
            subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    f'tell application "System Events" to set frontmost of first process whose name is "{name}" to true',
                ],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception:
            pass

    def _minimize_window(self, browser: Any = None, tab: Any = None) -> None:
        """Cross-platform: keep Chrome minimized via Drission/CDP only.

        Minimize or park the Chrome window off-screen when headed.
        """
        if self._headless or not self._minimized:
            return
        targets = []
        if tab is not None:
            targets.append(tab)
        if browser is not None:
            try:
                targets.append(browser.latest_tab)
            except Exception:
                pass
            targets.append(browser)
        for obj in targets:
            if obj is None:
                continue
            for attr_path in (
                ("set", "window", "mini"),
                ("set", "window", "minimize"),
            ):
                try:
                    cur = obj
                    for n in attr_path[:-1]:
                        cur = getattr(cur, n)
                    fn = getattr(cur, attr_path[-1])
                    fn()
                    self._log("window minimized")
                    return
                except Exception:
                    continue
            try:
                obj.run_cdp(
                    "Browser.setWindowBounds",
                    windowId=1,
                    bounds={"windowState": "minimized"},
                )
                self._log("window minimized (cdp)")
                return
            except Exception:
                continue

    def _quiet_browser(self, browser: Any = None, tab: Any = None) -> None:
        """Minimize the Chrome window when quiet mode is enabled."""
        if not self._focus_guard_enabled():
            return
        self._minimize_window(browser=browser, tab=tab)

    def _drop_tls_browser(self) -> None:
        """Quit this thread's warm Chrome (if any)."""
        browser = getattr(_tls, "browser", None)
        _tls.browser = None
        _tls.fp = None
        _tls.mint_tab = None
        _tls.mint_url = None
        if browser is None:
            return
        with _all_browsers_lock:
            _all_browsers.discard(browser)
        try:
            browser.quit()
        except Exception:
            pass
        self._log("browser quit")

    # Back-compat alias used by CF-block path.
    def _drop_shared_browser(self) -> None:
        self._drop_tls_browser()

    def _ensure_browser(self) -> tuple[Any, bool]:
        """Return (browser, cold_launch). Reuse this thread's warm Chrome."""
        from DrissionPage import Chromium

        fp = self._fingerprint()
        browser = getattr(_tls, "browser", None)
        if browser is not None and getattr(_tls, "fp", None) == fp:
            try:
                _ = browser.tabs_count
                return browser, False
            except Exception:
                self._log("warm browser dead; relaunch")
                self._drop_tls_browser()

        prev_front = self._frontmost_app_name() if self._focus_guard_enabled() else ""
        opts = self._build_options()
        browser = Chromium(opts)
        _tls.browser = browser
        _tls.fp = fp
        with _all_browsers_lock:
            _all_browsers.add(browser)
        self._log("cold launch Chrome (thread-local; reuse on this worker)")
        if self._focus_guard_enabled():
            self._quiet_browser(browser=browser)
            self._restore_frontmost(prev_front)
            self._log(
                f"minimized after cold launch; restored frontmost={prev_front!r}"
            )
        return browser, True

    def _get_tab(self, browser: Any) -> Any:
        """Open/reuse a tab without activating the Chrome window.

        Prefer ``new_tab(background=True)`` (CDP background target). Fall back to
        reusing ``latest_tab`` so we never foreground a new tab on macOS.
        """
        # Prefer a background target when the API supports it.
        for kwargs in (
            {"background": True},
            {"new_window": False, "background": True},
        ):
            try:
                tab = browser.new_tab(**kwargs)
                if tab is not None:
                    self._log(f"new_tab background ok ({kwargs})")
                    return tab
            except TypeError:
                continue
            except Exception as exc:
                self._log(f"new_tab{kwargs} skip: {exc}")
        # Reuse existing tab when new_tab is unavailable.
        try:
            tab = browser.latest_tab
            if tab is not None:
                self._log("reuse latest_tab (no new target)")
                return tab
        except Exception:
            pass
        try:
            tab = browser.new_tab()
            if tab is not None:
                self._log("new_tab default")
                return tab
        except Exception:
            pass
        try:
            tabs = browser.tab_ids
            if tabs:
                return browser.get_tab(tabs[-1])
        except Exception:
            pass
        raise RuntimeError("could not open browser tab")

    def _invalidate_mint_tab(self) -> None:
        tab = getattr(_tls, "mint_tab", None)
        _tls.mint_tab = None
        _tls.mint_url = None
        if tab is not None:
            try:
                tab.close()
            except Exception:
                pass

    def _mint_tab_alive(self, tab: Any, url: str) -> bool:
        """True if tab still has a live signup page with turnstile API."""
        if tab is None:
            return False
        if (getattr(_tls, "mint_url", None) or "") != url:
            return False
        try:
            href = self._run_js(tab, "return String(location.href || '');") or ""
            if "accounts.x.ai" not in str(href):
                return False
            ok = self._run_js(
                tab, "return !!(window.turnstile && window.turnstile.render);"
            )
            return bool(ok)
        except Exception:
            return False

    def _ensure_mint_page(
        self, browser: Any, url: str, deadline: float
    ) -> tuple[Any, bool]:
        """Safari-style warm page: navigate once, reuse for later force-renders.

        Returns (tab, navigated).
        """
        tab = getattr(_tls, "mint_tab", None)
        if self._mint_tab_alive(tab, url):
            self._log("warm mint page reuse (no reload)")
            self._quiet_browser(browser=browser, tab=tab)
            return tab, False

        if tab is not None:
            self._invalidate_mint_tab()

        tab = self._get_tab(browser)
        self._quiet_browser(browser=browser, tab=tab)
        nav_to = min(20, max(5, int(deadline - time.time())))
        self._log(f"navigate mint page once timeout={nav_to}s")
        tab.get(url, timeout=nav_to)
        self._quiet_browser(browser=browser, tab=tab)
        time.sleep(0.15)

        title = ""
        try:
            title = tab.title or ""
        except Exception:
            pass
        if "Attention Required" in title or "Just a moment" in title:
            self._invalidate_mint_tab()
            raise RuntimeError(f"CF block/interstitial: {title}")

        self._click_email_path(tab)
        if not self._ensure_turnstile_api(tab, min(deadline, time.time() + 8)):
            self._invalidate_mint_tab()
            raise RuntimeError("turnstile API not available on page")

        _tls.mint_tab = tab
        _tls.mint_url = url
        return tab, True


    def _close_tab(self, tab: Any) -> None:
        try:
            tab.close()
        except Exception:
            pass

    def _reset_browser_session(self, browser: Any) -> None:
        """Clear cookies between accounts; keep the Chrome process warm."""
        try:
            # DrissionPage: browser-level cookie clear when available
            browser.set.cookies.clear()
            return
        except Exception:
            pass
        try:
            tab = browser.latest_tab
            tab.set.cookies.clear()
        except Exception:
            pass

    def _run_js(self, tab: Any, script: str, *args: Any) -> Any:
        """DrissionPage run_js expects statement body with ``return``, not arrow fns."""
        try:
            if args:
                return tab.run_js(script, *args)
            return tab.run_js(script)
        except Exception as exc:
            self._log(f"run_js failed: {exc}")
            return None

    def _click_email_path(self, tab: Any) -> None:
        for text in ("使用邮箱注册", "Sign up with email", "Continue with email"):
            try:
                ele = tab.ele(f"text:{text}", timeout=1.5)
                if ele:
                    ele.click()
                    self._log(f"clicked: {text}")
                    time.sleep(0.4)
                    return
            except Exception:
                continue
        self._run_js(
            tab,
            """
const texts = ['使用邮箱注册', 'Sign up with email', 'Continue with email'];
for (const t of texts) {
  const nodes = Array.from(document.querySelectorAll('button, a, div, span'));
  const n = nodes.find(x => (x.innerText || '').includes(t));
  if (n) { n.click(); return t; }
}
return '';
""",
        )

    def _ensure_turnstile_api(self, tab: Any, deadline: float) -> bool:
        while time.time() < deadline:
            ok = self._run_js(
                tab,
                "return !!(window.turnstile && window.turnstile.render);",
            )
            if ok:
                return True
            # inject explicit api (sync append; poll next loop)
            self._run_js(
                tab,
                """
if (window.turnstile && window.turnstile.render) return true;
if (!document.getElementById('xai-ts-api-inject')) {
  const s = document.createElement('script');
  s.id = 'xai-ts-api-inject';
  s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  s.async = true;
  document.head.appendChild(s);
}
return false;
""",
            )
            time.sleep(0.5)
        return bool(
            self._run_js(
                tab, "return !!(window.turnstile && window.turnstile.render);"
            )
        )

    def _nudge_click(self, tab: Any) -> None:
        self._run_js(
            tab,
            """
const host = document.getElementById('xai-force-ts-host');
if (host) {
  const r = host.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const el = document.elementFromPoint(x, y) || host;
  el.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: x, clientY: y }));
  return 'host';
}
const iframe = document.querySelector('iframe[src*="challenges.cloudflare"], iframe[src*="turnstile"]');
if (iframe) {
  iframe.click();
  return 'iframe';
}
return '';
""",
        )
        self._run_js(
            tab,
            """
const walk = (root, depth) => {
  if (!root || depth > 6) return false;
  try {
    const inp = root.querySelector && root.querySelector('input[type="checkbox"], .mark');
    if (inp) { inp.click(); return true; }
  } catch (e) {}
  const nodes = root.querySelectorAll ? root.querySelectorAll('*') : [];
  for (const n of nodes) {
    if (n.shadowRoot && walk(n.shadowRoot, depth + 1)) return true;
  }
  return false;
};
walk(document, 0);
const nodes = Array.from(document.querySelectorAll('iframe, div, body'));
for (const n of nodes) {
  const txt = ((n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute && n.getAttribute('src') || '')).toLowerCase();
  if (txt.includes('turnstile') && typeof n.click === 'function') {
    try { n.click(); } catch (e) {}
  }
}
return true;
""",
        )

    def _widget_box(self, tab: Any) -> Optional[dict[str, float]]:
        box = self._run_js(
            tab,
            """
const host = document.getElementById('xai-force-ts-host');
const pick = (el) => {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  if (r.width < 10 || r.height < 10) return null;
  return {x: r.x, y: r.y, w: r.width, h: r.height};
};
let box = pick(host && host.querySelector('iframe')) || pick(host);
if (box) return box;
const nodes = [
  ...document.querySelectorAll('iframe[src*="challenges.cloudflare"]'),
  ...document.querySelectorAll('iframe[src*="turnstile"]'),
  ...document.querySelectorAll('.cf-turnstile'),
];
for (const n of nodes) {
  box = pick(n);
  if (box) return box;
}
return null;
""",
        )
        if isinstance(box, dict) and "x" in box:
            try:
                return {k: float(box[k]) for k in ("x", "y", "w", "h")}
            except Exception:
                return None
        return None

    def _cdp_click_xy(self, tab: Any, x: float, y: float) -> bool:
        """Browser-internal mouse click via CDP — does not move OS cursor."""
        try:
            tab.run_cdp(
                "Input.dispatchMouseEvent",
                type="mouseMoved",
                x=float(x),
                y=float(y),
            )
            tab.run_cdp(
                "Input.dispatchMouseEvent",
                type="mousePressed",
                x=float(x),
                y=float(y),
                button="left",
                clickCount=1,
                buttons=1,
            )
            time.sleep(0.04)
            tab.run_cdp(
                "Input.dispatchMouseEvent",
                type="mouseReleased",
                x=float(x),
                y=float(y),
                button="left",
                clickCount=1,
                buttons=0,
            )
            return True
        except Exception as exc:
            self._log(f"cdp click failed: {exc}")
            return False

    def _cdp_click_widget(self, tab: Any) -> str:
        """Click Turnstile checkbox inside Chrome (no OS focus / mouse steal)."""
        box = self._widget_box(tab) or {
            "x": 20.0,
            "y": 80.0,
            "w": 300.0,
            "h": 80.0,
        }
        offsets = (
            (0.0, 0.0),
            (0.0, 2.0),
            (-2.0, 0.0),
            (3.0, 0.0),
            (5.0, 1.0),
            (-4.0, 2.0),
        )
        last = ""
        for dx, dy in offsets:
            cx = box["x"] + min(28.0, max(18.0, box["w"] * 0.10)) + dx
            cy = box["y"] + max(14.0, box["h"] * 0.50) + dy
            if self._cdp_click_xy(tab, cx, cy):
                last = f"cdp:{cx:.0f},{cy:.0f}"
                self._log(
                    f"cdp click ({cx:.0f},{cy:.0f}) box=({box['w']:.0f}x{box['h']:.0f})"
                )
                time.sleep(0.35)
                token = self._run_js(tab, _READ_TOKEN_JS) or ""
                if isinstance(token, str) and len(token) >= 80:
                    return last
        return last

    def _poll_token(
        self,
        tab: Any,
        deadline: float,
        *,
        allow_cdp: bool = False,
        reject: str = "",
    ) -> str:
        nudged = False
        cdp_clicked = False
        reject = reject if isinstance(reject, str) and len(reject) >= 80 else ""
        while time.time() < deadline:
            token = self._run_js(tab, _READ_TOKEN_JS) or ""
            if (
                isinstance(token, str)
                and len(token) >= 80
                and token != reject
            ):
                return token
            remaining = deadline - time.time()
            if allow_cdp and not cdp_clicked and remaining > 1.0:
                cdp_clicked = True
                self._cdp_click_widget(tab)
            elif not nudged and remaining < self._timeout / 2 and remaining > 3:
                nudged = True
                self._nudge_click(tab)
            time.sleep(0.25)
        return ""

    def solve_turnstile(
        self,
        website_url: str,
        website_key: str,
        *,
        premium: bool = False,  # noqa: ARG002
    ) -> str:
        url = (website_url or "").strip()
        key = (website_key or "").strip()
        if not url or not key:
            raise ValueError("website_url and website_key are required")

        deadline = time.time() + self._timeout
        self._log(
            f"open {url} sitekey={key[:16]}... headless={self._headless} "
            f"offscreen={self._offscreen and not self._headless} "
            f"minimized={self._minimized and not self._headless} "
            f"hard_timeout={self._timeout:.0f}s proxy={'yes' if self._proxy else 'no'}"
        )

        last_err: Optional[BaseException] = None
        try:
            force_s = float(os.environ.get("TURNSTILE_FORCE_POLL") or "12")
        except ValueError:
            force_s = 12.0
        force_s = max(4.0, min(force_s, 25.0))
        reload_on_fail = _env_truthy("TURNSTILE_RELOAD_ON_FAIL", True)

        for attempt in range(2):
            if time.time() >= deadline - 2:
                break
            try:
                browser, cold = self._ensure_browser()
                if cold:
                    self._log("cold Chrome — first mint will load page once")
                else:
                    self._log("warm Chrome process")

                # Safari-like: one warm mint tab; force-render widgets in place.
                tab, navigated = self._ensure_mint_page(browser, url, deadline)
                mode = "nav" if navigated else "reuse"

                if navigated:
                    try:
                        early_s = float(os.environ.get("TURNSTILE_EARLY_POLL") or "3")
                    except ValueError:
                        early_s = 3.0
                    early_s = max(1.0, min(early_s, 8.0))
                    token = self._poll_token(
                        tab, min(deadline, time.time() + early_s), allow_cdp=False
                    )
                    if token:
                        with _solve_count_lock:
                            global _solve_count
                            _solve_count += 1
                            n = _solve_count
                        self._log(
                            f"token ok len={len(token)} (native/early, "
                            f"solve#{n}, {mode})"
                        )
                        return token

                # Snapshot pre-render token (should be empty after force clear).
                prev = self._run_js(tab, _READ_TOKEN_JS) or ""
                if not (isinstance(prev, str) and len(prev) >= 80):
                    prev = ""

                meta = self._run_js(tab, _FORCE_RENDER_JS, key)
                self._log(f"force render ({mode}): {meta}")
                # Never accept the pre-render token on warm reuse.
                still = self._run_js(tab, _READ_TOKEN_JS) or ""
                if isinstance(still, str) and len(still) >= 80:
                    prev = still
                    self._run_js(
                        tab,
                        """
window.__xaiTsToken = '';
for (const el of document.querySelectorAll(
  'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
)) { try { el.value = ''; } catch (e) {} }
return true;
""",
                    )
                time.sleep(0.25 if mode == "reuse" else 0.35)
                self._cdp_click_widget(tab)
                token = self._poll_token(
                    tab,
                    min(deadline, time.time() + force_s),
                    allow_cdp=True,
                    reject=prev if isinstance(prev, str) else "",
                )
                if token:
                    with _solve_count_lock:
                        _solve_count += 1
                        n = _solve_count
                    self._log(
                        f"token ok len={len(token)} (force-render, "
                        f"solve#{n}, {mode})"
                    )
                    return token

                last_err = RuntimeError(
                    f"empty token after force-render ({force_s:.0f}s, {mode})"
                )
                self._log(f"attempt {attempt + 1}: {last_err}")
                if reload_on_fail:
                    self._invalidate_mint_tab()
            except Exception as exc:
                last_err = exc
                self._log(f"attempt {attempt + 1} failed: {exc}")
                self._invalidate_mint_tab()
                msg = str(exc).lower()
                if "cf block" in msg or "interstitial" in msg:
                    self._drop_tls_browser()
            if time.time() + 3 >= deadline:
                break

        if time.time() >= deadline:
            raise TimeoutError(
                f"Turnstile hard timeout after {self._timeout:.0f}s "
                f"(DrissionPage/turnstilePatch; no token)"
            ) from last_err
        raise RuntimeError(
            f"DrissionPage did not produce a Turnstile token "
            f"({self._timeout:.0f}s budget). "
            "Try HTTPS_PROXY=…, TURNSTILE_HEADLESS=0 (default), "
            "or confirm turnstilePatch/ is present."
        ) from last_err



def _close_all_drission_browsers() -> None:
    with _all_browsers_lock:
        browsers = list(_all_browsers)
        _all_browsers.clear()
    for browser in browsers:
        try:
            browser.quit()
        except Exception:
            pass
    try:
        _tls.browser = None
        _tls.fp = None
        _tls.mint_tab = None
        _tls.mint_url = None
    except Exception:
        pass


import atexit as _atexit

_atexit.register(_close_all_drission_browsers)
