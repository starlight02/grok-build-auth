# -*- coding: utf-8 -*-
"""HTTP(S) proxy pool with **fast host-IP** region lookup.

Primary workflow (large pools):
  1. ``PROXY_POOL_FILE`` — one proxy URL per line
  2. Read proxy **host IP** (no traffic through the proxy)
  3. Batch geo-lookup (ip-api.com batch, 100 IPs/req) — seconds for 1k lines
  4. Keep only ``PROXY_REGION`` matches, round-robin rotate

This is geolocation of the proxy *endpoint address*, which matches static /
datacenter / sticky lists (like ``host:port`` residential gateways). It is
orders of magnitude faster than probing exit-IP through each proxy.

Optional:
  - Manual tag skips lookup: ``us|http://...``
  - ``PROXY_GEO_MODE=exit`` — slow through-proxy exit probe (debug only)
  - ``HTTPS_PROXY`` single-proxy fallback when no pool list

Env:
  PROXY_POOL_FILE          path to list file (one proxy per line)
  PROXY_POOL               inline list (comma / newline)
  PROXY_REGION             target country code (us/jp/hk/…)
  PROXY_POOL_SCOPE         same_region (default) | all
  PROXY_GEO_MODE           host (default, fast) | exit (slow)
  PROXY_GEO_TIMEOUT        batch HTTP timeout seconds (default 20)
  PROXY_GEO_CACHE          cache JSON (default ./.proxy_geo_cache.json)
  PROXY_GEO_REFRESH        1 = ignore cache
  PROXY_GEO_WORKERS        only used for exit mode
  PROXY_PREFLIGHT         1 (default multi-proxy) | 0 — TCP+CONNECT alive check
  PROXY_PREFLIGHT_WORKERS  concurrent probes (default 16)
  PROXY_PREFLIGHT_TIMEOUT  per-proxy seconds (default 8)
  PROXY_RETRY              per-account proxy rotates on transport fail (default 8)

"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse


DEFAULT_REGION = "default"
SCOPE_SAME = "same_region"
SCOPE_ALL = "all"
_DEFAULT_CACHE = ".proxy_geo_cache.json"
_BATCH_URL = "http://ip-api.com/batch?fields=status,message,countryCode,query"
_BATCH_SIZE = 100

# Slow path only (PROXY_GEO_MODE=exit)
_EXIT_PROBE_URLS = (
    "http://ip-api.com/json/?fields=status,countryCode,query",
    "https://ipapi.co/json/",
)


@dataclass(frozen=True)
class ProxyEntry:
    url: str
    region: str = DEFAULT_REGION
    tagged: bool = False  # True if region came from the line (no probe needed)
    exit_ip: str = ""

    @property
    def host(self) -> str:
        try:
            u = urlparse(self.url if "://" in self.url else f"http://{self.url}")
            return u.hostname or ""
        except Exception:
            return ""


def _strip_comment(line: str) -> str:
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return ""
    if " #" in s:
        s = s.split(" #", 1)[0].strip()
    return s


def _normalize_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    u = urlparse(url)
    if not u.hostname:
        return ""
    return url


def _normalize_region(raw: str) -> str:
    r = (raw or "").strip().lower()
    # accept "US", "us-east" → country-ish token; keep simple ISO-like codes
    if not r:
        return DEFAULT_REGION
    # map common aliases
    aliases = {
        "usa": "us",
        "united states": "us",
        "unitedstates": "us",
        "uk": "gb",
        "britain": "gb",
        "great britain": "gb",
        "korea": "kr",
        "south korea": "kr",
        "hongkong": "hk",
        "hong kong": "hk",
        "taiwan": "tw",
        "macau": "mo",
        "macao": "mo",
    }
    if r in aliases:
        return aliases[r]
    # take leading alpha token (us, jp, hk, …)
    m = re.match(r"^([a-z]{2})", r)
    if m and r in aliases or (len(r) == 2 and r.isalpha()):
        return r
    if m and len(r) <= 5:
        return r
    return aliases.get(r, r)


def parse_proxy_line(line: str) -> Optional[ProxyEntry]:
    """Parse one line. Bare URL → untagged (region to be probed)."""
    s = _strip_comment(line)
    if not s:
        return None

    region = DEFAULT_REGION
    url_raw = s
    tagged = False

    if "|" in s:
        left, right = s.split("|", 1)
        if right.strip():
            region = _normalize_region(left)
            url_raw = right.strip()
            tagged = True
    elif "," in s and not s.lower().startswith(
        ("http://", "https://", "socks5://", "socks4://")
    ):
        left, right = s.split(",", 1)
        if right.strip() and ("://" in right or "." in right):
            region = _normalize_region(left)
            url_raw = right.strip()
            tagged = True
    else:
        parts = s.split(None, 1)
        if len(parts) == 2 and "://" not in parts[0] and (
            "://" in parts[1] or parts[1][0].isdigit() or "." in parts[1]
        ):
            # only treat as tag if left looks like a region code, not user:pass
            left = parts[0]
            if re.fullmatch(r"[A-Za-z]{2,8}(-[A-Za-z0-9]+)?", left) and ":" not in left:
                region = _normalize_region(left)
                url_raw = parts[1].strip()
                tagged = True

    url = _normalize_url(url_raw)
    if not url:
        return None
    return ProxyEntry(url=url, region=region if tagged else DEFAULT_REGION, tagged=tagged)


def parse_proxy_text(text: str) -> List[ProxyEntry]:
    out: List[ProxyEntry] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        ent = parse_proxy_line(line)
        if ent is None:
            continue
        if ent.url in seen:
            continue
        seen.add(ent.url)
        out.append(ent)
    return out


def load_proxy_entries(
    *,
    pool_env: Optional[str] = None,
    pool_file: Optional[str] = None,
    single_proxy: Optional[str] = None,
) -> List[ProxyEntry]:
    """Load from PROXY_POOL / PROXY_POOL_FILE / single fallback."""
    entries: List[ProxyEntry] = []

    text = pool_env if pool_env is not None else (os.environ.get("PROXY_POOL") or "")
    if text.strip():
        chunks: List[str] = []
        for part in text.replace(";", "\n").replace(",", "\n").splitlines():
            part = part.strip()
            if part:
                chunks.append(part)
        entries.extend(parse_proxy_text("\n".join(chunks)))

    path = pool_file if pool_file is not None else (os.environ.get("PROXY_POOL_FILE") or "").strip()
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"PROXY_POOL_FILE not found: {p}")
        entries.extend(parse_proxy_text(p.read_text(encoding="utf-8", errors="replace")))

    deduped: List[ProxyEntry] = []
    seen: set[str] = set()
    for ent in entries:
        if ent.url in seen:
            continue
        seen.add(ent.url)
        deduped.append(ent)

    if deduped:
        return deduped

    single = (
        single_proxy
        if single_proxy is not None
        else single_proxy_from_env()
    )
    if single:
        ent = parse_proxy_line(single)
        if ent is not None:
            return [ent]
    return []


def resolve_pool_scope(raw: Optional[str] = None) -> str:
    v = (raw if raw is not None else (os.environ.get("PROXY_POOL_SCOPE") or "")).strip().lower()
    if v in {SCOPE_ALL, "any", "global", "*"}:
        return SCOPE_ALL
    return SCOPE_SAME


def _cache_path() -> Path:
    raw = (os.environ.get("PROXY_GEO_CACHE") or _DEFAULT_CACHE).strip()
    return Path(raw).expanduser()


def _load_geo_cache() -> Dict[str, Any]:
    path = _cache_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_geo_cache(cache: Dict[str, Any]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _cache_key_host(host: str) -> str:
    return f"host:{host.strip().lower()}"


def _cache_key_url(url: str) -> str:
    try:
        u = urlparse(url if "://" in url else f"http://{url}")
        host = (u.hostname or "").lower()
        port = u.port or ""
        return f"url:{u.scheme}://{host}:{port}"
    except Exception:
        return f"url:{url}"


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _resolve_host_ip(host: str) -> str:
    host = (host or "").strip().strip("[]")
    if not host:
        return ""
    if _is_ip(host):
        return host
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        for info in infos:
            addr = info[4][0]
            if addr:
                return addr
    except Exception:
        return ""
    return ""


def _http_json(method: str, url: str, *, data: Optional[bytes] = None, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "User-Agent": "grok-build-auth-proxy-geo/1.1",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(2_000_000)
    return json.loads(raw.decode("utf-8", errors="replace"))


def batch_lookup_host_countries(
    hosts: Sequence[str],
    *,
    timeout: float = 20.0,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, str]:
    """Map host/IP → ISO country via ip-api batch (direct, not through proxies)."""
    _log = log or (lambda msg: None)
    host_ip: Dict[str, str] = {}
    for h in hosts:
        h = (h or "").strip().lower()
        if not h or h in host_ip:
            continue
        ip = _resolve_host_ip(h)
        if ip:
            host_ip[h] = ip

    unique_ips = sorted(set(host_ip.values()))
    if not unique_ips:
        return {}

    t0 = time.time()
    chunks: List[List[str]] = [
        unique_ips[i : i + _BATCH_SIZE] for i in range(0, len(unique_ips), _BATCH_SIZE)
    ]
    _log(f"geo-batch {len(unique_ips)} unique IPs in {len(chunks)} request(s)")

    ip_country: Dict[str, str] = {}

    def _one(chunk: List[str], *, attempt: int = 1) -> None:
        payload = json.dumps([{"query": ip} for ip in chunk]).encode("utf-8")
        try:
            data = _http_json("POST", _BATCH_URL, data=payload, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if attempt < 4 and (
                "429" in msg or "closed" in msg.lower() or "timed out" in msg.lower()
            ):
                time.sleep(2.0 * attempt)
                return _one(chunk, attempt=attempt + 1)
            _log(f"geo-batch chunk failed: {exc}")
            return
        if not isinstance(data, list):
            return
        for row in data:
            if not isinstance(row, dict):
                continue
            if str(row.get("status") or "").lower() != "success":
                continue
            q = str(row.get("query") or "").strip()
            code = row.get("countryCode") or ""
            if q and code:
                ip_country[q] = _normalize_region(str(code))

    for i, chunk in enumerate(chunks):
        if i:
            time.sleep(1.5)  # free ip-api rate limit
        _one(chunk)

    out: Dict[str, str] = {}
    for h, ip in host_ip.items():
        c = ip_country.get(ip)
        if c:
            out[h] = c
            out[ip] = c
    _log(
        f"geo-batch done hosts={len(hosts)} ips={len(unique_ips)} "
        f"hit={len(ip_country)} in {time.time() - t0:.2f}s"
    )
    return out




def _parse_country_from_body(body: str) -> Tuple[str, str]:
    body = (body or "").strip()
    if not body:
        return "", ""
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except Exception:
            data = {}
        if isinstance(data, dict):
            code = data.get("countryCode") or data.get("country_code") or ""
            ip = str(data.get("query") or data.get("ip") or "")
            if isinstance(code, str) and len(code) >= 2:
                return _normalize_region(str(code)), ip
    return "", ""


def probe_exit_region(
    proxy_url: str,
    *,
    timeout: float = 8.0,
) -> Tuple[str, str]:
    """Slow: probe exit country **through** the proxy. Prefer host batch mode."""
    proxy_url = _normalize_url(proxy_url)
    if not proxy_url:
        return "", ""
    timeout = max(3.0, float(timeout))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )
    for probe in _EXIT_PROBE_URLS:
        try:
            req = urllib.request.Request(
                probe,
                headers={"User-Agent": "grok-build-auth-proxy-probe/1.1", "Accept": "application/json"},
                method="GET",
            )
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read(65536).decode("utf-8", errors="replace")
            region, exit_ip = _parse_country_from_body(body)
            if region and region != DEFAULT_REGION:
                return region, exit_ip
        except Exception:
            continue
    return "", ""


def resolve_regions(
    entries: Sequence[ProxyEntry],
    *,
    workers: Optional[int] = None,
    timeout: Optional[float] = None,
    refresh: Optional[bool] = None,
    log: Optional[Callable[[str], None]] = None,
    mode: Optional[str] = None,
) -> List[ProxyEntry]:
    """Fill ``region`` for untagged entries.

    Default ``mode=host``: batch-lookup proxy host IPs (fast).
    ``mode=exit``: through-proxy probes (slow, only for small lists).
    """
    if not entries:
        return []

    mode_v = (mode or os.environ.get("PROXY_GEO_MODE") or "host").strip().lower()
    if mode_v in {"exit", "through", "live"}:
        mode_v = "exit"
    else:
        mode_v = "host"

    try:
        timeout_f = float(timeout if timeout is not None else (os.environ.get("PROXY_GEO_TIMEOUT") or "20"))
    except ValueError:
        timeout_f = 20.0

    if refresh is None:
        refresh = (os.environ.get("PROXY_GEO_REFRESH") or "").strip().lower() in {
            "1", "true", "yes", "on"
        }

    _log = log or (lambda msg: None)
    cache = {} if refresh else _load_geo_cache()
    out: List[Optional[ProxyEntry]] = [None] * len(entries)
    need_idx: List[int] = []

    for i, ent in enumerate(entries):
        if ent.tagged and ent.region and ent.region != DEFAULT_REGION:
            out[i] = ent
            continue
        host = ent.host.lower()
        # prefer host-ip cache, then url cache
        hit = None
        if host:
            h = cache.get(_cache_key_host(host))
            if isinstance(h, dict) and h.get("region"):
                hit = h
        if hit is None:
            u = cache.get(_cache_key_url(ent.url))
            if isinstance(u, dict) and u.get("region"):
                hit = u
        if hit is not None:
            out[i] = ProxyEntry(
                url=ent.url,
                region=_normalize_region(str(hit.get("region"))),
                tagged=False,
                exit_ip=str(hit.get("ip") or host),
            )
        else:
            need_idx.append(i)

    if not need_idx:
        return [e for e in out if e is not None]

    if mode_v == "host":
        hosts = []
        for i in need_idx:
            h = entries[i].host
            if h:
                hosts.append(h)
        mapping = batch_lookup_host_countries(hosts, timeout=timeout_f, log=_log)
        ok = fail = 0
        for i in need_idx:
            ent = entries[i]
            host = ent.host.lower()
            region = mapping.get(host) or mapping.get(_resolve_host_ip(host)) or ""
            if region:
                ok += 1
                ip = _resolve_host_ip(host) or host
                out[i] = ProxyEntry(url=ent.url, region=region, tagged=False, exit_ip=ip)
                if host:
                    cache[_cache_key_host(host)] = {
                        "region": region,
                        "ip": ip,
                        "ts": int(time.time()),
                        "mode": "host",
                    }
            else:
                fail += 1
                out[i] = ProxyEntry(url=ent.url, region="unknown", tagged=False, exit_ip="")
                if host:
                    cache[_cache_key_host(host)] = {
                        "region": "unknown",
                        "ip": host,
                        "ts": int(time.time()),
                        "mode": "host",
                    }
        _log(f"geo-host assigned ok={ok} fail={fail}")
        _save_geo_cache(cache)
    else:
        # slow exit path
        try:
            workers_n = int(workers if workers is not None else (os.environ.get("PROXY_GEO_WORKERS") or "32"))
        except ValueError:
            workers_n = 32
        workers_n = max(1, min(64, workers_n))
        _log(f"geo-exit probe {len(need_idx)}/{len(entries)} (slow path, workers={workers_n})")
        ok = fail = 0

        def _one(i: int) -> Tuple[int, str, str]:
            region, ip = probe_exit_region(entries[i].url, timeout=min(timeout_f, 8.0))
            return i, region, ip

        with ThreadPoolExecutor(max_workers=workers_n) as ex:
            futs = [ex.submit(_one, i) for i in need_idx]
            for fut in as_completed(futs):
                i, region, ip = fut.result()
                ent = entries[i]
                if region:
                    ok += 1
                    out[i] = ProxyEntry(url=ent.url, region=region, tagged=False, exit_ip=ip)
                    cache[_cache_key_url(ent.url)] = {
                        "region": region,
                        "ip": ip,
                        "ts": int(time.time()),
                        "mode": "exit",
                    }
                else:
                    fail += 1
                    out[i] = ProxyEntry(url=ent.url, region="unknown", tagged=False, exit_ip="")
        _log(f"geo-exit done ok={ok} fail={fail}")
        _save_geo_cache(cache)

    return [e for e in out if e is not None]



class ProxyPool:
    """Thread-safe round-robin pool over region-filtered proxies.

    Dead proxies are marked bad and skipped for the rest of the process.
    """

    def __init__(
        self,
        entries: Sequence[ProxyEntry],
        *,
        scope: str = SCOPE_SAME,
        region: Optional[str] = None,
    ) -> None:
        if not entries:
            raise ValueError("ProxyPool requires at least one proxy entry")
        self._all: List[ProxyEntry] = list(entries)
        self._scope = resolve_pool_scope(scope)
        self._by_region: Dict[str, List[ProxyEntry]] = defaultdict(list)
        for e in self._all:
            self._by_region[e.region].append(e)
        self._lock = threading.Lock()
        self._rr: Dict[str, int] = defaultdict(int)
        self._acquired = 0
        self._disabled: set[str] = set()
        self._disabled_reasons: Dict[str, str] = {}

        want = ""
        if region is not None and str(region).strip():
            want = _normalize_region(region)
        elif (os.environ.get("PROXY_REGION") or "").strip():
            want = _normalize_region(os.environ.get("PROXY_REGION") or "")

        if self._scope == SCOPE_ALL:
            self._region = want or "*"
        else:
            self._region = self._choose_region(want)

        if self._scope == SCOPE_SAME and not self._active_list():
            available = ", ".join(f"{r}:{len(v)}" for r, v in sorted(self._by_region.items()))
            raise RuntimeError(
                f"no proxies in region={self._region!r}; available: {available or '(none)'}"
            )

    def _choose_region(self, preferred: str) -> str:
        preferred = _normalize_region(preferred) if preferred else ""
        if preferred:
            # Explicit PROXY_REGION: never silently fall back to another country.
            return preferred
        usable = {
            r: v
            for r, v in self._by_region.items()
            if r not in {DEFAULT_REGION, "unknown", ""} and v
        }
        if not usable:
            usable = {r: v for r, v in self._by_region.items() if v}
        counts = Counter({r: len(v) for r, v in usable.items()})
        if not counts:
            return DEFAULT_REGION
        best_n = max(counts.values())
        for e in self._all:
            if e.region in usable and counts[e.region] == best_n:
                return e.region
        return next(iter(usable))

    @classmethod
    def from_env(
        cls,
        *,
        log: Optional[Callable[[str], None]] = None,
        probe: bool = True,
    ) -> Optional["ProxyPool"]:
        """Load list, probe regions, filter to PROXY_REGION (same_region default)."""
        _log = log or (lambda msg: None)
        entries = load_proxy_entries()
        if not entries:
            return None
        # Single bare HTTPS_PROXY with no pool file → no need to probe for rotation
        only_single = (
            not (os.environ.get("PROXY_POOL") or "").strip()
            and not (os.environ.get("PROXY_POOL_FILE") or "").strip()
            and len(entries) == 1
        )
        if probe and not only_single:
            need = [e for e in entries if not e.tagged]
            if need:
                entries = resolve_regions(entries, log=log)
            else:
                entries = list(entries)
        elif only_single and not entries[0].tagged:
            pass

        scope = resolve_pool_scope()
        region = (os.environ.get("PROXY_REGION") or "").strip() or None
        pool = cls(entries, scope=scope, region=region)
        if not only_single and _preflight_enabled(len(pool._active_list())):
            pool.preflight(log=_log)
        return pool

    @property
    def scope(self) -> str:
        return self._scope

    @property
    def region(self) -> str:
        return self._region

    @property
    def size(self) -> int:
        return len(self._active_list())

    @property
    def total(self) -> int:
        return len(self._all)

    @property
    def disabled_count(self) -> int:
        with self._lock:
            return len(self._disabled)

    def regions(self) -> Dict[str, int]:
        return {r: len(v) for r, v in sorted(self._by_region.items())}

    def _region_candidates(self) -> List[ProxyEntry]:
        if self._scope == SCOPE_ALL:
            return [e for e in self._all if e.region not in {"unknown"}] or list(self._all)
        return list(self._by_region.get(self._region) or [])

    def _active_list(self) -> List[ProxyEntry]:
        return [e for e in self._region_candidates() if e.url not in self._disabled]

    def set_region(self, region: str) -> str:
        with self._lock:
            if self._scope == SCOPE_ALL:
                return self._region
            self._region = self._choose_region(region)
            if not self._active_list():
                raise RuntimeError(f"no proxies in region={self._region!r}")
            return self._region

    def mark_bad(self, url: str, reason: str = "") -> bool:
        """Disable *url* for the rest of this process. True if newly disabled."""
        url = (url or "").strip()
        if not url:
            return False
        with self._lock:
            if url in self._disabled:
                return False
            self._disabled.add(url)
            if reason:
                self._disabled_reasons[url] = reason[:200]
            return True

    def acquire(self) -> ProxyEntry:
        with self._lock:
            items = self._active_list()
            if not items:
                dead = len(self._disabled)
                raise RuntimeError(
                    f"proxy pool exhausted for region={self._region!r} "
                    f"(disabled={dead}, total_region={len(self._region_candidates())})"
                )
            key = "*" if self._scope == SCOPE_ALL else self._region
            # Skip any race-disabled entries within one full cycle.
            for _ in range(len(items)):
                idx = self._rr[key] % len(items)
                self._rr[key] = idx + 1
                ent = items[idx]
                if ent.url not in self._disabled:
                    self._acquired += 1
                    return ent
            raise RuntimeError("proxy pool has no live entries for active region")

    def preflight(
        self,
        *,
        log: Optional[Callable[[str], None]] = None,
        workers: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[int, int]:
        """Probe active-region proxies; mark dead ones. Returns (ok, fail)."""
        _log = log or (lambda msg: None)
        items = list(self._active_list())
        if not items:
            return 0, 0
        try:
            workers_n = int(workers if workers is not None else (os.environ.get("PROXY_PREFLIGHT_WORKERS") or "16"))
        except ValueError:
            workers_n = 16
        workers_n = max(1, min(64, workers_n))
        try:
            timeout_f = float(
                timeout if timeout is not None else (os.environ.get("PROXY_PREFLIGHT_TIMEOUT") or "8")
            )
        except ValueError:
            timeout_f = 8.0
        timeout_f = max(2.0, min(30.0, timeout_f))

        _log(
            f"preflight {len(items)} proxies workers={workers_n} timeout={timeout_f:.0f}s"
        )
        t0 = time.time()
        ok = 0
        fail = 0

        def _one(ent: ProxyEntry) -> Tuple[str, bool, str]:
            good, why = probe_proxy_alive(ent.url, timeout=timeout_f)
            return ent.url, good, why

        with ThreadPoolExecutor(max_workers=workers_n) as ex:
            futs = [ex.submit(_one, e) for e in items]
            for fut in as_completed(futs):
                url, good, why = fut.result()
                if good:
                    ok += 1
                else:
                    fail += 1
                    self.mark_bad(url, why)

        _log(
            f"preflight done ok={ok} fail={fail} live={self.size} "
            f"in {time.time() - t0:.1f}s"
        )
        if self.size == 0:
            raise RuntimeError(
                f"proxy preflight: zero live proxies in region={self._region!r} "
                f"(probed={len(items)}, all failed)"
            )
        return ok, fail

    def summary(self) -> str:
        by = self.regions()
        active = self._active_list()
        dead = self.disabled_count
        dead_s = f" disabled={dead}" if dead else ""
        if self._scope == SCOPE_ALL:
            return f"scope=all active={len(active)}/{len(self._all)}{dead_s} regions={by}"
        return (
            f"scope=same_region region={self._region} "
            f"active={len(active)}/{len(self._all)}{dead_s} regions={by}"
        )


def _preflight_enabled(active_n: int) -> bool:
    raw = (os.environ.get("PROXY_PREFLIGHT") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    # Default: preflight multi-proxy pools (single sticky proxy skips).
    return active_n > 1


def probe_proxy_alive(proxy_url: str, *, timeout: float = 6.0) -> Tuple[bool, str]:
    """Fast liveness: TCP to proxy, then HTTPS GET via proxy to accounts.x.ai."""
    proxy_url = _normalize_url(proxy_url)
    if not proxy_url:
        return False, "empty"
    u = urlparse(proxy_url)
    host = u.hostname or ""
    if not host:
        return False, "no-host"
    port = u.port or (443 if (u.scheme or "").lower() == "https" else 80)
    try:
        with socket.create_connection((host, int(port)), timeout=min(timeout, 4.0)) as sock:
            sock.shutdown(socket.SHUT_RDWR)
    except Exception as exc:  # noqa: BLE001
        return False, f"tcp:{exc.__class__.__name__}"

    # Full CONNECT path used by registration (curl_cffi / urllib).
    try:
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        opener = urllib.request.build_opener(handler)
        req = urllib.request.Request(
            "https://accounts.x.ai/sign-up",
            method="GET",
            headers={
                "User-Agent": "grok-build-auth-proxy-preflight/1.0",
                "Accept": "text/html",
            },
        )
        with opener.open(req, timeout=timeout) as resp:
            # Any HTTP response (even 403/5xx) means the tunnel works.
            _ = resp.status
            return True, "ok"
    except urllib.error.HTTPError:
        # Tunnel + TLS + HTTP happened; proxy is usable for transport.
        return True, "ok-http"
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "timed out" in msg or "timeout" in msg:
            return False, "connect-timeout"
        if "refused" in msg:
            return False, "connect-refused"
        if "aborted" in msg or "tunnel" in msg or "proxy" in msg:
            return False, f"connect:{exc.__class__.__name__}"
        return False, f"connect:{exc.__class__.__name__}"


def is_proxy_transport_error(exc: BaseException) -> bool:
    """True when failure is almost certainly the proxy path, not app logic."""
    msg = str(exc).lower()
    needles = (
        "connection timed out",
        "operation timed out",
        "proxy connect aborted",
        "proxy connect",
        "tunnel connection failed",
        "failed to perform",
        "curl: (28)",
        "curl: (56)",
        "curl: (7)",
        "curl: (35)",
        "curl: (97)",
        "could not resolve proxy",
        "proxy error",
        "connection refused",
        "connection reset",
        "network is unreachable",
        "socks",
        "proxy_pool exhausted",
        # scrape got CF / block page through a bad exit IP
        "cloudflare challenge",
        "cloudflare block",
        "cloudflare js challenge",
        "ip or proxy is",
        "signup page scrape failed",
        "could not scrape signup page metadata",
        "just a moment",
        "attention required",
        "sorry, you have been blocked",
    )
    return any(n in msg for n in needles)


def proxy_retry_limit() -> int:
    raw = (os.environ.get("PROXY_RETRY") or "").strip()
    if raw:
        try:
            return max(1, min(50, int(raw)))
        except ValueError:
            pass
    return 8



def single_proxy_from_env() -> str:
    return (
        (os.environ.get("HTTPS_PROXY") or "").strip()
        or (os.environ.get("HTTP_PROXY") or "").strip()
        or (os.environ.get("https_proxy") or "").strip()
        or (os.environ.get("http_proxy") or "").strip()
    )


__all__ = [
    "DEFAULT_REGION",
    "SCOPE_SAME",
    "SCOPE_ALL",
    "ProxyEntry",
    "ProxyPool",
    "is_proxy_transport_error",
    "load_proxy_entries",
    "parse_proxy_line",
    "parse_proxy_text",
    "probe_exit_region",
    "probe_proxy_alive",
    "proxy_retry_limit",
    "resolve_pool_scope",
    "resolve_regions",
    "single_proxy_from_env",
]
