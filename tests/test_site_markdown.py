"""Minimal markdown renderer tests (GRP-43): the digest-body subset, escaped."""

from __future__ import annotations

from grepify.site.markdown import render_markdown


def test_paragraphs_split_on_blank_lines() -> None:
    assert render_markdown("one\n\ntwo") == "<p>one</p>\n<p>two</p>"


def test_bullet_list() -> None:
    assert render_markdown("- a\n- b") == "<ul><li>a</li><li>b</li></ul>"


def test_bold_inline() -> None:
    assert render_markdown("a **bold** word") == "<p>a <strong>bold</strong> word</p>"


def test_composed_tldr_then_narrative() -> None:
    body = "**TL;DR**\n\n- first\n- second\n\nA narrative paragraph."
    assert render_markdown(body) == (
        "<p><strong>TL;DR</strong></p>\n"
        "<ul><li>first</li><li>second</li></ul>\n"
        "<p>A narrative paragraph.</p>"
    )


def test_html_is_escaped_no_injection() -> None:
    out = render_markdown("<script>alert(1)</script> & <b>x</b>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out and "&amp;" in out


def test_wrapped_paragraph_lines_join() -> None:
    assert render_markdown("line one\nline two") == "<p>line one line two</p>"


def test_deterministic() -> None:
    body = "**TL;DR**\n\n- x\n\nbody"
    assert render_markdown(body) == render_markdown(body)


def test_empty_is_empty() -> None:
    assert render_markdown("") == ""
    assert render_markdown("   \n\n  ") == ""
