"""GRP-52/53: YouTube transcript fetching, storage, and excerpting.

Transcripts are a **best-effort, absence-tolerant** enrichment (PRD §13):
YouTube metadata ingestion (GRP-12) works with or without them, so a missing or
unfetchable transcript is never an error - it just leaves ``transcript_ref``
null (F-ING-03). This module owns three things:

- :class:`TranscriptClient` - the injectable seam (like
  :class:`~grepify.ingest.http.Transport`) so tests drive storage with a canned
  client and no network; the real youtube-transcript-api client
  (:class:`YouTubeTranscriptApiClient`) imports its library lazily (optional
  ``transcripts`` extra).
- :class:`TranscriptStore` - fetch (once), cap, gzip-compress, and store a
  transcript under ``data/transcripts/`` (GRP-52), returning a portable
  ``transcript_ref``; and read one back for excerpting.
- :func:`excerpt_transcript` - the first <=1500 chars with a smart cut
  (GRP-53, F-EXT-01), for the extraction prompt.

Storage layout, compression, caps (GRP-52)
------------------------------------------
A transcript is stored at ``data/transcripts/<video_id>.txt.gz`` and the
``transcript_ref`` recorded on the item is that path **relative to the data
root** (posix), so it is portable across machines and stable in the ``data``
branch diff. Text is truncated to ``transcript_max_chars`` (PRD §7 limits,
default 60000) before compression so a pathological multi-hour auto-transcript
cannot bloat the repo (PRD §5 guardrails, GRP-63). gzip is written with a fixed
mtime so the same transcript compresses to the same bytes (no churn).

Idempotency + absence (F-ING-07 / F-ING-03)
-------------------------------------------
:meth:`TranscriptStore.ensure` is idempotent: if the blob already exists it
returns the ref **without calling the client**, so a transcript already stored
is never re-fetched. If the client yields nothing (no captions, disabled, or
blocked from CI IPs - PRD §13), or raises, ``ensure`` returns ``None`` and no
file is written; the item stores ``transcript_ref=null``. A video that has no
transcript is therefore re-checked on each run (a bounded best-effort cost, not
a correctness bug) - there is no §6 "checked, absent" marker and adding one is a
schema decision out of this issue's scope.

Failure modes
-------------
:meth:`TranscriptStore.ensure` and :meth:`TranscriptStore.read` never raise for
a missing/unfetchable/corrupt transcript - all degrade to ``None`` (best-effort,
PRD §13). The only exceptions that escape are programming faults (e.g. an
unwritable data dir surfacing ``OSError`` on write), which are systemic, not
per-source. :func:`excerpt_transcript` is pure and raises nothing.
"""

from __future__ import annotations

import gzip
import importlib
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from grepify.paths import DataLayout

_SAFE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_GZIP_MTIME = 0  # fixed so identical text -> identical bytes (no data-branch churn)
DEFAULT_EXCERPT_CHARS = 1500  # F-EXT-01: transcript excerpt <= 1500 chars for youtube


class TranscriptClient(Protocol):
    """What :class:`TranscriptStore` needs to fetch a transcript - the seam.

    Returns the transcript as one plain-text string, or ``None`` when the video
    has none (disabled / no captions). Implementations should map their
    library's "no transcript" exceptions to ``None``; the store additionally
    treats *any* raised exception as absence (best-effort, PRD §13), so a client
    that leaks a network error still degrades to a null ``transcript_ref``.
    """

    def fetch(self, video_id: str, *, languages: Sequence[str]) -> str | None: ...


class TranscriptStore:
    """Fetch-once, capped, gzip-compressed transcript storage (see module docstring)."""

    def __init__(
        self,
        layout: DataLayout,
        client: TranscriptClient,
        *,
        max_chars: int,
        languages: Sequence[str],
    ) -> None:
        self._layout = layout
        self._client = client
        self._max_chars = max_chars
        self._languages = list(languages)

    def ensure(self, video_id: str) -> str | None:
        """Return the ``transcript_ref`` for ``video_id`` (fetching + storing it
        on first sight), or ``None`` if the video has no usable transcript.

        Idempotent: an already-stored transcript returns its ref without a
        client call (F-ING-07). See the module docstring for the absence rule.
        """
        if not _SAFE_VIDEO_ID.match(video_id):
            return None  # not a real youtube id -> nothing to fetch (defensive)

        blob = self._blob_path(video_id)
        ref = self._ref(blob)
        if blob.exists():
            return ref

        text = self._fetch(video_id)
        if not text:
            return None

        capped = text[: self._max_chars]
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(gzip.compress(capped.encode("utf-8"), mtime=_GZIP_MTIME))
        return ref

    def read(self, transcript_ref: str) -> str | None:
        """Return the stored transcript text for ``transcript_ref``, or ``None``
        if the blob is missing/unreadable (best-effort - a consumer that wants an
        excerpt just gets none, GRP-53).

        Refs are internally generated relative posix paths, but this reads from
        truth (which can travel on the shared ``data`` branch), so a tampered ref
        is defended against: a ref that escapes the data root (absolute path,
        ``..`` traversal) yields ``None`` rather than reading an arbitrary file.
        """
        root = self._layout.root.resolve()
        path = (root / transcript_ref).resolve()
        if root not in path.parents:
            return None
        try:
            return gzip.decompress(path.read_bytes()).decode("utf-8")
        except (OSError, EOFError, gzip.BadGzipFile, UnicodeDecodeError):
            return None

    def _fetch(self, video_id: str) -> str | None:
        try:
            return self._client.fetch(video_id, languages=self._languages)
        except Exception:  # best-effort: any client fault means "no transcript"
            return None

    def _blob_path(self, video_id: str) -> Path:
        return self._layout.transcripts_dir / f"{video_id}.txt.gz"

    def _ref(self, blob: Path) -> str:
        return blob.relative_to(self._layout.root).as_posix()


class YouTubeTranscriptApiClient:
    """Live :class:`TranscriptClient` backed by youtube-transcript-api (optional
    ``transcripts`` extra), imported lazily. A thin adapter: fetch the transcript
    for the preferred languages and join the snippets into one string. Every
    "no transcript" outcome (disabled, none found, unavailable, or blocked from
    CI IPs) returns ``None`` - absence is not an error (F-ING-03, PRD §13).

    Not unit-tested (no network); storage/excerpting logic is tested through
    :class:`TranscriptStore` with a fake client.
    """

    def fetch(self, video_id: str, *, languages: Sequence[str]) -> str | None:
        try:
            api_mod = importlib.import_module("youtube_transcript_api")
        except ImportError:  # pragma: no cover - only without the extra installed
            return None
        try:
            snippets = _fetch_snippets(api_mod, video_id, list(languages))
        except Exception:  # all outcomes -> absence (None); see class docstring
            return None
        text = " ".join(part.strip() for part in snippets if part.strip())
        return text or None


def _fetch_snippets(api_mod: Any, video_id: str, languages: list[str]) -> list[str]:
    """Extract snippet texts across youtube-transcript-api versions.

    v1.x exposes an instance ``.fetch(...)`` returning objects with ``.text``;
    older releases expose a classmethod ``get_transcript(...)`` returning dicts.
    Both are handled so a version bump doesn't silently break ingestion.
    """
    api_cls = api_mod.YouTubeTranscriptApi
    if hasattr(api_cls, "get_transcript"):  # <1.0 classmethod, list[dict]
        rows = api_cls.get_transcript(video_id, languages=languages)
        return [str(row.get("text", "")) for row in rows]
    fetched = api_cls().fetch(video_id, languages=languages)  # >=1.0 instance API
    return [str(getattr(snippet, "text", "")) for snippet in fetched]


def excerpt_transcript(text: str, *, max_chars: int = DEFAULT_EXCERPT_CHARS) -> str:
    """First ``max_chars`` of ``text`` with a smart cut (GRP-53, F-EXT-01).

    Prefers to end on a sentence boundary within the window; failing that, on a
    word boundary; failing that (one giant token), a hard cut at ``max_chars``.
    Leading/trailing whitespace is stripped. A transcript already within the cap
    is returned whole (whitespace-collapsed). Pure - never raises.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_chars:
        return collapsed

    window = collapsed[:max_chars]
    sentence_end = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if sentence_end >= max_chars // 2:
        # +1 keeps the terminal punctuation, dropping the trailing space.
        return window[: sentence_end + 1].rstrip()

    word_end = window.rfind(" ")
    if word_end >= max_chars // 2:
        return window[:word_end].rstrip()

    return window.rstrip()
