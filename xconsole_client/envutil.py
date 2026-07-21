# -*- coding: utf-8 -*-
"""Small env helpers shared across transports and solvers."""

from __future__ import annotations

import os


def env_truthy(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def proxy_from_env(explicit: str = "", *, include_all: bool = True) -> str:
    """Resolve an HTTP(S) proxy URL.

    Preference: explicit argument → HTTPS_PROXY → HTTP_PROXY → (optional) ALL_PROXY,
    including lowercase variants common in shell environments.
    """
    explicit = (explicit or "").strip()
    if explicit:
        return explicit
    keys = [
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "https_proxy",
        "http_proxy",
    ]
    if include_all:
        keys.extend(["ALL_PROXY", "all_proxy"])
    for key in keys:
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return ""
