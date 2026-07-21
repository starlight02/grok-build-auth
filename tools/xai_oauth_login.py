#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run xAI/Grok OAuth PKCE login and save tokens to oauth_output/."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from xconsole_client.xai_oauth import main


if __name__ == "__main__":
    raise SystemExit(main())
