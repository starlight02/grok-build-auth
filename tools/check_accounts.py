#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone Grok Build account checker (thin CLI wrapper).

Checks whether local auth JSON files can actually call the Build/CLI free
endpoint. Does NOT print tokens / passwords / SSO.

Accepts either:
  - CLIProxyAPI auth files (access_token, base_url, headers, email, disabled)
  - accounts_output bundles (oauth_access_token / cliproxyapi_auth path)

Examples:

  python tools/check_accounts.py cliproxyapi_auth/
  python tools/check_accounts.py accounts_output/account_*.json
  python tools/check_accounts.py cliproxyapi_auth/user@example.com.json --json
  HTTPS_PROXY=http://127.0.0.1:7890 python tools/check_accounts.py cliproxyapi_auth/
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from xconsole_client.account_check.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
