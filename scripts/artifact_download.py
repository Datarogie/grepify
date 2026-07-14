"""Credential-safe GitHub Actions artifact archive downloader helpers.

The production workflow keeps this logic inline in the `data-update` job so the
contents-write job does not need to check out and execute mutable repository
code. These helpers mirror that inline implementation and give the redirect and
ZIP extraction credential boundary deterministic unit coverage.
"""

from __future__ import annotations

import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request

REDIRECT_STATUSES = {302, 303, 307, 308}
ALLOWED_ARCHIVE_HOST_SUFFIXES = (
    ".blob.core.windows.net",
    ".actions.githubusercontent.com",
)
ALLOWED_ARCHIVE_HOSTS = {
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
    "productionresultssa.blob.core.windows.net",
}


class UrlOpener(Protocol):
    def __call__(self, request: Request, *, timeout: int): ...


@dataclass(frozen=True)
class DownloadResult:
    api_request: Request
    archive_request: Request
    content: bytes


def validate_signed_archive_url(location: str | None) -> str:
    if not location:
        raise ValueError("artifact archive redirect did not include a Location header")
    parsed = urlparse(location)
    if parsed.scheme != "https":
        raise ValueError("artifact archive redirect must use https")
    if parsed.username or parsed.password:
        raise ValueError("artifact archive redirect must not include userinfo")
    if not parsed.hostname or not parsed.netloc:
        raise ValueError("artifact archive redirect has malformed authority")
    host = parsed.hostname.lower().rstrip(".")
    if host not in ALLOWED_ARCHIVE_HOSTS and not host.endswith(ALLOWED_ARCHIVE_HOST_SUFFIXES):
        raise ValueError("artifact archive redirect host is not in the allowed GitHub storage set")
    return location


def download_artifact_archive(
    *,
    archive_url: str,
    token: str,
    opener: UrlOpener,
    api_timeout: int = 30,
    archive_timeout: int = 60,
) -> DownloadResult:
    api_request = Request(  # noqa: S310
        archive_url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        response = opener(api_request, timeout=api_timeout)
    except HTTPError as exc:
        if exc.code not in REDIRECT_STATUSES:
            raise ValueError(f"unexpected artifact archive redirect status: {exc.code}") from exc
        signed_url = validate_signed_archive_url(exc.headers.get("Location"))
    else:
        status = getattr(response, "status", "unknown")
        raise ValueError(f"unexpected non-redirect artifact archive response: {status}")

    archive_request = Request(signed_url)  # noqa: S310
    with opener(archive_request, timeout=archive_timeout) as archive_response:
        content = archive_response.read()
    return DownloadResult(api_request=api_request, archive_request=archive_request, content=content)


def _safe_member_path(destination: Path, member_name: str) -> Path:
    member = Path(member_name)
    if member.is_absolute() or ".." in member.parts:
        raise ValueError(f"unsafe artifact path: {member_name}")
    if any(part == ".git" for part in member.parts):
        raise ValueError(f"artifact must not contain .git metadata: {member_name}")
    target = (destination / member).resolve()
    destination_resolved = destination.resolve()
    if target != destination_resolved and destination_resolved not in target.parents:
        raise ValueError(f"artifact path escapes destination: {member_name}")
    return target


def safe_extract_zip(archive: str | Path | BinaryIO, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zip_file:
        members = zip_file.infolist()
        for info in members:
            mode = info.external_attr >> 16
            if (
                stat.S_ISLNK(mode)
                or stat.S_ISCHR(mode)
                or stat.S_ISBLK(mode)
                or stat.S_ISFIFO(mode)
            ):
                raise ValueError(f"artifact member has unsafe file type: {info.filename}")
            _safe_member_path(destination, info.filename)
        for info in members:
            target = _safe_member_path(destination, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zip_file.open(info) as src, target.open("wb") as dst:
                dst.write(src.read())
