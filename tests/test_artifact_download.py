from __future__ import annotations

import stat
import zipfile
from http.client import HTTPMessage
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from scripts.artifact_download import (
    download_artifact_archive,
    safe_extract_zip,
    validate_signed_archive_url,
)

TOKEN = "repo-token"  # noqa: S105
ALLOWED_URL = "https://productionresultssa.blob.core.windows.net/actions-results/archive.zip"


class Response(BytesIO):
    def __init__(self, body: bytes = b"archive") -> None:
        super().__init__(body)
        self.status = 200

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class FakeOpener:
    def __init__(self, *, status: int = 302, location: str | None = ALLOWED_URL) -> None:
        self.status = status
        self.location = location
        self.requests: list[Request] = []

    def __call__(self, request: Request, *, timeout: int) -> Response:
        del timeout
        self.requests.append(request)
        if len(self.requests) == 1:
            headers = HTTPMessage()
            if self.location is not None:
                headers.add_header("Location", self.location)
            raise HTTPError(request.full_url, self.status, "redirect", headers, None)
        return Response(b"zip-bytes")


def header(request: Request, name: str) -> str | None:
    return request.get_header(name)


def make_zip(entries: dict[str, bytes], modes: dict[str, int] | None = None) -> BytesIO:
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        for name, content in entries.items():
            info = zipfile.ZipInfo(name)
            if modes and name in modes:
                info.external_attr = modes[name] << 16
            zip_file.writestr(info, content)
    archive.seek(0)
    return archive


def test_valid_redirect_authorizes_only_initial_github_api_request() -> None:
    opener = FakeOpener()
    result = download_artifact_archive(
        archive_url="https://api.github.com/repos/o/r/actions/artifacts/1/zip",
        token=TOKEN,
        opener=opener,
    )

    assert result.content == b"zip-bytes"
    assert len(opener.requests) == 2
    assert opener.requests[0].full_url == "https://api.github.com/repos/o/r/actions/artifacts/1/zip"
    assert header(opener.requests[0], "Authorization") == "Bearer repo-token"
    assert header(opener.requests[0], "Accept") == "application/vnd.github+json"
    assert opener.requests[1].full_url == ALLOWED_URL
    assert header(opener.requests[1], "Authorization") is None
    assert header(opener.requests[1], "GH_TOKEN") is None
    assert header(opener.requests[1], "Cookie") is None


@pytest.mark.parametrize(
    "location",
    [
        "http://productionresultssa.blob.core.windows.net/archive.zip",
        "https://user:pass@productionresultssa.blob.core.windows.net/archive.zip",
        "https:///archive.zip",
        "not a url",
        "https://objects.example/archive.zip",
    ],
)
def test_malformed_or_unsafe_redirect_urls_fail_safely(location: str) -> None:
    with pytest.raises(ValueError):
        validate_signed_archive_url(location)
    with pytest.raises(ValueError):
        download_artifact_archive(
            archive_url="https://api.github.com/repos/o/r/actions/artifacts/1/zip",
            token=TOKEN,
            opener=FakeOpener(location=location),
        )


def test_missing_location_header_fails_safely() -> None:
    with pytest.raises(ValueError, match="Location"):
        download_artifact_archive(
            archive_url="https://api.github.com/repos/o/r/actions/artifacts/1/zip",
            token=TOKEN,
            opener=FakeOpener(location=None),
        )


def test_unexpected_redirect_status_fails_safely() -> None:
    with pytest.raises(ValueError, match="unexpected artifact archive redirect status"):
        download_artifact_archive(
            archive_url="https://api.github.com/repos/o/r/actions/artifacts/1/zip",
            token=TOKEN,
            opener=FakeOpener(status=401),
        )


def test_unexpected_non_redirect_response_fails_safely() -> None:
    requests: list[Request] = []

    def opener(request: Request, *, timeout: int) -> Response:
        del timeout
        requests.append(request)
        return Response()

    with pytest.raises(ValueError, match="unexpected non-redirect"):
        download_artifact_archive(
            archive_url="https://api.github.com/repos/o/r/actions/artifacts/1/zip",
            token=TOKEN,
            opener=opener,
        )
    assert len(requests) == 1


def test_safe_extract_zip_writes_valid_archive(tmp_path: Path) -> None:
    archive = make_zip({"items.jsonl": b"{}\n", "nested/keywords.jsonl": b"[]\n"})
    safe_extract_zip(archive, tmp_path / "data-result")
    assert (tmp_path / "data-result" / "items.jsonl").read_bytes() == b"{}\n"
    assert (tmp_path / "data-result" / "nested" / "keywords.jsonl").read_bytes() == b"[]\n"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "data/.git/config"])
def test_safe_extract_zip_rejects_unsafe_paths(tmp_path: Path, name: str) -> None:
    archive = make_zip({name: b"bad"})
    with pytest.raises(ValueError):
        safe_extract_zip(archive, tmp_path / "data-result")


def test_safe_extract_zip_rejects_symlinks(tmp_path: Path) -> None:
    archive = make_zip({"link": b"target"}, modes={"link": stat.S_IFLNK | 0o777})
    with pytest.raises(ValueError):
        safe_extract_zip(archive, tmp_path / "data-result")
