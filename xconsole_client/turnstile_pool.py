# -*- coding: utf-8 -*-
"""Background Turnstile token pool.

Mint tokens on dedicated workers while registration threads only *consume*
fresh tokens over pure HTTP. Production is demand-driven: keep a small ready
stock, expand under waiters, stop when stock already covers demand.

Enabled by default from run.py (``TURNSTILE_POOL=0`` to disable). When env
knobs are unset, ``suggest_pool_params(reg_threads)`` sizes the pool from
registration concurrency (``-t``).

Env (each overrides auto):
  TURNSTILE_POOL=1            enable (run.py default on)
  TURNSTILE_POOL_SIZE         hard max buffered tokens (auto: = -t)
  TURNSTILE_POOL_TARGET       idle ready stock (auto: min(2, size))
  TURNSTILE_TOKEN_MAX_AGE=200 discard tokens older than N seconds
  TURNSTILE_POOL_MINTERS      mint threads (auto: scale with -t; Safari=1)
  TURNSTILE_PAUSE_FILE=/tmp/grok-turnstile.pause
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple


def _env_int(name: str, default: int, *, lo: int = 1, hi: int = 64) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(int(raw), hi))
    except ValueError:
        return default


def _env_int_if_set(name: str, default: int, *, lo: int = 1, hi: int = 64) -> int:
    """Like ``_env_int`` but only overrides when the env var is non-empty."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(int(raw), hi))
    except ValueError:
        return default


def _env_float(name: str, default: float, *, lo: float = 1.0, hi: float = 600.0) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(float(raw), hi))
    except ValueError:
        return default


def suggest_pool_params(
    reg_threads: int,
    *,
    solver_mode: str = "",
) -> Tuple[int, int, int]:
    """Derive ``(size, target, minters)`` from registration concurrency.

    Explicit ``TURNSTILE_POOL_SIZE`` / ``_TARGET`` / ``_MINTERS`` win when set.

    Auto rules (warm mint ≈2.4s/token ≈25/min per minter):
      size    = clamp(-t, 2..32)           # cover concurrent acquires
      target  = min(2, size)              # small idle stock; demand expands
      minters = 1 for Safari HID;
                else ceil(-t/4) clamped 1..4  (-t4→1, -t8→2, -t16→4)
    """
    t = max(1, min(int(reg_threads or 1), 64))
    mode = (solver_mode or "").strip().lower()

    size_auto = min(32, max(2, t))
    size = _env_int_if_set("TURNSTILE_POOL_SIZE", size_auto, lo=1, hi=32)

    target_auto = min(2, size)
    target = _env_int_if_set("TURNSTILE_POOL_TARGET", target_auto, lo=0, hi=32)
    target = max(0, min(size, target))

    if mode in {"safari", "webkit-system", "system-safari"}:
        minters_auto = 1
    else:
        # 1 minter covers free-tier -t4; scale gently so we don't over-mint.
        minters_auto = max(1, min(4, (t + 3) // 4))
    minters = _env_int_if_set("TURNSTILE_POOL_MINTERS", minters_auto, lo=1, hi=4)

    return size, target, minters


def pause_file_path() -> str:
    return (os.environ.get("TURNSTILE_PAUSE_FILE") or "/tmp/grok-turnstile.pause").strip()


def is_paused() -> bool:
    path = pause_file_path()
    try:
        return bool(path) and os.path.exists(path)
    except Exception:
        return False


def wait_while_paused(
    stop: Optional[threading.Event] = None,
    *,
    log: Optional[Callable[[str], None]] = None,
    poll: float = 0.5,
) -> None:
    """Block while pause file exists (unless *stop* is set)."""
    noticed = False
    while is_paused():
        if stop is not None and stop.is_set():
            return
        if not noticed and log is not None:
            log(f"paused — remove {pause_file_path()} to resume mint/click")
            noticed = True
        time.sleep(poll)


@dataclass(frozen=True)
class PooledToken:
    token: str
    minted_at: float

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.minted_at)


class TurnstileTokenPool:
    """Producer/consumer pool around any ``solve_turnstile``-compatible solver."""

    def __init__(
        self,
        solver: Any,
        *,
        website_url: str,
        website_key: str,
        size: Optional[int] = None,
        max_age: Optional[float] = None,
        minters: Optional[int] = None,
        target: Optional[int] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._solver = solver
        self._url = website_url
        self._key = website_key
        self._size = int(
            size if size is not None else _env_int("TURNSTILE_POOL_SIZE", 4, lo=1, hi=32)
        )
        self._max_age = float(
            max_age
            if max_age is not None
            else _env_float("TURNSTILE_TOKEN_MAX_AGE", 200.0, lo=30.0, hi=500.0)
        )
        self._minters = int(
            minters if minters is not None else _env_int("TURNSTILE_POOL_MINTERS", 1, lo=1, hi=4)
        )
        # Idle ready-stock. Under load expands to waiting+target (capped by size).
        raw_target = (
            int(target)
            if target is not None
            else _env_int("TURNSTILE_POOL_TARGET", min(2, self._size), lo=0, hi=32)
        )
        self._target = max(0, min(self._size, raw_target))
        self._log = log or (lambda msg: None)
        self._q: queue.Queue[PooledToken] = queue.Queue(maxsize=self._size)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._stats_lock = threading.Lock()
        self._minted = 0
        self._discarded = 0
        self._acquired = 0
        self._mint_errors = 0
        self._waiting = 0  # consumers blocked in acquire()

    @property
    def max_age(self) -> float:
        return self._max_age

    @property
    def size(self) -> int:
        return self._size

    @property
    def target(self) -> int:
        return self._target

    @property
    def minters(self) -> int:
        return self._minters

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for i in range(self._minters):
            t = threading.Thread(
                target=self._mint_loop,
                name=f"ts-pool-mint-{i + 1}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        self._log(
            f"token pool start size={self._size} target={self._target} "
            f"max_age={self._max_age:.0f}s minters={self._minters} "
            f"pause_file={pause_file_path()}"
        )

    def stop(self, *, wait: float = 1.0) -> None:
        self._stop.set()
        # Unblock put() waiters by draining is not required; threads are daemon.
        deadline = time.time() + max(0.0, wait)
        for t in self._threads:
            remain = deadline - time.time()
            if remain <= 0:
                break
            t.join(timeout=remain)
        self._threads.clear()
        self._log(
            f"token pool stop minted={self._minted} acquired={self._acquired} "
            f"discarded={self._discarded} mint_errors={self._mint_errors} q={self._q.qsize()}"
        )

    def qsize(self) -> int:
        return self._q.qsize()

    def acquire(self, *, timeout: float = 180.0, max_age: Optional[float] = None) -> PooledToken:
        """Block until a non-expired token is available."""
        age_limit = self._max_age if max_age is None else float(max_age)
        deadline = time.time() + max(1.0, float(timeout))
        with self._stats_lock:
            self._waiting += 1
        try:
            while time.time() < deadline:
                if self._stop.is_set():
                    raise RuntimeError("turnstile token pool stopped")
                remain = deadline - time.time()
                try:
                    item = self._q.get(timeout=min(1.0, max(0.05, remain)))
                except queue.Empty:
                    continue
                if item.age > age_limit:
                    with self._stats_lock:
                        self._discarded += 1
                    self._log(f"pool discard stale token age={item.age:.0f}s")
                    continue
                with self._stats_lock:
                    self._acquired += 1
                return item
            raise TimeoutError(
                f"turnstile token pool empty after {timeout:.0f}s "
                f"(q={self._q.qsize()} minted={self._minted} errors={self._mint_errors})"
            )
        finally:
            with self._stats_lock:
                self._waiting = max(0, self._waiting - 1)

    def _desired_stock(self) -> int:
        """How many tokens should be ready right now.

        Idle: keep ``target`` (often 1–2). Under load: cover waiters + target,
        never above hard ``size``. target=0 means pure on-demand (only mint
        while someone is waiting).
        """
        with self._stats_lock:
            waiting = self._waiting
        if waiting > 0:
            return min(self._size, waiting + self._target)
        return min(self._size, self._target)

    def _need_more(self) -> bool:
        return self._q.qsize() < self._desired_stock()

    def _mint_loop(self) -> None:
        idle_logged = False
        while not self._stop.is_set():
            wait_while_paused(self._stop, log=self._log)
            if self._stop.is_set():
                return
            # Demand-driven: stop when stock already covers registration use.
            if not self._need_more():
                if not idle_logged:
                    self._log(
                        f"pool mint pause (satisfied q={self._q.qsize()}/"
                        f"{self._size} want={self._desired_stock()} "
                        f"waiting={self._waiting})"
                    )
                    idle_logged = True
                time.sleep(0.25)
                continue
            idle_logged = False
            try:
                token = self._solver.solve_turnstile(
                    website_url=self._url,
                    website_key=self._key,
                    premium=True,
                )
            except Exception as exc:  # noqa: BLE001
                with self._stats_lock:
                    self._mint_errors += 1
                self._log(f"pool mint error: {exc}")
                # Brief backoff so a sticky failure doesn't spin the CPU/HID.
                self._stop.wait(1.5)
                continue
            if not token or len(token) < 80:
                with self._stats_lock:
                    self._mint_errors += 1
                self._log("pool mint empty token")
                self._stop.wait(0.8)
                continue
            # After a slow mint, drop if demand already covered (reduce overshoot).
            if not self._need_more() and self._q.qsize() >= max(1, self._target or 1):
                with self._stats_lock:
                    self._discarded += 1
                self._log(
                    f"pool drop mint (no longer needed q={self._q.qsize()} "
                    f"want={self._desired_stock()})"
                )
                continue
            item = PooledToken(token=token, minted_at=time.time())
            while not self._stop.is_set():
                try:
                    self._q.put(item, timeout=0.5)
                    with self._stats_lock:
                        self._minted += 1
                    self._log(
                        f"pool +1 len={len(token)} q={self._q.qsize()}/{self._size} "
                        f"want={self._desired_stock()} wait={self._waiting}"
                    )
                    break
                except queue.Full:
                    # Consumer lag — wait, but drop if token would expire soon.
                    if item.age > self._max_age * 0.9:
                        with self._stats_lock:
                            self._discarded += 1
                        self._log("pool drop mint (queue full, near expiry)")
                        break
