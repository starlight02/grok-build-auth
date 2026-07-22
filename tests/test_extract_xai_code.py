# -*- coding: utf-8 -*-
from __future__ import annotations

from xconsole_client.codes import extract_xai_code


def test_sentence_form() -> None:
    assert extract_xai_code("Your code is XAI0X1") == "XAI0X1"


def test_subject_body() -> None:
    assert extract_xai_code("Subject: xAI verification\n\nXAI0X1") == "XAI0X1"


def test_dashed_current_format() -> None:
    assert extract_xai_code("code: LSQ-OPU") == "LSQ-OPU"


def test_reject_pure_digits() -> None:
    assert extract_xai_code("123456") is None


def test_lowercase_normalized() -> None:
    assert extract_xai_code("your code is xai0x1") == "XAI0X1"


def test_long_run_fallback() -> None:
    assert extract_xai_code("AB12CD34EF") == "AB12CD34"


def test_empty() -> None:
    assert extract_xai_code("") is None
