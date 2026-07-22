# -*- coding: utf-8 -*-
"""x.ai email verification code extraction (single source of truth)."""

from __future__ import annotations

import re
from typing import Optional

# Order of patterns tried (return first match):
#   1. dashed current format "LSQ-OPU"
#   2. (?i)\b[A-Z0-9]{6}\b
#   3. (?i)\b[A-Z0-9]{8}\b
#   4. keyword-anchored 4-8 chars
#
# Pure-digit matches (e.g. "123456") are rejected.
# Fallback: length-based run detector for long unspaced alnum strings.
_XAI_CODE_PATTERNS = (
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Z0-9])"),
    re.compile(r"(?i)\b[A-Z0-9]{6}\b"),
    re.compile(r"(?i)\b[A-Z0-9]{8}\b"),
    re.compile(
        r"(?i)(?:code|otp|验证码|verification|verify|code is|your code)"
        r"[^A-Za-z0-9]{0,40}([A-Z0-9]{4,8})"
    ),
)

XAI_CODE_LENGTH_PRIMARY = 6
XAI_CODE_LENGTH_FALLBACK = 8
_XAI_RUN_RE = re.compile(r"[A-Za-z0-9]+")


def _is_pure_digits(s: str) -> bool:
    return bool(s) and s.isdigit()


def extract_xai_code(text: str) -> Optional[str]:
    """Return the first plausible x.ai email verification code in *text*.

    >>> extract_xai_code("Your code is XAI0X1") == "XAI0X1"
    True
    >>> extract_xai_code("123456") is None
    True
    """
    if not text:
        return None

    for pat in _XAI_CODE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1) if m.groups() else m.group(0)
        if _is_pure_digits(raw):
            continue
        return raw.upper()

    for m in _XAI_RUN_RE.finditer(text):
        run = m.group(0)
        n = len(run)
        if _is_pure_digits(run):
            continue
        if n == XAI_CODE_LENGTH_PRIMARY:
            return run.upper()
        if n == XAI_CODE_LENGTH_FALLBACK:
            return run.upper()
        if n > XAI_CODE_LENGTH_FALLBACK:
            return run[:XAI_CODE_LENGTH_FALLBACK].upper()
    return None
