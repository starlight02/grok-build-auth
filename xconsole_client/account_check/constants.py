# -*- coding: utf-8 -*-
"""Shared defaults for Grok Build account probes."""

from __future__ import annotations


DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"

DEFAULT_HEADERS = {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": "0.2.93",
    "x-grok-client-identifier": "grok-shell",
}

PROBE_MODEL = "grok-4.5"

PROBE_BODY = {
    "model": PROBE_MODEL,
    "input": "Reply exactly: OK",
    "max_output_tokens": 8,
}
