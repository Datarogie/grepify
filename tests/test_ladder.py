"""GRP-66: acquisition-ladder URL derivation + feed autodiscovery (ADR 0002 §1)."""

from __future__ import annotations

import pytest

from grepify.ingest.ladder import alt_endpoint_urls, discover_feed_url, site_root


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://example.com/feed/",
            [
                "https://example.com/feed",
                "https://example.com/feed/atom/",
                "https://example.com/?feed=rss2",
            ],
        ),
        (
            "https://example.com/feed",
            [
                "https://example.com/feed/",
                "https://example.com/feed/atom/",
                "https://example.com/?feed=rss2",
            ],
        ),
        (
            "https://blog.example.com/category/ai/feed/",
            [
                "https://blog.example.com/category/ai/feed",
                "https://blog.example.com/category/ai/feed/atom/",
                "https://blog.example.com/category/ai/?feed=rss2",
            ],
        ),
        ("https://example.com/rss.xml", ["https://example.com/rss.xml/"]),
    ],
)
def test_alt_endpoint_urls_are_deterministic_and_exclude_the_original(
    url: str, expected: list[str]
) -> None:
    result = alt_endpoint_urls(url)
    assert result == expected
    assert url not in result
    assert len(result) == len(set(result))  # deduped


def test_site_root_is_scheme_host_home() -> None:
    assert site_root("https://blog.example.com/category/ai/feed/") == "https://blog.example.com/"


def test_discover_feed_url_returns_first_same_host_feed_link() -> None:
    html = b"""
    <html><head>
      <link rel="alternate" type="application/rss+xml" href="/feed/atom.xml">
      <link rel="alternate" type="application/atom+xml" href="https://example.com/other.xml">
    </head></html>
    """
    assert (
        discover_feed_url(html, base_url="https://example.com/")
        == "https://example.com/feed/atom.xml"
    )


def test_discover_feed_url_ignores_off_host_links() -> None:
    html = b"""
    <html><head>
      <link rel="alternate" type="application/rss+xml" href="https://third-party.example/feed">
    </head></html>
    """
    assert discover_feed_url(html, base_url="https://example.com/") is None


def test_discover_feed_url_treats_www_as_same_host() -> None:
    html = b'<link rel="alternate" type="application/rss+xml" href="https://www.example.com/feed">'
    assert (
        discover_feed_url(html, base_url="https://example.com/") == "https://www.example.com/feed"
    )


def test_discover_feed_url_none_for_html_without_feed_link() -> None:
    assert (
        discover_feed_url(
            b"<html><head><title>no feed</title></head></html>", base_url="https://x/"
        )
        is None
    )


def test_discover_feed_url_tolerates_malformed_and_undecodable_markup() -> None:
    # Undecodable bytes are replaced, not raised; a broken tag is skipped.
    assert discover_feed_url(b"\xff\xfe<link rel=alternate", base_url="https://x/") is None
