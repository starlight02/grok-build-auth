# -*- coding: utf-8 -*-
"""Unit tests for scripts/secret_check.py (pre-commit privacy/secret scanner)."""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to import path so we can import scripts.secret_check
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.secret_check import check_line_for_secret, is_path_forbidden


def test_forbidden_paths():
    assert is_path_forbidden(".env") is not None
    assert is_path_forbidden(".env.local") is not None
    assert is_path_forbidden(".env.production") is not None
    assert is_path_forbidden("sso_output/sso_123.json") is not None
    assert is_path_forbidden("accounts_output/acc.json") is not None
    assert is_path_forbidden("cliproxyapi_auth/auth.json") is not None
    assert is_path_forbidden("certs/server.key") is not None
    assert is_path_forbidden("certs/private.pem") is not None
    assert is_path_forbidden(".ssh/id_rsa") is not None
    assert is_path_forbidden(".proxy_geo_cache.json") is not None


def test_allowed_paths():
    assert is_path_forbidden(".env.example") is None
    assert is_path_forbidden("run.py") is None
    assert is_path_forbidden("xconsole_client/client.py") is None
    assert is_path_forbidden("README.md") is None


def test_secret_detection_private_key():
    res = check_line_for_secret("-----BEGIN RSA PRIVATE KEY-----")  # secret-check:ignore
    assert res is not None
    assert res[0] == "Private Key Header"


def test_secret_detection_github_token():
    dummy_tok = "ghp_" + "A" * 36
    res = check_line_for_secret(f"token = '{dummy_tok}'")
    assert res is not None
    assert res[0] == "GitHub Token"


def test_secret_detection_openai_key():
    dummy_key = "sk-proj-" + "B" * 36
    res = check_line_for_secret(f"key = '{dummy_key}'")
    assert res is not None
    assert res[0] == "OpenAI API Key"


def test_secret_detection_hardcoded_assignment():
    res = check_line_for_secret('api_key = "secret_value_9876543210"')  # secret-check:ignore
    assert res is not None
    assert res[0] == "Hardcoded Secret / API Key Assignment"


def test_secret_detection_ignore_pragma():
    line = 'api_key = "secret_value_9876543210"  # secret-check:ignore'
    res = check_line_for_secret(line)
    assert res is None


def test_secret_detection_placeholders():
    assert check_line_for_secret('api_key = "your_key_here"') is None
    assert check_line_for_secret('TEMPMAIL_API_KEY = "example_key"') is None
    assert check_line_for_secret("API_KEY = '<token>'") is None
