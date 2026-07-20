# -*- coding: utf-8 -*-
"""Pluggable multi-channel disposable mailbox routing.

Design goals
------------
1. **Single channel** (only tempmail, or ``-e yyds``): zero multi-LB overhead;
   create may block on free-tier pacing / retries like before.
2. **Multi channel** (``-e auto`` with 2+ configured): prefer high-weight
   channels when they have a free slot; overflow to others for *this* create
   only. Rate limits are capacity, not "channel dead".
3. **Extensible**: add a channel with :func:`register_channel` — CLI ``auto``,
   detection, weights, capacity, and create all pick it up. No edits to
   ``run.py`` if/else trees required for new backends.

Built-ins: ``tempmail``, ``yyds``, ``cloudflare``.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

CreateFn = Callable[[], Tuple[str, Any]]
# () -> (email, receiver_with_wait_for_code)
ReadyInFn = Callable[[], float]
ConfiguredFn = Callable[[], bool]
CapacityFn = Callable[[], int]


class ChannelBusy(Exception):
    """Channel has no free capacity right now — try another / wait its slot."""

    def __init__(
        self,
        channel: str,
        retry_after: float = 3.0,
        reason: str = "busy",
    ) -> None:
        self.channel = (channel or "").strip().lower()
        self.retry_after = max(0.05, float(retry_after))
        self.reason = reason or "busy"
        super().__init__(
            f"channel {self.channel} busy ({self.reason}, retry_after={self.retry_after:.2f}s)"
        )


@dataclass
class Mailbox:
    """One disposable inbox bound to its production channel."""

    email: str
    receiver: Any
    channel: str
    created_at: float = field(default_factory=time.time)

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.created_at)


@dataclass
class ChannelSpec:
    """Registration record for one mail backend.

    New backends: build a ``ChannelSpec`` and call :func:`register_channel`.
    """

    name: str
    # Preference when a free slot exists (higher wins). Overflow when full/rate-limited.
    weight: int = 50
    # Default concurrent create capacity (dynamic via capacity_fn if set).
    capacity: int = 1
    # True → always eligible for auto-detect (e.g. free tempmail).
    always_available: bool = False
    # Whether env/credentials look usable right now.
    configured: ConfiguredFn = field(default=lambda: False)
    # Create one inbox: (email, receiver).
    create: Optional[CreateFn] = None
    # Seconds until a free create slot (0 = free). Optional.
    ready_in: Optional[ReadyInFn] = None
    # Dynamic capacity override (e.g. tempmail free=1, paid=3).
    capacity_fn: Optional[CapacityFn] = None
    # Human blurb for help / logs.
    help: str = ""

    def resolved_capacity(self) -> int:
        if self.capacity_fn is not None:
            try:
                return max(1, min(32, int(self.capacity_fn())))
            except Exception:  # noqa: BLE001
                pass
        return max(1, min(32, int(self.capacity)))

    def is_available(self) -> bool:
        if self.always_available:
            return True
        try:
            return bool(self.configured())
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, ChannelSpec] = {}
_REGISTRY_LOCK = threading.Lock()


def register_channel(spec: ChannelSpec, *, replace: bool = False) -> None:
    """Register (or replace) a mail channel. Safe to call from plugins."""
    name = (spec.name or "").strip().lower()
    if not name:
        raise ValueError("ChannelSpec.name required")
    if not callable(spec.create):
        raise ValueError(f"ChannelSpec.create required for {name!r}")
    spec.name = name
    with _REGISTRY_LOCK:
        if name in _REGISTRY and not replace:
            raise ValueError(
                f"mail channel {name!r} already registered (use replace=True to override)"
            )
        _REGISTRY[name] = spec


def get_channel(name: str) -> Optional[ChannelSpec]:
    return _REGISTRY.get((name or "").strip().lower())


def known_channels() -> List[str]:
    """Registered channel names (registration order)."""
    with _REGISTRY_LOCK:
        return list(_REGISTRY.keys())


def cli_email_choices() -> List[str]:
    """Argparse choices: auto + every registered channel."""
    return ["auto", *known_channels()]


# ---------------------------------------------------------------------------
# Built-in backends (create factories live here so run.py stays thin)
# ---------------------------------------------------------------------------

_cf_lock = threading.Lock()


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _create_tempmail() -> Tuple[str, Any]:
    from xconsole_client.tempmail_transport import TempmailInbox

    inbox = TempmailInbox(
        api_key=_env("TEMPMAIL_API_KEY"),
        prefix="xai",
        debug=False,
    )
    email = inbox.create()
    return email, inbox


def _tempmail_ready_in() -> float:
    from xconsole_client.tempmail_transport import free_create_ready_in

    return float(free_create_ready_in())


def _tempmail_capacity() -> int:
    # Free tier: serialize to 1 in-flight. Plus key: a few parallel creates.
    if _env("TEMPMAIL_API_KEY"):
        return 3
    return 1


def _create_yyds() -> Tuple[str, Any]:
    from xconsole_client.yyds_transport import YydsInbox

    inbox = YydsInbox(
        api_key=_env("YYDS_API_KEY"),
        jwt=_env("YYDS_JWT"),
        prefix="xai",
        debug=False,
    )
    email = inbox.create()
    return email, inbox


def _yyds_configured() -> bool:
    return bool(_env("YYDS_API_KEY") or _env("YYDS_JWT"))


def _create_cloudflare() -> Tuple[str, Any]:
    from xconsole_client.mailbox import AliasMailAccount, AliasMailCodeReceiver

    with _cf_lock:
        cf = AliasMailAccount.ensure_cf()
        alloc = AliasMailAccount(cf)
        address = alloc.create(prefix="xai")
    receiver = AliasMailCodeReceiver(cf, address=address, timeout=120, interval=3, since_now=True)
    return address, receiver


def _cloudflare_configured() -> bool:
    token = _env("CLOUDFLARE_API_TOKEN") or _env("CLOUDFLARE_MCP_READ_ALL_TOKEN")
    account = _env("CLOUDFLARE_ACCOUNT_ID")
    d1 = _env("CLOUDFLARE_D1_DB_ID")
    domains = _env("ALIAS_MAIL_DOMAINS") or _env("ALIAS_EXTRA_DOMAINS")
    return bool(token and account and d1 and domains)


def _register_builtins() -> None:
    """Idempotent registration of shipped backends."""
    builtins = [
        ChannelSpec(
            name="tempmail",
            weight=100,
            capacity=1,
            always_available=True,
            configured=lambda: True,
            create=_create_tempmail,
            ready_in=_tempmail_ready_in,
            capacity_fn=_tempmail_capacity,
            help="Tempmail.lol (free or TEMPMAIL_API_KEY)",
        ),
        ChannelSpec(
            name="yyds",
            weight=40,
            capacity=2,
            always_available=False,
            configured=_yyds_configured,
            create=_create_yyds,
            help="YYDS / maliapi (YYDS_API_KEY or YYDS_JWT)",
        ),
        ChannelSpec(
            name="cloudflare",
            weight=60,
            capacity=2,
            always_available=False,
            configured=_cloudflare_configured,
            create=_create_cloudflare,
            help="Cloudflare D1 alias mail (CLOUDFLARE_* + ALIAS_MAIL_DOMAINS)",
        ),
    ]
    for spec in builtins:
        with _REGISTRY_LOCK:
            if spec.name not in _REGISTRY:
                _REGISTRY[spec.name] = spec


_register_builtins()


# ---------------------------------------------------------------------------
# Resolve / weights / capacity (env overrides, registry-aware)
# ---------------------------------------------------------------------------


def parse_channel_list(raw: str) -> List[str]:
    parts = [p.strip().lower() for p in re.split(r"[,，\s|]+", raw or "") if p.strip()]
    out: List[str] = []
    seen: set[str] = set()
    known = set(known_channels())
    for p in parts:
        if p in {"auto", "all", "*"}:
            continue
        if p not in known:
            raise ValueError(f"unknown mail channel {p!r}; registered: {sorted(known)}")
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def detect_available_channels() -> List[str]:
    """Channels that look usable under current env."""
    _register_builtins()
    found = [n for n, spec in _REGISTRY.items() if spec.is_available()]

    # Stable: higher default weight first for display.
    def _w(n: str) -> int:
        s = get_channel(n)
        return int(s.weight) if s is not None else 0

    return sorted(found, key=lambda n: -_w(n))


def resolve_channels(choice: str = "auto") -> List[str]:
    """Resolve active channel list.

    Priority:
      1. ``MAIL_BACKENDS`` env (explicit list)
      2. CLI choice: ``auto`` → all available; single name → that channel only
    """
    _register_builtins()
    env_list = parse_channel_list(_env("MAIL_BACKENDS"))
    if env_list:
        return env_list

    raw = (choice or "auto").strip().lower()
    if raw in {"", "auto", "all", "*"}:
        found = detect_available_channels()
        # Always keep at least tempmail if registered (free fallback).
        if not found:
            if "tempmail" in _REGISTRY:
                return ["tempmail"]
            raise RuntimeError(
                "no mail channels available; configure at least one backend "
                f"(registered: {known_channels()})"
            )
        return found

    if raw not in _REGISTRY:
        raise ValueError(f"unknown email backend {raw!r}; use auto|{'|'.join(known_channels())}")
    return [raw]


def _parse_kv_overrides(raw: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for part in re.split(r"[,，\s]+", raw or ""):
        if not part or ":" not in part:
            continue
        k, v = part.split(":", 1)
        k = k.strip().lower()
        try:
            out[k] = float(v.strip())
        except ValueError:
            continue
    return out


def channel_weights(channels: Sequence[str]) -> Dict[str, int]:
    """Weights for the given set. Override: MAIL_CHANNEL_WEIGHTS=tempmail:100,yyds:40."""
    weights: Dict[str, int] = {}
    for c in channels:
        spec = get_channel(c)
        weights[c] = int(spec.weight if spec else 50)
    for k, v in _parse_kv_overrides(_env("MAIL_CHANNEL_WEIGHTS")).items():
        if k in weights:
            weights[k] = max(1, min(1000, int(v)))
    return weights


def channel_capacity(name: str) -> int:
    """Capacity for one channel. Override: MAIL_CHANNEL_CAPACITY=tempmail:3,yyds:2."""
    overrides = _parse_kv_overrides(_env("MAIL_CHANNEL_CAPACITY"))
    n = (name or "").strip().lower()
    if n in overrides:
        return max(1, min(32, int(overrides[n])))
    spec = get_channel(n)
    if spec is not None:
        return spec.resolved_capacity()
    return 1


def create_on_channel(name: str) -> Tuple[str, Any]:
    """Direct create on one registered channel (no router)."""
    spec = get_channel(name)
    if spec is None or spec.create is None:
        raise ValueError(f"unknown or incomplete mail channel: {name!r}")
    return spec.create()


def create_mailbox(choice: str = "auto", *, log: Optional[Callable[[str], None]] = None) -> Mailbox:
    """One-shot create: single channel direct, multi via router."""
    channels = resolve_channels(choice)
    if len(channels) == 1:
        email, receiver = create_on_channel(channels[0])
        return Mailbox(
            email=str(email or "").strip(),
            receiver=receiver,
            channel=channels[0],
        )
    return build_router(channels, log=log).create()


# ---------------------------------------------------------------------------
# Error classification / probes
# ---------------------------------------------------------------------------


def classify_error(exc: BaseException) -> Tuple[str, float]:
    """Return (kind, retry_after). kind: rate | fail."""
    if isinstance(exc, ChannelBusy):
        return "rate", float(exc.retry_after)
    msg = str(exc).lower()
    if any(
        k in msg
        for k in (
            "rate limit",
            "rate limited",
            "429",
            "too many",
            "free-tier pacing",
            "pacing",
            "slow down",
            "quota",
        )
    ):
        retry = 3.0
        m = re.search(r"retry[_\s-]?after[=:\s]+([0-9.]+)", msg)
        if m:
            try:
                retry = max(0.2, float(m.group(1)))
            except ValueError:
                pass
        return "rate", retry
    if any(k in msg for k in ("timeout", "timed out", "connection", "temporarily")):
        return "fail", 2.5
    return "fail", 2.0


def _probe_ready_in(channel: str) -> float:
    spec = get_channel(channel)
    if spec is None or spec.ready_in is None:
        return 0.0
    try:
        return max(0.0, float(spec.ready_in()))
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


@dataclass
class _ChannelState:
    name: str
    weight: int = 50
    capacity: int = 1
    rate_free_at: float = 0.0
    penalty_until: float = 0.0
    created_ok: int = 0
    created_fail: int = 0
    busy_hits: int = 0
    in_flight: int = 0
    last_error: str = ""


class ChannelRouter:
    """Priority + capacity overflow router.

    * 1 channel → allow blocking waits inside create (compat with old single-backend path).
    * 2+ channels → never block on free-tier pacing; overflow to ready peers;
      preferred keeps its RPM as slots reopen.
    """

    def __init__(
        self,
        channels: Sequence[str],
        creators: Optional[Dict[str, CreateFn]] = None,
        *,
        log: Optional[Callable[[str], None]] = None,
        weights: Optional[Dict[str, int]] = None,
        capacities: Optional[Dict[str, int]] = None,
    ) -> None:
        _register_builtins()
        chans = [c.strip().lower() for c in channels if c and str(c).strip()]
        if not chans:
            raise ValueError("ChannelRouter requires at least one channel")
        for c in chans:
            if c not in _REGISTRY and not (creators and c in creators):
                raise ValueError(f"unknown mail channel {c!r}; registered={known_channels()}")

        self._channels = chans
        # Creators: explicit map wins; else registry.
        self._creators: Dict[str, CreateFn] = {}
        for c in chans:
            if creators and c in creators:
                self._creators[c] = creators[c]
            else:
                spec = get_channel(c)
                if spec is None or spec.create is None:
                    raise ValueError(f"no create fn for channel {c!r}")
                self._creators[c] = spec.create

        self._log = log or (lambda _m: None)
        self._lock = threading.Lock()
        w = weights or channel_weights(chans)
        self._state = {
            c: _ChannelState(
                name=c,
                weight=int(w.get(c, 50)),
                capacity=int((capacities or {}).get(c, channel_capacity(c))),
            )
            for c in chans
        }
        # Single-channel: allow create() to wait (free pacing / retries).
        # Multi: fail over immediately when preferred has no slot.
        self._allow_wait = len(chans) == 1
        self._single = len(chans) == 1

    @property
    def channels(self) -> List[str]:
        return list(self._channels)

    def summary(self) -> str:
        now = time.time()
        parts = []
        with self._lock:
            for c in self._channels:
                st = self._state[c]
                rate = max(0.0, st.rate_free_at - now)
                pen = max(0.0, st.penalty_until - now)
                mode = "solo" if self._single else "lb"
                parts.append(
                    f"{c}:w={st.weight}/cap={st.capacity}/ok={st.created_ok}"
                    f"/fail={st.created_fail}/busy={st.busy_hits}"
                    + (f"/rate={rate:.1f}s" if rate > 0 else "")
                    + (f"/pen={pen:.1f}s" if pen > 0 else "")
                    + (f"/fly={st.in_flight}" if st.in_flight else "")
                    + f"/{mode}"
                )
        return " ".join(parts)

    def _slot_wait(self, ch: str, now: float) -> float:
        st = self._state[ch]
        wait = max(0.0, st.rate_free_at - now)
        # Solo mode: don't soft-block on probe — create() may block itself.
        if not self._single:
            wait = max(wait, _probe_ready_in(ch))
            if st.in_flight >= max(1, st.capacity):
                wait = max(wait, 0.05)
        return wait

    def _pick(self, now: float, exclude: set[str]) -> Tuple[Optional[str], float]:
        if self._single:
            # Only one option (unless hard-excluded this pass).
            for c in self._channels:
                if c not in exclude:
                    return c, 0.0
            return None, 0.0

        ready: List[_ChannelState] = []
        waiting: List[Tuple[float, _ChannelState]] = []
        for c in self._channels:
            if c in exclude:
                continue
            st = self._state[c]
            wait = self._slot_wait(c, now)
            if wait <= 0.02:
                ready.append(st)
            else:
                waiting.append((wait, st))

        if ready:

            def score(st: _ChannelState) -> tuple:
                penalized = 1 if st.penalty_until > now else 0
                headroom = max(0, st.capacity - st.in_flight)
                return (-st.weight, -headroom, penalized, st.created_ok)

            ready.sort(key=score)
            return ready[0].name, 0.0

        if not waiting:
            return None, 0.0

        def wait_score(item: Tuple[float, _ChannelState]) -> tuple:
            wait, st = item
            return (wait / max(st.weight, 1), wait, -st.weight)

        waiting.sort(key=wait_score)
        wait, st = waiting[0]
        return st.name, min(wait, 1.5)

    def create(self) -> Mailbox:
        """Create one inbox (solo wait or multi prefer+overflow)."""
        deadline = time.time() + 90.0
        last_err: Optional[BaseException] = None
        hard_exclude: set[str] = set()

        while time.time() < deadline:
            now = time.time()
            with self._lock:
                ch, sleep_s = self._pick(now, hard_exclude)
                if ch is None:
                    break
                st = self._state[ch]
                st.in_flight += 1

            if sleep_s > 0:
                time.sleep(sleep_s)

            try:
                prev = os.environ.get("MAIL_CREATE_ALLOW_WAIT")
                os.environ["MAIL_CREATE_ALLOW_WAIT"] = "1" if self._allow_wait else "0"
                try:
                    email, receiver = self._creators[ch]()
                finally:
                    if prev is None:
                        os.environ.pop("MAIL_CREATE_ALLOW_WAIT", None)
                    else:
                        os.environ["MAIL_CREATE_ALLOW_WAIT"] = prev
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                kind, retry = classify_error(exc)
                with self._lock:
                    st = self._state[ch]
                    st.in_flight = max(0, st.in_flight - 1)
                    st.last_error = str(exc)[:160]
                    if kind == "rate":
                        st.busy_hits += 1
                        st.rate_free_at = max(st.rate_free_at, time.time() + retry)
                    else:
                        st.created_fail += 1
                        st.penalty_until = max(st.penalty_until, time.time() + retry)
                        if not self._single:
                            hard_exclude.add(ch)
                if self._single:
                    # Solo: short backoff then retry same channel (old behavior).
                    self._log(f"mail channel {ch} {kind}: {exc}; retry")
                    time.sleep(min(retry, 2.0))
                    hard_exclude.clear()
                else:
                    self._log(
                        f"mail channel {ch} {kind}: {exc} (next_slot≈{retry:.1f}s; overflow/retry)"
                    )
                continue

            email = str(email or "").strip()
            with self._lock:
                st = self._state[ch]
                st.in_flight = max(0, st.in_flight - 1)
                if not email or receiver is None:
                    st.created_fail += 1
                    st.penalty_until = max(st.penalty_until, time.time() + 1.5)
                    last_err = RuntimeError(f"{ch} create returned empty")
                    continue
                st.created_ok += 1

            self._log(f"mail create via {ch}: {email}")
            return Mailbox(
                email=email,
                receiver=receiver,
                channel=ch,
                created_at=time.time(),
            )

        raise RuntimeError(f"all mail channels failed/busy ({self.summary()}); last={last_err}")


def build_router(
    channels: Optional[Sequence[str]] = None,
    *,
    choice: str = "auto",
    log: Optional[Callable[[str], None]] = None,
    creators: Optional[Dict[str, CreateFn]] = None,
) -> ChannelRouter:
    """Build a router for ``channels`` or ``resolve_channels(choice)``."""
    chans = list(channels) if channels is not None else resolve_channels(choice)
    return ChannelRouter(chans, creators=creators, log=log)
