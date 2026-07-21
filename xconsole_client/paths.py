# -*- coding: utf-8 -*-
"""Project-root path anchors (package-relative, no cwd dependence)."""

from __future__ import annotations

from pathlib import Path

# xconsole_client/ → repository root
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def project_root() -> Path:
    return PROJECT_ROOT


def sso_output_dir() -> Path:
    """Default ``sso_output/`` under the repo root."""
    return PROJECT_ROOT / "sso_output"


def oauth_output_dir() -> Path:
    return PROJECT_ROOT / "oauth_output"


def cliproxyapi_auth_dir() -> Path:
    """Default ``cliproxyapi_auth/`` under the repo root (overridable by env)."""
    return PROJECT_ROOT / "cliproxyapi_auth"


def turnstile_extension_dir() -> Path:
    """Chrome MV2 extension used by Drission (``extensions/turnstilePatch``)."""
    return PROJECT_ROOT / "extensions" / "turnstilePatch"


def alias_mail_dir() -> Path:
    """Optional Cloudflare alias mailbox helper (``contrib/alias_mail``)."""
    return PROJECT_ROOT / "contrib" / "alias_mail"


def tools_dir() -> Path:
    return PROJECT_ROOT / "tools"
