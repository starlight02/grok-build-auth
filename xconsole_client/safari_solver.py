# -*- coding: utf-8 -*-
"""Turnstile solver via system Safari (macOS Apple Events).

Uses real ``/Applications/Safari.app`` — not Playwright WebKit.

Requirements (one-time in Safari Settings → Developer):
  - Show features for web developers
  - Allow JavaScript from Apple Events

Click strategy:
  1. Force-render explicit Turnstile host (default; avoids solved native box)
  2. HID click (Quartz/cliclick) on mapped screen coords
     (disable with TURNSTILE_SAFARI_HID=0)
  3. Optional ``TURNSTILE_INTERACTIVE=1`` human-click fallback

Env:
  TURNSTILE_SOLVER=safari
  TURNSTILE_TIMEOUT=60
  TURNSTILE_DEBUG=1
  TURNSTILE_PAUSE_FILE=/tmp/grok-turnstile.pause  (exists → no HID clicks)
  TURNSTILE_SAFARI_HID=1           0=never OS mouse
  TURNSTILE_SAFARI_BOUNDS=x1,y1,x2,y2  on-screen bounds used only for HID
  TURNSTILE_REQUIRE_FRONTMOST=1    0=click even if Safari not frontmost
  TURNSTILE_SAFARI_EMAIL=0         1=click「使用邮箱注册」(default 0)
  TURNSTILE_SAFARI_FORCE_RENDER=1  0=legacy native-only (can stick after first solve)
  HTTPS_PROXY / HTTP_PROXY — ignored (Safari system proxy only)
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

# Safari Apple Events are document-global; serialize mints process-wide.
_safari_lock = threading.Lock()

_READ_TOKEN_JS = r"""
(() => {
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
})()
"""

_FORCE_RENDER_JS = r"""
(() => {
  const sitekey = %SITEKEY%;
  window.__xaiTsToken = '';
  window.__xaiTsErr = null;
  window.__xaiTsPhase = '';
  if (!window.turnstile || !window.turnstile.render) {
    return JSON.stringify({ ok: false, err: 'no-turnstile-api' });
  }
  const hostId = 'xai-force-ts-host';
  let host = document.getElementById(hostId);
  if (host) {
    try {
      if (window.turnstile.remove) window.turnstile.remove(host);
    } catch (e) {}
    host.remove();
  }
  host = document.createElement('div');
  host.id = hostId;
  // Fixed top-left of the *content* viewport (not browser chrome).
  host.style.cssText =
    'width:300px;height:80px;position:fixed;top:16px;left:16px;'
    + 'z-index:2147483647;background:#fff;';
  document.body.appendChild(host);
  for (const el of document.querySelectorAll('input[name="cf-turnstile-response"]')) {
    el.value = '';
  }
  let inp = document.querySelector('input[name="cf-turnstile-response"]');
  if (!inp) {
    inp = document.createElement('input');
    inp.type = 'hidden';
    inp.name = 'cf-turnstile-response';
    document.body.appendChild(inp);
  }
  try {
    const id = window.turnstile.render(host, {
      sitekey: sitekey,
      theme: 'light',
      size: 'normal',
      appearance: 'always',
      callback: (tok) => {
        window.__xaiTsToken = tok || '';
        inp.value = tok || '';
      },
      'error-callback': (code) => { window.__xaiTsErr = String(code); },
      'expired-callback': () => { window.__xaiTsToken = ''; },
      'before-interactive-callback': () => { window.__xaiTsPhase = 'need-click'; },
      'after-interactive-callback': () => { window.__xaiTsPhase = 'after'; },
    });
    return JSON.stringify({ ok: true, id: String(id) });
  } catch (e) {
    return JSON.stringify({ ok: false, err: String(e) });
  }
})()
"""

_FIND_NATIVE_WIDGET_JS = r"""
(() => {
  const pick = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 20) return null;
    if (r.width > 520 || r.height > 140) return null;
    return {
      x: r.x, y: r.y, w: r.width, h: r.height,
      tag: el.tagName, id: el.id || '',
      src: String(el.src || el.getAttribute?.('src') || '').slice(0, 80),
    };
  };
  const score = (b) => {
    // Prefer managed-size cards near top-left (homepage CF widget).
    let s = b.w * b.h;
    if (b.w >= 250 && b.w <= 340 && b.h >= 55 && b.h <= 90) s += 50000;
    if (b.y < 160 && b.x < 200) s += 20000;
    if (b.y < 80 && b.x < 80) s += 10000;
    return s;
  };
  let best = null;
  let bestS = 0;
  const consider = (el, why) => {
    const b = pick(el);
    if (!b) return;
    const sc = score(b);
    if (sc > bestS) {
      bestS = sc;
      best = Object.assign({ why }, b);
    }
  };
  const sels = [
    'iframe[src*="challenges.cloudflare"]',
    'iframe[src*="turnstile"]',
    '.cf-turnstile iframe',
    '.cf-turnstile',
    '[data-sitekey] iframe',
    '[data-sitekey]',
    '#xai-force-ts-host iframe',
    '#xai-force-ts-host',
  ];
  for (const sel of sels) {
    try {
      document.querySelectorAll(sel).forEach((el) => consider(el, sel));
    } catch (e) {}
  }
  // Text anchor: 「请验证您是真人」 / Verify you are human (may be in light DOM label).
  try {
    const re = /请验证您是真人|Verify you are human|确认您是真人/i;
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walk.nextNode())) {
      if (!re.test(node.nodeValue || '')) continue;
      let el = node.parentElement;
      for (let i = 0; i < 8 && el; i++) {
        consider(el, 'text-human');
        el = el.parentElement;
      }
    }
  } catch (e) {}
  // Geometry fallback: ~300x65-ish boxes in upper area.
  try {
    document.querySelectorAll('div,iframe,section').forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.y > 220 || r.x > 420) return;
      if (r.width >= 250 && r.width <= 360 && r.height >= 55 && r.height <= 95) {
        consider(el, 'geom');
      }
    });
  } catch (e) {}
  if (!best) return '';
  return JSON.stringify(best);
})()
"""

_CLICK_EMAIL_JS = r"""
(() => {
  const texts = ['使用邮箱注册', 'Sign up with email', 'Continue with email', '邮箱'];
  const nodes = Array.from(document.querySelectorAll('button, a, div, span'));
  for (const t of texts) {
    const el = nodes.find((n) => (n.textContent || '').includes(t));
    if (el) { el.click(); return t; }
  }
  return '';
})()
"""

_INJECT_API_JS = r"""
(() => {
  if (window.turnstile && window.turnstile.render) return 'present';
  if (document.getElementById('xai-ts-api')) return 'loading';
  const s = document.createElement('script');
  s.id = 'xai-ts-api';
  s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  s.async = true;
  document.head.appendChild(s);
  return 'started';
})()
"""

_CLEAN_TS_JS = r"""
(() => {
  try {
    window.__xaiTsToken = '';
    window.__xaiTsErr = null;
    window.__xaiTsPhase = '';
  } catch (e) {}
  for (const el of document.querySelectorAll(
    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], input[name="g-recaptcha-response"]'
  )) {
    try { el.value = ''; } catch (e) {}
  }
  const host = document.getElementById('xai-force-ts-host');
  if (host) {
    try {
      if (window.turnstile && window.turnstile.remove) window.turnstile.remove(host);
    } catch (e) {}
    try { host.remove(); } catch (e) {}
  }
  // Drop leftover explicit widgets so the next render is not a no-op.
  try {
    document.querySelectorAll('iframe[src*="challenges.cloudflare"], iframe[src*="turnstile"]').forEach((el) => {
      try { el.remove(); } catch (e) {}
    });
  } catch (e) {}
  return 'cleaned';
})()
"""


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _run_osascript(source: str, *, timeout: float = 60.0) -> str:
    proc = subprocess.run(
        ["/usr/bin/osascript", "-"],
        input=source,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or f"osascript exit {proc.returncode}")
    return (proc.stdout or "").strip()


def _as_quote(s: str) -> str:
    """AppleScript quoted form of a string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class SafariTurnstileSolver:
    """Solve Turnstile with system Safari via Apple Events + HID click.

    Strategy:

    1. Prefer native homepage widget; else force-render into fixed host.
    2. Flash HID on mapped screen coords (unless TURNSTILE_SAFARI_HID=0).
    3. Optional ``TURNSTILE_INTERACTIVE=1`` human-click fallback.
    """

    def __init__(
        self,
        *,
        timeout: Optional[float] = None,
        headless: Optional[bool] = None,  # noqa: ARG002 — Safari is always headed
        proxy: str = "",
        debug: bool = False,
        interactive: Optional[bool] = None,
        **_ignored: Any,
    ) -> None:
        if sys_platform_is_not_darwin():
            raise RuntimeError("Safari Turnstile backend is macOS-only")
        env_to = (os.environ.get("TURNSTILE_TIMEOUT") or "").strip()
        self._timeout = float(timeout if timeout is not None else (env_to or 60))
        self._timeout = max(self._timeout, 15.0)
        self._proxy = (proxy or "").strip()
        self._debug = bool(debug) or _env_truthy("TURNSTILE_DEBUG", False)
        if interactive is None:
            self._interactive = _env_truthy("TURNSTILE_INTERACTIVE", False)
        else:
            self._interactive = bool(interactive)
        # HID needs a normal on-screen window; no flash/restore/park-behind.
        self._hid_enabled = _env_truthy("TURNSTILE_SAFARI_HID", True)
        self._click_bounds = self._parse_bounds(
            (os.environ.get("TURNSTILE_SAFARI_BOUNDS") or "").strip() or "900,500,1400,900"
        )
        if self._proxy:
            self._log("HTTPS_PROXY set but Safari backend uses system proxy only; ignored")

    @staticmethod
    def _parse_bounds(raw: str) -> tuple[int, int, int, int]:
        try:
            parts = [int(float(x.strip())) for x in raw.split(",")]
            if len(parts) == 4:
                x1, y1, x2, y2 = parts
                if x2 > x1 + 100 and y2 > y1 + 100:
                    return x1, y1, x2, y2
        except Exception:
            pass
        return 900, 500, 1400, 900

    def _log(self, msg: str) -> None:
        if self._debug:
            print(f"[SafariTurnstile] {msg}", flush=True)
        else:
            low = msg.lower()
            if low.startswith("pause") or "skip click" in low or low.startswith("hid "):
                print(f"[SafariTurnstile] {msg}", flush=True)

    def _pause_file(self) -> str:
        return (os.environ.get("TURNSTILE_PAUSE_FILE") or "/tmp/grok-turnstile.pause").strip()

    def _is_paused(self) -> bool:
        path = self._pause_file()
        try:
            return bool(path) and Path(path).exists()
        except Exception:
            return False

    def _wait_if_paused(self, deadline: float) -> None:
        noticed = False
        while self._is_paused() and time.time() < deadline:
            if not noticed:
                self._log(f"paused — remove {self._pause_file()} to resume clicks")
                noticed = True
            time.sleep(0.4)

    def _require_frontmost(self) -> bool:
        """If true, skip HID when Safari is not frontmost."""
        return _env_truthy("TURNSTILE_REQUIRE_FRONTMOST", True)

    def _frontmost_app_name(self) -> str:
        src = (
            "try\n"
            '  tell application "System Events"\n'
            "    set p to name of first application process whose frontmost is true\n"
            "  end tell\n"
            "  return p\n"
            "on error\n"
            '  return ""\n'
            "end try\n"
        )
        try:
            return (_run_osascript(src, timeout=5.0) or "").strip()
        except Exception:
            return ""

    def _safari_is_frontmost(self) -> bool:
        return self._frontmost_app_name().lower() == "safari"

    def _set_window_bounds(self, bounds: tuple[int, int, int, int]) -> None:
        x1, y1, x2, y2 = bounds
        src = f"""
tell application "Safari"
  if (count of windows) is 0 then return
  try
    set bounds of front window to {{{x1}, {y1}, {x2}, {y2}}}
  end try
end tell
"""
        try:
            _run_osascript(src, timeout=8.0)
        except Exception as exc:
            self._log(f"set bounds failed: {exc}")

    def _set_minimized(self, minimized: bool) -> None:
        val = "true" if minimized else "false"
        src = f"""
tell application "System Events"
  tell process "Safari"
    if (count of windows) is 0 then return
    try
      set value of attribute "AXMinimized" of window 1 to {val}
    end try
  end tell
end tell
"""
        try:
            _run_osascript(src, timeout=8.0)
        except Exception:
            pass

    def _prepare_window_for_click(self) -> None:
        """Unminimize + ensure on-screen bounds so HID can hit the widget."""
        self._set_minimized(False)
        self._set_window_bounds(self._click_bounds)

    def _ensure_safari_frontmost(self, *, deadline: float) -> bool:
        """Bring Safari front for HID; do not restore another app afterwards."""
        try:
            _run_osascript('tell application "Safari" to activate', timeout=5.0)
        except Exception as exc:
            self._log(f"activate Safari failed: {exc}")
        for _ in range(8):
            if time.time() >= deadline:
                break
            if self._safari_is_frontmost():
                return True
            time.sleep(0.12)
        ok = self._safari_is_frontmost()
        if not ok:
            self._log("skip click: Safari is not frontmost (focus another app / use pause file)")
        return ok

    def _js(self, expression: str, *, timeout: float = 30.0) -> str:
        """Run a JS expression in Safari's front document; return string result."""
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
            fh.write(expression)
            path = fh.name
        try:
            src = f"""
set jsPath to {_as_quote(path)}
set js to do shell script "/bin/cat " & quoted form of jsPath
tell application "Safari"
  if (count of windows) is 0 then
    error "Safari has no open window"
  end if
  set r to do JavaScript js in front document
  if r is missing value then return ""
  return r as text
end tell
"""
            return _run_osascript(src, timeout=timeout)
        finally:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass

    def _open(self, url: str) -> None:
        """Ensure signup URL is loaded. No close-window / cache-bust thrash.

        If front document is already on accounts.x.ai, leave it alone so mint
        only force-renders a new widget (no full page flash).
        """
        try:
            href = self._js("String(location.href || '')", timeout=5.0)
            if href and "accounts.x.ai" in href:
                self._log("reuse open Safari document (no reload)")
                return
        except Exception:
            pass
        src = f"""
tell application "Safari"
  if (count of windows) is 0 then
    make new document with properties {{URL:{_as_quote(url)}}}
  else
    set URL of front document to {_as_quote(url)}
  end if
end tell
"""
        try:
            _run_osascript(src, timeout=30.0)
        except Exception:
            _run_osascript(
                f"""
tell application "Safari"
  activate
  if (count of windows) is 0 then
    make new document with properties {{URL:{_as_quote(url)}}}
  else
    set URL of front document to {_as_quote(url)}
  end if
end tell
""",
                timeout=30.0,
            )

    def _page_ready(self) -> bool:
        try:
            href = self._js("location.href", timeout=10.0)
            return bool(href) and "accounts.x.ai" in href
        except Exception as exc:
            self._log(f"page ready check: {exc}")
            return False

    def _window_bounds(self) -> Optional[tuple[int, int, int, int]]:
        """Return Safari front window bounds (x1,y1,x2,y2) global screen coords."""
        src = """
tell application "Safari"
  if (count of windows) is 0 then return ""
  set b to bounds of front window
  return (item 1 of b as text) & "," & (item 2 of b as text) & "," & ¬
    (item 3 of b as text) & "," & (item 4 of b as text)
end tell
"""
        try:
            raw = _run_osascript(src, timeout=10.0)
            parts = [int(float(x.strip())) for x in raw.split(",")]
            if len(parts) == 4:
                return parts[0], parts[1], parts[2], parts[3]
        except Exception as exc:
            self._log(f"window bounds: {exc}")
        return None

    def _widget_viewport_box(self) -> Optional[dict[str, float]]:
        """Viewport box of native Turnstile card / force host (CSS pixels)."""
        try:
            import json as _json

            raw = self._js(_FIND_NATIVE_WIDGET_JS, timeout=10.0)
            if raw:
                data = _json.loads(raw)
                if isinstance(data, dict) and "x" in data:
                    self._log(
                        f"widget box via {data.get('why', '?')} "
                        f"({data['x']:.0f},{data['y']:.0f},{data['w']:.0f}x{data['h']:.0f})"
                    )
                    return {k: float(data[k]) for k in ("x", "y", "w", "h")}
        except Exception as exc:
            self._log(f"widget box: {exc}")
        return None

    def _os_click_screen(self, x: int, y: int) -> str:
        """Real HID mouse click (Quartz / cliclick). No permanent activate."""
        if self._is_paused():
            self._log(f"skip click: paused ({self._pause_file()})")
            return "skipped-paused"
        if self._require_frontmost() and not self._safari_is_frontmost():
            self._log("skip click: Safari not frontmost")
            return "skipped-not-frontmost"
        x_i, y_i = int(x), int(y)
        errors: list[str] = []
        try:
            from Quartz.CoreGraphics import (  # type: ignore
                CGEventCreateMouseEvent,
                CGEventPost,
                CGPointMake,
                kCGEventLeftMouseDown,
                kCGEventLeftMouseUp,
                kCGEventMouseMoved,
                kCGHIDEventTap,
                kCGMouseButtonLeft,
            )

            pt = CGPointMake(float(x_i), float(y_i))
            for ev in (
                kCGEventMouseMoved,
                kCGEventLeftMouseDown,
                kCGEventLeftMouseUp,
            ):
                e = CGEventCreateMouseEvent(None, ev, pt, kCGMouseButtonLeft)
                CGEventPost(kCGHIDEventTap, e)
                time.sleep(0.02)
        except Exception as exc:
            errors.append(f"quartz:{exc}")

        cliclick = "/opt/homebrew/bin/cliclick"
        if Path(cliclick).exists():
            try:
                subprocess.run(
                    [cliclick, f"m:{x_i},{y_i}", "w:50", f"c:{x_i},{y_i}"],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
            except Exception as exc:
                errors.append(f"cliclick:{exc}")

        # Fallback only when Quartz failed — avoid permanent activate in quiet mode.
        if errors:
            try:
                src = f"""
tell application "System Events"
  click at {{{x_i}, {y_i}}}
end tell
return "sys-events"
"""
                _run_osascript(src, timeout=10.0)
            except Exception as exc:
                errors.append(f"sys:{exc}")

        if len(errors) >= 3:
            return "click-failed:" + ";".join(errors)
        return f"clicked({x_i},{y_i})"

    def _content_chrome_top(self, bounds: tuple[int, int, int, int]) -> int:
        """Points from window top edge to web content origin (toolbar etc.)."""
        x1, y1, x2, y2 = bounds
        win_h = max(1, y2 - y1)
        raw = (os.environ.get("TURNSTILE_SAFARI_CHROME_TOP") or "").strip()
        if raw:
            try:
                return max(0, int(raw))
            except ValueError:
                pass
        try:
            inner = float(self._js("String(window.innerHeight || 0)", timeout=5.0))
        except Exception:
            inner = 0.0
        if inner > 50:
            # Safari AS window height includes chrome; innerHeight is content.
            chrome = int(round(win_h - inner))
            # Clamp to sane toolbar sizes (compact ↔ full toolbar).
            if 20 <= chrome <= 120:
                return chrome
        return 52

    def _click_widget_until_token(self, deadline: float, *, label: str) -> str:
        """HID click with jitter; poll token. No focus restore / park-behind."""
        offsets = (
            (0, 0),
            (0, 2),
            (-2, 0),
            (3, 0),
            (0, -2),
            (5, 1),
            (-3, 2),
        )
        for i, (dx, dy) in enumerate(offsets):
            if time.time() >= deadline - 0.5:
                break
            self._wait_if_paused(deadline)
            if time.time() >= deadline - 0.5:
                break

            if not self._ensure_safari_frontmost(deadline=deadline):
                time.sleep(0.5)
                continue
            self._prepare_window_for_click()

            bounds = self._window_bounds()
            box = self._widget_viewport_box() or {
                "x": 16.0,
                "y": 16.0,
                "w": 300.0,
                "h": 80.0,
            }
            if not bounds:
                break
            x1, y1, x2, y2 = bounds
            chrome_top = self._content_chrome_top(bounds)
            vx = box["x"] + min(28.0, max(18.0, box["w"] * 0.10)) + dx
            vy = box["y"] + max(14.0, box["h"] * 0.50) + dy
            sx = int(round(x1 + vx))
            sy = int(round(y1 + chrome_top + vy))
            sx = max(x1 + 5, min(x2 - 5, sx))
            sy = max(y1 + chrome_top + 5, min(y2 - 5, sy))
            self._log(
                f"{label} try#{i + 1} screen=({sx},{sy}) d=({dx},{dy}) "
                f"front={self._frontmost_app_name()!r}"
            )
            self._os_click_screen(sx, sy)

            token = self._poll_token(
                min(deadline, time.time() + 2.5), label=f"{label}-click{i + 1}"
            )
            if token:
                return token
        return self._poll_token(deadline, label=label)

    def _read_token(self) -> str:
        try:
            token = self._js(_READ_TOKEN_JS, timeout=10.0)
        except Exception as exc:
            self._log(f"poll err: {exc}")
            return ""
        return token if token and len(token) >= 80 else ""

    def _poll_token(self, deadline: float, *, label: str) -> str:
        while time.time() < deadline:
            token = self._read_token()
            if token:
                self._log(f"token via {label} len={len(token)}")
                return token
            try:
                err = self._js("String(window.__xaiTsErr || '')", timeout=5.0)
                if err and err not in {"", "null", "undefined"}:
                    self._log(f"turnstile err: {err}")
            except Exception:
                pass
            time.sleep(0.45)
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
        if not Path("/Applications/Safari.app").exists():
            raise RuntimeError("Safari.app not found at /Applications/Safari.app")

        deadline = time.time() + self._timeout
        self._log(
            f"open {url} sitekey={key[:16]}... hard_timeout={self._timeout:.0f}s "
            f"interactive={self._interactive} hid={self._hid_enabled}"
        )
        self._wait_if_paused(deadline)
        if time.time() >= deadline - 3:
            raise TimeoutError(
                f"Safari Turnstile paused until timeout (remove {self._pause_file()} to resume)"
            )

        with _safari_lock:
            last_err: Optional[BaseException] = None
            for attempt in range(2):
                if time.time() >= deadline - 3:
                    break
                try:
                    token = self._solve_once(url, key, deadline)
                    if token:
                        self._log(f"token ok len={len(token)}")
                        return token
                    last_err = RuntimeError("empty token")
                except Exception as exc:
                    last_err = exc
                    self._log(f"attempt {attempt + 1} failed: {exc}")
                    msg = str(exc).lower()
                    if "allow javascript from apple events" in msg:
                        raise RuntimeError(
                            "Safari blocked do JavaScript. Enable: "
                            "Safari Settings → Developer → "
                            "Allow JavaScript from Apple Events"
                        ) from exc
                time.sleep(0.4)

        raise TimeoutError(
            f"Safari Turnstile hard timeout after {self._timeout:.0f}s "
            f"(widget may need a real human click; set TURNSTILE_INTERACTIVE=1 "
            f"and click the checkbox in Safari)"
        ) from last_err

    def _solve_once(self, url: str, key: str, deadline: float) -> str:
        self._open(url)
        for _ in range(40):
            if time.time() >= deadline:
                break
            if self._page_ready():
                try:
                    ready = self._js("String(document.readyState || '')", timeout=5.0)
                except Exception:
                    ready = ""
                if ready.strip().lower() in {"interactive", "complete"}:
                    break
            time.sleep(0.35)
        time.sleep(0.5)

        # Drop any residual token/host from a previous mint in this profile.
        try:
            self._js(_CLEAN_TS_JS, timeout=8.0)
        except Exception as exc:
            self._log(f"clean skip: {exc}")

        # Email path is optional and often wrong for minting.
        if _env_truthy("TURNSTILE_SAFARI_EMAIL", False):
            try:
                clicked = self._js(_CLICK_EMAIL_JS, timeout=10.0)
                self._log(f"email path click: {clicked!r}")
                time.sleep(0.7)
            except Exception as exc:
                self._log(f"email click skip: {exc}")
        else:
            self._log("skip email path (TURNSTILE_SAFARI_EMAIL=0)")

        # Always force-render a fresh explicit widget.
        # Preferring "native" homepage boxes was wrong: after one HID solve the
        # dead/solved card still matches geometry, we skipped re-render, and the
        # next mint never showed a new challenge.
        # Opt out only with TURNSTILE_SAFARI_FORCE_RENDER=0 (legacy native path).
        force_render = _env_truthy("TURNSTILE_SAFARI_FORCE_RENDER", True)
        if not force_render:
            self._log("FORCE_RENDER=0: legacy native-only path (may stick on solved widget)")
            for _ in range(25):
                if time.time() >= deadline:
                    break
                box = self._widget_viewport_box()
                if box and box.get("w", 0) >= 80 and box.get("h", 0) >= 40:
                    break
                time.sleep(0.35)
        else:
            try:
                has_ts = self._js(
                    "String(!!(window.turnstile && window.turnstile.render))",
                    timeout=10.0,
                )
            except Exception:
                has_ts = "false"
            if has_ts.strip().lower() not in {"true", "1"}:
                try:
                    inj = self._js(_INJECT_API_JS, timeout=10.0)
                    self._log(f"turnstile api: {inj}")
                except Exception as exc:
                    self._log(f"inject skip: {exc}")
                for _ in range(20):
                    if time.time() >= deadline:
                        break
                    try:
                        ready = self._js(
                            "String(!!(window.turnstile && window.turnstile.render))",
                            timeout=5.0,
                        )
                    except Exception:
                        ready = "false"
                    if ready.strip().lower() in {"true", "1"}:
                        break
                    time.sleep(0.35)

            render_js = _FORCE_RENDER_JS.replace("%SITEKEY%", repr(key))
            try:
                meta = self._js(render_js, timeout=15.0)
                self._log(f"force render: {meta}")
            except Exception as exc:
                self._log(f"force render failed: {exc}")
            time.sleep(1.2)
        # HID click (optional).
        if not self._hid_enabled:
            self._log("HID disabled (TURNSTILE_SAFARI_HID=0); no click path")
            return ""

        click_budget = min(28.0, max(8.0, deadline - time.time() - 2.0))
        click_deadline = min(deadline, time.time() + click_budget)
        self._log(f"HID click budget {click_budget:.0f}s mode=frontmost")
        token = self._click_widget_until_token(click_deadline, label="hid")
        if token:
            return token

        if self._interactive and time.time() < deadline - 1:
            remain = max(0.0, deadline - time.time())
            print(
                f"  [SafariTurnstile] 请在 Safari 窗口里用鼠标点一下 Turnstile 复选框 "
                f"(等待 {remain:.0f}s)…",
                flush=True,
            )
            self._log(f"interactive wait {remain:.0f}s")
            try:
                _run_osascript('tell application "Safari" to activate', timeout=5.0)
            except Exception:
                pass
            token = self._poll_token(deadline, label="interactive")
            if token:
                return token

        return ""


def sys_platform_is_not_darwin() -> bool:
    return os.uname().sysname != "Darwin"
