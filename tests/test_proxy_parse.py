# -*- coding: utf-8 -*-
from __future__ import annotations

from xconsole_client.proxy_pool import parse_proxy_line


def test_bare_url() -> None:
    ent = parse_proxy_line("http://1.2.3.4:8080")
    assert ent is not None
    assert "1.2.3.4" in ent.url


def test_region_tag() -> None:
    ent = parse_proxy_line("us http://1.2.3.4:8080")
    assert ent is not None
    assert ent.region == "us"


def test_comment_and_blank() -> None:
    assert parse_proxy_line("# comment") is None
    assert parse_proxy_line("   ") is None
