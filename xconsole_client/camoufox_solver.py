# -*- coding: utf-8 -*-
"""Camoufox (anti-detect Firefox) Turnstile solver — optional backend.

Playwright 1.5x+ sends ``isMobile`` in viewport CDP calls that Camoufox's
Juggler protocol rejects. Work around by:

1. Launching the Camoufox binary via ``playwright.firefox.launch``
2. Opening contexts with ``no_viewport=True`` (never setDefaultViewport)
3. Evaluating page JS with the ``mw:`` prefix (Camoufox main-world bridge)

Env:
  TURNSTILE_SOLVER=camoufox
  TURNSTILE_HEADLESS=0|1|virtual   (default 0; headless often CF-blocked)
  TURNSTILE_TIMEOUT=60
  HTTPS_PROXY / HTTP_PROXY / ALL_PROXY
"""

from __future__ import annotations

import atexit
import os
import threading
import time
from typing import Any, Optional
from urllib.parse import urlparse

_browser_lock = threading.Lock()
_shared: dict[str, Any] = {
    "playwright": None,
    "browser": None,
    "fp": None,  # (headless, proxy)
    "virtual_display": None,
}
_solve_count = 0


def _env_truthy(name: str, default: bool = False) -> bool:
    from .envutil import env_truthy

    return env_truthy(name, default)


def _proxy_from_env(explicit: str = "") -> str:
    from .envutil import proxy_from_env

    # Prefer explicit first is already handled by proxy_from_env.
    return proxy_from_env(explicit, include_all=True)


def _playwright_proxy(proxy: str) -> Optional[dict[str, str]]:
    proxy = (proxy or "").strip()
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    u = urlparse(proxy)
    if not u.hostname:
        return None
    server = f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
    out: dict[str, str] = {"server": server}
    if u.username:
        out["username"] = u.username
    if u.password:
        out["password"] = u.password
    return out


def _mw(page: Any, body: str) -> Any:
    """Evaluate *body* in Camoufox main world (required for window.turnstile)."""
    src = body if body.lstrip().startswith("mw:") else "mw:" + body
    return page.evaluate(src)


_READ_TOKEN_JS = """() => {
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
}"""


def _force_render_js(sitekey: str) -> str:
    # Embed sitekey: Camoufox mw: evaluate does not forward page.evaluate args.
    return f"""() => {{
  const sitekey = {sitekey!r};
  window.__xaiTsToken = '';
  window.__xaiTsErr = null;
  if (!window.turnstile || !window.turnstile.render) {{
    return {{ ok: false, err: 'no-turnstile-api' }};
  }}
  const hostId = 'xai-force-ts-host';
  let host = document.getElementById(hostId);
  if (!host) {{
    host = document.createElement('div');
    host.id = hostId;
    host.style.cssText =
      'width:320px;height:90px;position:fixed;top:100px;left:40px;'
      + 'z-index:2147483647;background:#fff;border:1px solid #999;';
    document.body.appendChild(host);
  }}
  let inp = document.querySelector('input[name="cf-turnstile-response"]');
  if (!inp) {{
    inp = document.createElement('input');
    inp.type = 'hidden';
    inp.name = 'cf-turnstile-response';
    document.body.appendChild(inp);
  }}
  try {{
    const id = window.turnstile.render(host, {{
      sitekey: sitekey,
      theme: 'light',
      size: 'normal',
      appearance: 'always',
      callback: (tok) => {{
        window.__xaiTsToken = tok || '';
        inp.value = tok || '';
      }},
      'error-callback': (code) => {{ window.__xaiTsErr = String(code); }},
      'expired-callback': () => {{ window.__xaiTsToken = ''; }},
    }});
    return {{ ok: true, id: String(id) }};
  }} catch (e) {{
    return {{ ok: false, err: String(e) }};
  }}
}}"""


_INJECT_API_JS = """() => {
  if (window.turnstile && window.turnstile.render) return 'already';
  if (document.getElementById('xai-ts-api')) return 'pending';
  const s = document.createElement('script');
  s.id = 'xai-ts-api';
  s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  document.head.appendChild(s);
  return 'appended';
}"""


class CamoufoxTurnstileSolver:
    """Solve Turnstile with Camoufox anti-detect Firefox via Playwright."""

    def __init__(
        self,
        *,
        timeout: Optional[float] = None,
        headless: Optional[bool] = None,
        proxy: str = "",
        debug: bool = False,
    ) -> None:
        self._debug = bool(debug)
        env_to = (os.environ.get("TURNSTILE_TIMEOUT") or "").strip()
        self._timeout = float(timeout if timeout is not None else (env_to or 60))
        if headless is None:
            raw = (os.environ.get("TURNSTILE_HEADLESS") or "").strip().lower()
            if raw in {"virtual", "xvfb"}:
                self._headless: Any = "virtual"
            elif raw:
                self._headless = _env_truthy("TURNSTILE_HEADLESS", False)
            else:
                # Headed is far more reliable against CF for this sitekey.
                self._headless = False
        else:
            self._headless = headless
        self._proxy = _proxy_from_env(proxy)

    def _log(self, msg: str) -> None:
        if self._debug or _env_truthy("TURNSTILE_DEBUG", False):
            print(f"[CamoufoxTurnstile] {msg}", flush=True)

    def _ensure_browser(self) -> tuple[Any, bool]:
        """Return (browser, cold_launch). Process-global warm reuse."""
        fp = (str(self._headless), self._proxy or "")
        with _browser_lock:
            if _shared["browser"] is not None and _shared["fp"] == fp:
                return _shared["browser"], False
            self._drop_locked()
            try:
                from camoufox.utils import launch_options
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise RuntimeError(
                    "Camoufox backend requires packages: camoufox, playwright. "
                    "Install with: pip install camoufox && camoufox fetch"
                ) from exc

            headless = self._headless
            virtual_display = None
            if headless == "virtual":
                try:
                    from camoufox.virtdisplay import VirtualDisplay

                    virtual_display = VirtualDisplay(debug=self._debug)
                    os.environ["DISPLAY"] = virtual_display.get()
                    headless = False
                    self._log(f"virtual display {os.environ.get('DISPLAY')}")
                except Exception as exc:
                    self._log(f"virtual display failed ({exc}); using headed")
                    headless = False

            os_name = None
            if os.uname().sysname == "Darwin":
                os_name = "macos"
            elif os.uname().sysname == "Linux":
                os_name = "linux"
            elif os.uname().sysname == "Windows":
                os_name = "windows"

            proxy_dict = _playwright_proxy(self._proxy)
            opts = launch_options(
                headless=bool(headless),
                humanize=True,
                window=(1280, 800),
                os=os_name,
                disable_coop=True,
                main_world_eval=True,
                i_know_what_im_doing=True,
                proxy=proxy_dict,
            )
            # Never pass viewport — Camoufox Juggler rejects isMobile.
            launch_kwargs = {k: v for k, v in opts.items() if k not in ("viewport", "screen")}
            pw = sync_playwright().start()
            browser = pw.firefox.launch(**launch_kwargs)
            _shared["playwright"] = pw
            _shared["browser"] = browser
            _shared["fp"] = fp
            _shared["virtual_display"] = virtual_display
            self._log(
                f"cold launch Camoufox headless={self._headless} "
                f"proxy={'yes' if self._proxy else 'no'}"
            )
            return browser, True

    def _drop_locked(self) -> None:
        br = _shared.get("browser")
        pw = _shared.get("playwright")
        vd = _shared.get("virtual_display")
        _shared["browser"] = None
        _shared["playwright"] = None
        _shared["fp"] = None
        _shared["virtual_display"] = None
        if br is not None:
            try:
                br.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        if vd is not None:
            try:
                vd.kill()
            except Exception:
                pass

    def _new_page(self, browser: Any) -> tuple[Any, Any]:
        # no_viewport avoids Browser.setDefaultViewport(isMobile) protocol error.
        proxy = _playwright_proxy(self._proxy)
        kwargs: dict[str, Any] = {"no_viewport": True}
        if proxy:
            kwargs["proxy"] = proxy
        context = browser.new_context(**kwargs)
        page = context.new_page()
        return context, page

    def _click_email_path(self, page: Any) -> None:
        for text in (
            "Allow All",
            "Accept all",
            "Accept All",
            "允许全部",
            "全部允许",
        ):
            try:
                page.get_by_text(text, exact=False).first.click(timeout=1200)
                self._log(f"consent: {text}")
                page.wait_for_timeout(400)
                break
            except Exception:
                pass
        for text in (
            "使用邮箱注册",
            "Sign up with email",
            "Continue with email",
            "Sign up with Email",
        ):
            try:
                page.get_by_text(text, exact=False).first.click(timeout=4000)
                self._log(f"clicked: {text}")
                page.wait_for_timeout(800)
                return
            except Exception:
                continue

    def _ensure_turnstile_api(self, page: Any, deadline: float) -> bool:
        # Brief native wait, then inject early — mw: does not await Promises.
        native_until = min(deadline, time.time() + 1.5)
        while time.time() < native_until:
            try:
                if _mw(page, "() => !!(window.turnstile && window.turnstile.render)"):
                    return True
            except Exception:
                pass
            page.wait_for_timeout(250)
        try:
            status = _mw(page, _INJECT_API_JS)
            self._log(f"inject api: {status}")
        except Exception as exc:
            self._log(f"inject api failed: {exc}")
        end = max(deadline, time.time() + 6)
        while time.time() < end:
            try:
                if _mw(page, "() => !!(window.turnstile && window.turnstile.render)"):
                    return True
            except Exception:
                pass
            page.wait_for_timeout(250)
        return False

    def _poll_token(self, page: Any, deadline: float) -> str:
        while time.time() < deadline:
            try:
                tok = _mw(page, _READ_TOKEN_JS) or ""
            except Exception:
                tok = ""
            if isinstance(tok, str) and len(tok) >= 80:
                return tok
            # Short timeouts: missing host must not eat the whole TURNSTILE_TIMEOUT.
            try:
                host = page.query_selector("#xai-force-ts-host")
                if host is not None:
                    box = host.bounding_box()
                    if box:
                        page.mouse.click(box["x"] + 24, box["y"] + box["height"] / 2)
                        page.mouse.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
            except Exception:
                pass
            try:
                for frame in page.frames:
                    u = frame.url or ""
                    if "challenges.cloudflare.com" not in u:
                        continue
                    el = frame.frame_element()
                    b = el.bounding_box()
                    if b:
                        page.mouse.click(b["x"] + 24, b["y"] + b["height"] / 2)
            except Exception:
                pass
            page.wait_for_timeout(400)
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
            f"hard_timeout={self._timeout:.0f}s proxy={'yes' if self._proxy else 'no'}"
        )

        global _solve_count
        last_err: Optional[BaseException] = None
        for attempt in range(2):
            if time.time() >= deadline - 3:
                break
            context = None
            try:
                browser, cold = self._ensure_browser()
                if not cold:
                    self._log("warm reuse Camoufox (no relaunch)")
                context, page = self._new_page(browser)
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(300)

                title = ""
                try:
                    title = page.title() or ""
                except Exception:
                    pass
                if "Attention Required" in title or "Just a moment" in title:
                    raise RuntimeError(f"CF block/interstitial: {title}")

                if not self._ensure_turnstile_api(page, min(deadline, time.time() + 8)):
                    self._click_email_path(page)
                    if not self._ensure_turnstile_api(page, min(deadline, time.time() + 12)):
                        raise RuntimeError("turnstile API not available on page")
                else:
                    self._click_email_path(page)
                    self._ensure_turnstile_api(page, min(deadline, time.time() + 6))

                token = self._poll_token(page, min(deadline, time.time() + 5))
                if token:
                    _solve_count += 1
                    self._log(
                        f"token ok len={len(token)} (native/early, "
                        f"solve#{_solve_count}, {'cold' if cold else 'warm'})"
                    )
                    try:
                        context.close()
                    except Exception:
                        pass
                    return token

                meta = _mw(page, _force_render_js(key))
                self._log(f"force render: {meta}")
                if not (isinstance(meta, dict) and meta.get("ok")):
                    raise RuntimeError(f"force render failed: {meta!r}")
                page.wait_for_timeout(600)
                token = self._poll_token(page, deadline)
                if token:
                    _solve_count += 1
                    self._log(
                        f"token ok len={len(token)} (force-render, "
                        f"solve#{_solve_count}, {'cold' if cold else 'warm'})"
                    )
                    try:
                        context.close()
                    except Exception:
                        pass
                    return token

                last_err = RuntimeError("empty token")
                try:
                    context.close()
                except Exception:
                    pass
            except Exception as exc:
                last_err = exc
                self._log(f"attempt {attempt + 1} failed: {exc}")
                try:
                    if context is not None:
                        context.close()
                except Exception:
                    pass
                msg = str(exc).lower()
                if "cf block" in msg or "ismobile" in msg or "protocol error" in msg:
                    with _browser_lock:
                        self._drop_locked()
                time.sleep(0.4)

        raise TimeoutError(
            f"Turnstile hard timeout after {self._timeout:.0f}s "
            f"(Camoufox, no token); last={last_err!r}"
        ) from last_err


def _close_all_camoufox() -> None:
    with _browser_lock:
        br = _shared.get("browser")
        pw = _shared.get("playwright")
        vd = _shared.get("virtual_display")
        _shared["browser"] = None
        _shared["playwright"] = None
        _shared["fp"] = None
        _shared["virtual_display"] = None
        if br is not None:
            try:
                br.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        if vd is not None:
            try:
                vd.kill()
            except Exception:
                pass


atexit.register(_close_all_camoufox)
