#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pre-commit privacy and secret detector for grok-build-auth.

Inspects staged files (via `git show :<file>`) to block accidental commits of:
1. Sensitive files (.env, sso_output/, accounts_output/, private keys, etc.)
2. API keys, JWT tokens, private keys, SSH keys, credentials in code/docs.

Bypass a false positive on a line by adding: `# secret-check:ignore` or `pragma: allowlist secret`
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

# ---------------------------------------------------------------------------
# 1. Path Rules (files/directories that should NEVER be committed)
# ---------------------------------------------------------------------------
FORBIDDEN_PATH_PATTERNS = [
    # Env files (except .env.example)
    (re.compile(r"^\.env(?:\..+)?$"), ".env file (use .env.example for template)"),
    # Runtime credential export directories
    (
        re.compile(
            r"^(?:sso_output|oauth_output|accounts_output|cliproxyapi_auth|cliproxyapi_auth_failed)/"
        ),
        "runtime credential output directory",
    ),
    # Private Key / Certificate files
    (
        re.compile(r"\.(?:pem|key|pkcs12|pfx|p12|asc)$", re.IGNORECASE),
        "private key / certificate file",
    ),
    (re.compile(r"(?:^|/)(?:id_rsa|id_ed25519|id_ecdsa|id_dsa)$"), "SSH private key file"),
    # Local proxy / account dumps
    (re.compile(r"^\.proxy_geo_cache\.json$"), "local proxy geo cache"),
]

# Path exceptions (e.g., .env.example is allowed)
ALLOWED_PATH_EXCEPTIONS = [
    re.compile(r"^\.env\.example$"),
]

# ---------------------------------------------------------------------------
# 2. Content Patterns (secrets / private data inside staged files)
# ---------------------------------------------------------------------------
SECRET_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("Private Key Header", re.compile(r"-----BEGIN (?:[A-Z0-9\s]+ )?PRIVATE KEY")),
    (
        "GitHub Token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[a-zA-Z0-9_]{20,}\b"),
    ),
    ("OpenAI API Key", re.compile(r"\bsk-(?:proj-|none-)?[a-zA-Z0-9]{32,}\b")),
    ("Anthropic API Key", re.compile(r"\bsk-ant-[a-zA-Z0-9_\-]{20,}\b")),
    ("AWS Access Key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "AWS Secret Access Key",
        re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?"),
    ),
    ("Slack Token", re.compile(r"\bxox[baprs]-[a-zA-Z0-9_\-]{10,}\b")),
    ("Google API Key", re.compile(r"\bAIzaSy[a-zA-Z0-9_\-]{35}\b")),
    ("Stripe Secret Key", re.compile(r"\bsk_live_[a-zA-Z0-9]{24,}\b")),
    (
        "JWT Token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    (
        "Hardcoded Secret / API Key Assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret[_-]?key|auth[_-]?token|access[_-]?token|private[_-]?key|bearer[_-]?token|yyds[_-]?key|yyds[_-]?jwt|tempmail[_-]?key|cloudflare[_-]?token)\s*[:=]\s*['\"]([^'\"\$\{\s]{12,})['\"]"
        ),
    ),
]

# Placeholders / mock tokens that are harmless
SAFE_PLACEHOLDERS = [
    "example",
    "placeholder",
    "your_key",
    "your-key",
    "your_token",
    "your-token",
    "change_me",
    "changeme",
    "dummy",
    "test_key",
    "xxx",
    "***",
    "<key>",
    "<token>",
    "0000000",
    "1234567",
    "abcdef",
]

# Binary file extensions to skip scanning content
BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".7z",
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dylib",
    ".dll",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
}

# Ignore comment markers
IGNORE_PRAGMAS = ["secret-check:ignore", "pragma: allowlist secret", "noqa: secret"]


def is_path_forbidden(rel_path: str) -> str | None:
    """Return reason string if file path is forbidden, else None."""
    norm = rel_path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    for allowed in ALLOWED_PATH_EXCEPTIONS:
        if allowed.match(norm):
            return None
    for pattern, reason in FORBIDDEN_PATH_PATTERNS:
        if pattern.search(norm):
            return f"Forbidden staged path matched ({reason}): {norm}"
    return None


def get_staged_files() -> List[str]:
    """Get list of staged files from git (Added, Copied, Modified)."""
    try:
        res = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
            check=True,
        )
        return [f.strip() for f in res.stdout.splitlines() if f.strip()]
    except Exception as exc:
        print(f"⚠️ secret-check: failed to get git staged files: {exc}", file=sys.stderr)
        return []


def get_staged_content(rel_path: str) -> str | None:
    """Read file content as staged in git index (`git show :file`)."""
    try:
        res = subprocess.run(
            ["git", "show", f":{rel_path}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if res.returncode == 0:
            return res.stdout
    except Exception:
        pass
    return None


def check_line_for_secret(line: str) -> Tuple[str, str] | None:
    """Check a single line of text. Return (rule_name, matched_text) or None."""
    # Check ignore pragmas
    if any(pragma in line for pragma in IGNORE_PRAGMAS):
        return None

    # Check safe placeholder keywords
    line_lower = line.lower()

    for rule_name, pattern in SECRET_PATTERNS:
        match = pattern.search(line)
        if not match:
            continue

        matched_str = match.group(0)
        matched_str_lower = matched_str.lower()

        # If rule matched group 1 (e.g. value in assignment), check that
        if match.groups():
            val = match.group(1)
            val_lower = val.lower()
            if any(p in val_lower for p in SAFE_PLACEHOLDERS):
                continue

        # Check general placeholder keywords
        if any(p in matched_str_lower for p in SAFE_PLACEHOLDERS):
            continue
        if any(
            p in line_lower
            for p in [
                "# example",
                "// example",
                "default_api",
                "env.example",
                "redacted",
            ]
        ):
            continue

        return rule_name, matched_str

    return None


def main() -> int:
    # If file paths passed via CLI args, use those; otherwise query git
    staged_files = [f for f in sys.argv[1:] if f and not f.startswith("-")]
    if not staged_files:
        staged_files = get_staged_files()

    if not staged_files:
        return 0

    violations: List[str] = []

    for rel_path in staged_files:
        # 1. Path check
        path_err = is_path_forbidden(rel_path)
        if path_err:
            violations.append(f"❌ {path_err}")
            continue

        # Skip binary files for content check
        ext = Path(rel_path).suffix.lower()
        if ext in BINARY_EXTENSIONS:
            continue

        # 2. Content check on staged version
        content = get_staged_content(rel_path)
        if content is None:
            continue

        for line_num, line in enumerate(content.splitlines(), 1):
            res = check_line_for_secret(line)
            if res:
                rule_name, matched_str = res
                # Mask secret string for output
                masked = (
                    matched_str[:4] + "..." + matched_str[-4:] if len(matched_str) > 10 else "***"
                )
                violations.append(f"❌ {rel_path}:{line_num} — Detected {rule_name}: `{masked}`")

    if violations:
        print("\n" + "=" * 60, file=sys.stderr)
        print("🚨 PRE-COMMIT PRIVACY / SECRET CHECK FAILED", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print(
            "The following potential secrets or forbidden files were detected:\n",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print("\n" + "-" * 60, file=sys.stderr)
        print("💡 How to resolve:", file=sys.stderr)
        print(
            "  1. Remove sensitive files from staging: git reset HEAD <file>",
            file=sys.stderr,
        )
        print(
            "  2. Remove hardcoded API keys / tokens from code / config",
            file=sys.stderr,
        )
        print(
            "  3. For genuine false positives, add `# secret-check:ignore` on the line",
            file=sys.stderr,
        )
        print("=" * 60 + "\n", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
