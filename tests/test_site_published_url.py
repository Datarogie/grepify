from __future__ import annotations

import pytest

from grepify.site.published_url import safe_published_url


@pytest.mark.parametrize(
    "raw",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,<svg onload=alert(1)>",
        "file:///etc/passwd",
        "vbscript:msgbox(1)",
        "\x01javascript:alert(1)",
        "java\nscript:alert(1)",
        "%6aavascript:alert(1)",
        "http://example.com:bad/path",
        "http://[::1",
        "//cdn.example.com/x",
        "/relative",
        "",
        None,
    ],
)
def test_unsafe_or_unresolved_published_urls_are_absent(raw: str | None) -> None:
    assert safe_published_url(raw) is None


@pytest.mark.parametrize(
    ("raw", "base", "expected"),
    [
        ("HTTP://Example.COM:80/a?b=1#frag", None, "http://example.com/a?b=1#frag"),
        (" https://Example.COM:443/a ", None, "https://example.com/a"),
        ("//cdn.example.com/x", "https://feeds.example.org/rss.xml", "https://cdn.example.com/x"),
        ("../post?id=1", "https://example.com/feeds/main.xml", "https://example.com/post?id=1"),
        ("item", "https://example.com/feeds/main.xml", "https://example.com/feeds/item"),
        ("https://[2001:db8::1]/post", None, "https://[2001:db8::1]/post"),
        ("http://[2001:db8::1]:8080/post", None, "http://[2001:db8::1]:8080/post"),
        ("http://[2001:db8::1]:80/post", None, "http://[2001:db8::1]/post"),
        ("https://[2001:db8::1]:443/post?q=1#frag", None, "https://[2001:db8::1]/post?q=1#frag"),
    ],
)
def test_valid_published_urls_are_normalized_and_relative_urls_resolve(
    raw: str, base: str | None, expected: str
) -> None:
    safe = safe_published_url(raw, base_url=base)
    assert safe is not None
    assert safe.href == expected
