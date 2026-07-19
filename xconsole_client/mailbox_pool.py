# -*- coding: utf-8 -*-
"""Background mailbox pool (multi-channel create ahead, consume on register).

Mirrors ``TurnstileTokenPool``: demand-driven producers pre-create disposable
inboxes so registration workers only acquire a ready ``Mailbox`` and then
send/wait for the x.ai code with the channel-bound receiver.

Each pooled item records ``channel`` (tempmail | yyds | cloudflare). Producers
use ``ChannelRouter`` so free-tier / 429 on one backend fail over immediately
to another configured backend — never dead-wait on a single source.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from typing import Callable, Optional, Tuple, Union

from xconsole_client.mail_channels import ChannelRouter, Mailbox


def _env_int(name: str, default: int, *, lo: int = 1, hi: int = 64) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        return default


def _env_int_if_set(name: str, default: int, *, lo: int = 1, hi: int = 64) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        return default


def _env_float(name: str, default: float, *, lo: float = 1.0, hi: float = 3600.0) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(hi, float(raw)))
    except ValueError:
        return default


def suggest_mail_pool_params(reg_threads: int) -> Tuple[int, int, int]:
    """Derive ``(size, target, minters)`` from registration concurrency.

    Explicit ``MAIL_POOL_SIZE`` / ``_TARGET`` / ``_MINTERS`` win when set.

    Auto:
      size    = clamp(-t, 2..32)
      target  = min(2, size)          # small idle stock
      minters = ceil(-t/4) clamp 1..4
    """
    t = max(1, min(int(reg_threads or 1), 64))
    size_auto = min(32, max(2, t))
    size = _env_int_if_set("MAIL_POOL_SIZE", size_auto, lo=1, hi=32)
    target_auto = min(2, size)
    target = _env_int_if_set("MAIL_POOL_TARGET", target_auto, lo=0, hi=32)
    target = max(0, min(size, target))
    minters_auto = max(1, min(4, (t + 3) // 4))
    minters = _env_int_if_set("MAIL_POOL_MINTERS", minters_auto, lo=1, hi=4)
    return size, target, minters


# Backward-compatible name.
PooledInbox = Mailbox


class MailboxPool:
    """Producer/consumer pool of pre-created multi-channel inboxes."""

    def __init__(
        self,
        source: Union[ChannelRouter, Callable[[], Mailbox], Callable[[], Tuple[str, object]]],
        *,
        size: Optional[int] = None,
        max_age: Optional[float] = None,
        minters: Optional[int] = None,
        target: Optional[int] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._source = source
        self._size = int(
            size if size is not None else _env_int("MAIL_POOL_SIZE", 4, lo=1, hi=32)
        )
        self._max_age = float(
            max_age
            if max_age is not None
            else _env_float("MAIL_POOL_MAX_AGE", 600.0, lo=30.0, hi=3600.0)
        )
        self._minters = int(
            minters if minters is not None else _env_int("MAIL_POOL_MINTERS", 1, lo=1, hi=4)
        )
        raw_target = (
            int(target)
            if target is not None
            else _env_int("MAIL_POOL_TARGET", min(2, self._size), lo=0, hi=32)
        )
        self._target = max(0, min(self._size, raw_target))
        self._log = log or (lambda _msg: None)
        self._q: queue.Queue[Mailbox] = queue.Queue(maxsize=self._size)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._stats_lock = threading.Lock()
        self._created = 0
        self._discarded = 0
        self._acquired = 0
        self._create_errors = 0
        self._waiting = 0
        self._by_channel: dict[str, int] = {}

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

    def router(self) -> Optional[ChannelRouter]:
        return self._source if isinstance(self._source, ChannelRouter) else None

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for i in range(self._minters):
            t = threading.Thread(
                target=self._create_loop,
                name=f"mail-pool-mint-{i + 1}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        extra = ""
        r = self.router()
        if r is not None:
            extra = f" channels=[{','.join(r.channels)}]"
        self._log(
            f"mail pool start size={self._size} target={self._target} "
            f"max_age={self._max_age:.0f}s minters={self._minters}{extra}"
        )

    def stop(self, *, wait: float = 1.0) -> None:
        self._stop.set()
        deadline = time.time() + max(0.0, wait)
        for t in self._threads:
            remain = deadline - time.time()
            if remain <= 0:
                break
            t.join(timeout=remain)
        self._threads.clear()
        by = ""
        with self._stats_lock:
            if self._by_channel:
                by = " by={" + ",".join(
                    f"{k}:{v}" for k, v in sorted(self._by_channel.items())
                ) + "}"
        r = self.router()
        health = f" health=[{r.summary()}]" if r is not None else ""
        self._log(
            f"mail pool stop created={self._created} acquired={self._acquired} "
            f"discarded={self._discarded} errors={self._create_errors} "
            f"q={self._q.qsize()}{by}{health}"
        )

    def qsize(self) -> int:
        return self._q.qsize()

    def acquire(self, *, timeout: float = 120.0, max_age: Optional[float] = None) -> Mailbox:
        """Block until a non-expired inbox is available."""
        age_limit = self._max_age if max_age is None else float(max_age)
        deadline = time.time() + max(1.0, float(timeout))
        with self._stats_lock:
            self._waiting += 1
        try:
            while time.time() < deadline:
                if self._stop.is_set():
                    raise RuntimeError("mailbox pool stopped")
                remain = deadline - time.time()
                try:
                    item = self._q.get(timeout=min(1.0, max(0.05, remain)))
                except queue.Empty:
                    continue
                if item.age > age_limit:
                    with self._stats_lock:
                        self._discarded += 1
                    self._log(
                        f"mail pool discard stale age={item.age:.0f}s "
                        f"[{item.channel}] {item.email}"
                    )
                    continue
                with self._stats_lock:
                    self._acquired += 1
                return item
            raise TimeoutError(
                f"mailbox pool empty after {timeout:.0f}s "
                f"(q={self._q.qsize()} created={self._created} errors={self._create_errors})"
            )
        finally:
            with self._stats_lock:
                self._waiting = max(0, self._waiting - 1)

    def _desired_stock(self) -> int:
        with self._stats_lock:
            waiting = self._waiting
        if waiting > 0:
            return min(self._size, waiting + self._target)
        return min(self._size, self._target)

    def _need_more(self) -> bool:
        return self._q.qsize() < self._desired_stock()

    def _produce_one(self) -> Mailbox:
        src = self._source
        if isinstance(src, ChannelRouter):
            return src.create()
        out = src()  # type: ignore[operator]
        if isinstance(out, Mailbox):
            return out
        if isinstance(out, tuple) and len(out) >= 2:
            email, receiver = out[0], out[1]
            channel = str(out[2]) if len(out) >= 3 else "unknown"
            return Mailbox(
                email=str(email or "").strip(),
                receiver=receiver,
                channel=channel,
                created_at=time.time(),
            )
        raise TypeError(f"mail pool source returned unsupported type: {type(out)!r}")

    def _create_loop(self) -> None:
        idle_logged = False
        while not self._stop.is_set():
            if not self._need_more():
                if not idle_logged:
                    self._log(
                        f"mail mint pause (satisfied q={self._q.qsize()}/"
                        f"{self._size} want={self._desired_stock()} "
                        f"waiting={self._waiting})"
                    )
                    idle_logged = True
                time.sleep(0.25)
                continue
            idle_logged = False
            try:
                item = self._produce_one()
            except Exception as exc:  # noqa: BLE001
                with self._stats_lock:
                    self._create_errors += 1
                self._log(f"mail pool create error: {exc}")
                self._stop.wait(1.0)
                continue
            email = str(item.email or "").strip()
            if not email or item.receiver is None:
                with self._stats_lock:
                    self._create_errors += 1
                self._log("mail pool create empty")
                self._stop.wait(0.8)
                continue
            if not self._need_more() and self._q.qsize() >= max(1, self._target or 1):
                with self._stats_lock:
                    self._discarded += 1
                self._log(
                    f"mail pool drop create (no longer needed q={self._q.qsize()} "
                    f"want={self._desired_stock()})"
                )
                continue
            while not self._stop.is_set():
                try:
                    self._q.put(item, timeout=0.5)
                    with self._stats_lock:
                        self._created += 1
                        ch = item.channel or "unknown"
                        self._by_channel[ch] = self._by_channel.get(ch, 0) + 1
                    self._log(
                        f"mail pool +1 [{item.channel}] {email} "
                        f"q={self._q.qsize()}/{self._size} "
                        f"want={self._desired_stock()} wait={self._waiting}"
                    )
                    break
                except queue.Full:
                    if item.age > self._max_age * 0.9:
                        with self._stats_lock:
                            self._discarded += 1
                        self._log("mail pool drop create (queue full, near expiry)")
                        break
