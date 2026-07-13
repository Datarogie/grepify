"""GRP-52/53: transcript store (fetch-once, cap, gzip, absence-ok) + excerpting.

Drives :class:`grepify.ingest.transcript.TranscriptStore` through a fake
:class:`~grepify.ingest.transcript.TranscriptClient` (no network) and checks the
compression/cap/idempotency/absence contract, plus the pure
:func:`~grepify.ingest.transcript.excerpt_transcript` smart cut.
"""

from __future__ import annotations

import gzip
from collections.abc import Sequence
from pathlib import Path

from grepify.ingest.transcript import TranscriptStore, excerpt_transcript
from grepify.paths import DataLayout


class FakeTranscriptClient:
    """Canned :class:`TranscriptClient`: per-video text, ``None`` for absence, or
    an exception to raise. Records ``.calls`` so idempotency can be asserted."""

    def __init__(
        self, results: dict[str, str | None | Exception], *, default: str | None = None
    ) -> None:
        self._results = results
        self._default = default
        self.calls: list[str] = []

    def fetch(self, video_id: str, *, languages: Sequence[str]) -> str | None:
        self.calls.append(video_id)
        outcome = self._results.get(video_id, self._default)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _store(
    tmp_path: Path, client: FakeTranscriptClient, *, max_chars: int = 60000
) -> TranscriptStore:
    return TranscriptStore(DataLayout(tmp_path), client, max_chars=max_chars, languages=["en"])


def test_ensure_stores_and_read_roundtrips(tmp_path: Path) -> None:
    client = FakeTranscriptClient({"vid0001AAA": "hello world transcript"})
    store = _store(tmp_path, client)

    ref = store.ensure("vid0001AAA")

    assert ref == "transcripts/vid0001AAA.txt.gz"
    assert (tmp_path / ref).exists()
    assert store.read(ref) == "hello world transcript"


def test_ensure_is_idempotent_no_refetch(tmp_path: Path) -> None:
    client = FakeTranscriptClient({"vid0001AAA": "text"})
    store = _store(tmp_path, client)

    first = store.ensure("vid0001AAA")
    second = store.ensure("vid0001AAA")

    assert first == second
    assert client.calls == ["vid0001AAA"]  # fetched once, not twice


def test_cap_truncates_before_storage(tmp_path: Path) -> None:
    client = FakeTranscriptClient({"vid0001AAA": "x" * 5000})
    store = _store(tmp_path, client, max_chars=1000)

    ref = store.ensure("vid0001AAA")

    assert ref is not None
    assert len(store.read(ref) or "") == 1000


def test_absence_returns_none_and_writes_nothing(tmp_path: Path) -> None:
    client = FakeTranscriptClient({"vid0001AAA": None})
    store = _store(tmp_path, client)

    assert store.ensure("vid0001AAA") is None
    assert not (tmp_path / "transcripts" / "vid0001AAA.txt.gz").exists()


def test_client_error_degrades_to_none(tmp_path: Path) -> None:
    client = FakeTranscriptClient({"vid0001AAA": RuntimeError("blocked from CI IP")})
    store = _store(tmp_path, client)
    # Best-effort (PRD §13): a transport failure is treated as absence, not raised.
    assert store.ensure("vid0001AAA") is None


def test_unsafe_video_id_is_skipped(tmp_path: Path) -> None:
    client = FakeTranscriptClient({}, default="x")
    store = _store(tmp_path, client)
    assert store.ensure("../etc/passwd") is None
    assert client.calls == []


def test_read_missing_ref_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path, FakeTranscriptClient({}))
    assert store.read("transcripts/nope.txt.gz") is None


def test_read_rejects_path_escape(tmp_path: Path) -> None:
    # Defensive: a tampered ref in truth that escapes the data root reads nothing.
    store = _store(tmp_path, FakeTranscriptClient({}))
    assert store.read("../../etc/passwd") is None
    assert store.read("/etc/passwd") is None


def test_stored_bytes_are_deterministic(tmp_path: Path) -> None:
    client = FakeTranscriptClient({"vid0001AAA": "same text"})
    ref = _store(tmp_path, client).ensure("vid0001AAA")
    assert ref is not None
    raw = (tmp_path / ref).read_bytes()
    # Recompressing the same capped text yields identical bytes (fixed mtime), so
    # the data branch sees no churn for an unchanged transcript.
    assert raw == gzip.compress(b"same text", mtime=0)


# --- excerpting ----------------------------------------------------------------


def test_excerpt_returns_short_text_whole_collapsed() -> None:
    assert excerpt_transcript("  a\n b   c ", max_chars=100) == "a b c"


def test_excerpt_cuts_on_sentence_boundary() -> None:
    text = "First sentence. Second sentence. " + "tail " * 100
    out = excerpt_transcript(text, max_chars=40)
    assert out.endswith(".")
    assert out == "First sentence. Second sentence."


def test_excerpt_cuts_on_word_boundary_when_no_sentence() -> None:
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    out = excerpt_transcript(text, max_chars=20)
    assert " " in out
    assert not out.endswith(" ")
    assert len(out) <= 20
    assert out == text[:20].rsplit(" ", 1)[0]


def test_excerpt_hard_cuts_a_giant_token() -> None:
    out = excerpt_transcript("x" * 5000, max_chars=1500)
    assert len(out) == 1500
