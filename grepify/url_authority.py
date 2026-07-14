"""URL authority formatting helpers shared by non-network URL policies."""

from __future__ import annotations


def format_url_authority(*, scheme: str, host: str, port: int | None) -> str:
    """Return a URL authority with IPv6 brackets and default ports normalized away."""
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    authority_host = f"[{host}]" if ":" in host else host
    return authority_host if port is None or default_port else f"{authority_host}:{port}"
