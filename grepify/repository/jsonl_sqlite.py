"""v1 storage impl: append-only JSONL truth + rebuilt SQLite cache (PRD §5).

Truth lives in date-partitioned JSONL under the data root (see
:mod:`grepify.paths`). The SQLite file is a derived query cache, dropped and
rebuilt from truth on every :meth:`rebuild_cache` call - so a rebuild is both
deterministic (same truth → same cache) and idempotent. Writes are idempotent by
primary key: re-adding the same record appends nothing.

Failure modes
-------------
- Unreadable / malformed truth JSONL → :class:`~grepify.errors.RepositoryError`
  (wrapping the underlying ``json``/``pydantic`` error), naming the file.
- Cache queried before it is built → :class:`~grepify.errors.RepositoryError`.
- SQLite errors during rebuild → wrapped in :class:`~grepify.errors.RepositoryError`.

Concurrency: the cache is single-writer and rebuilt per run; concurrent CI runs
are prevented by the Actions concurrency group (GRP-06) and data commits are
serialized by :func:`grepify.repository.commit.commit_data`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, TypeVar, cast

from pydantic import ValidationError

from grepify.errors import RepositoryError
from grepify.models import (
    Digest,
    FetchLogEntry,
    Item,
    ItemKeyword,
    LlmLogEntry,
    Source,
    SourceGroup,
)
from grepify.paths import DataLayout
from grepify.repository.base import Repository
from grepify.repository.schema import SCHEMA_DDL

_R = TypeVar("_R", bound="Item | ItemKeyword | Digest | FetchLogEntry | LlmLogEntry")


class JsonlSqliteRepository(Repository):
    """JSONL-truth + SQLite-cache repository."""

    def __init__(self, data_root: Path) -> None:
        self._layout = DataLayout(Path(data_root))
        self._conn: sqlite3.Connection | None = None
        self._groups: list[SourceGroup] = []
        self._sources: list[Source] = []

    # --- truth writes --------------------------------------------------------

    def add_items(self, items: Sequence[Item]) -> int:
        existing = self.existing_item_ids()
        return self._append_deduped(
            records=items,
            base_dir=self._layout.items_dir,
            date_of=lambda i: i.published_at,
            key_of=lambda i: i.item_id,
            existing_keys=existing,
        )

    def add_item_keywords(self, keywords: Sequence[ItemKeyword]) -> int:
        existing = self._existing_keys(
            self._layout.keywords_dir,
            lambda d: f"{d['item_id']}\x00{d['keyword']}\x00{d['method']}",
        )
        return self._append_deduped(
            records=keywords,
            base_dir=self._layout.keywords_dir,
            date_of=lambda k: k.extracted_at,
            key_of=lambda k: f"{k.item_id}\x00{k.keyword}\x00{k.method}",
            existing_keys=existing,
        )

    def add_digest(self, digest: Digest) -> None:
        path = self._layout.digest_file(digest.digest_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(digest.model_dump_json() + "\n", encoding="utf-8")

    def log_fetch(self, entry: FetchLogEntry) -> None:
        path = self._layout.dated_file(self._layout.fetch_log_dir, entry.started_at)
        self._append_line(path, entry.model_dump_json())

    def log_llm(self, entry: LlmLogEntry) -> None:
        path = self._layout.dated_file(self._layout.llm_log_dir, entry.created_at)
        self._append_line(path, entry.model_dump_json())

    # --- truth maintenance (rare, deliberate in-place rewrites) --------------

    def rewrite_items(self, items: Sequence[Item]) -> int:
        replacements = {item.item_id: item for item in items}
        if not replacements:
            return 0
        rewritten = 0
        for path in self._truth_files(self._layout.items_dir):
            out: list[str] = []
            changed = False
            for line in self._read_lines(path):
                replacement = replacements.get(self._line_item_id(path, line))
                if replacement is None:
                    out.append(line)
                else:
                    out.append(replacement.model_dump_json())
                    rewritten += 1
                    changed = True
            if changed:
                self._write_lines(path, out)
        return rewritten

    def delete_item_keywords(self, item_ids: Iterable[str]) -> int:
        targets = set(item_ids)
        if not targets:
            return 0
        deleted = 0
        for path in self._truth_files(self._layout.keywords_dir):
            out: list[str] = []
            changed = False
            for line in self._read_lines(path):
                if self._line_item_id(path, line) in targets:
                    deleted += 1
                    changed = True
                else:
                    out.append(line)
            if changed:
                self._write_lines(path, out)
        return deleted

    # --- truth reads ---------------------------------------------------------

    def iter_items(self) -> Iterator[Item]:
        items = self._read_all(self._layout.items_dir, Item)
        items.sort(key=lambda i: (i.published_at, i.item_id))
        return iter(items)

    def iter_item_keywords(self) -> Iterator[ItemKeyword]:
        rows = self._read_all(self._layout.keywords_dir, ItemKeyword)
        # `method` is part of the primary key (an item can carry both an `llm`
        # and a `fallback` row for the same keyword text), so it must be in the
        # sort key too for a fully deterministic order.
        rows.sort(key=lambda k: (k.item_id, k.rank, k.keyword, str(k.method)))
        return iter(rows)

    def iter_digests(self) -> Iterator[Digest]:
        digests: list[Digest] = []
        directory = self._layout.digests_dir
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                digests.append(self._parse_one(path, path.read_text(encoding="utf-8"), Digest))
        digests.sort(key=lambda d: d.digest_id)
        return iter(digests)

    def existing_item_ids(self) -> set[str]:
        return self._existing_keys(self._layout.items_dir, lambda d: str(d["item_id"]))

    def iter_fetch_log(self) -> Iterator[FetchLogEntry]:
        # Sort key is `started_at` alone, relying on Python's stable sort to
        # preserve `_read_all`'s file order (day-partitioned ascending, then
        # true append order within a day) as the tie-break. `started_at` is
        # second-precision text and `run_id` ends in random entropy, so
        # sorting by `(started_at, run_id, ...)` - as the other iter_* methods
        # do with *stable* identifiers like item_id - would let same-second
        # entries land in an arbitrary, non-chronological order instead of
        # the real attempt order health-snapshot consumers rely on (GRP-16).
        rows = self._read_all(self._layout.fetch_log_dir, FetchLogEntry)
        rows.sort(key=lambda e: e.started_at)
        return iter(rows)

    # --- config projection ---------------------------------------------------

    def load_config(self, groups: Iterable[SourceGroup], sources: Iterable[Source]) -> None:
        self._groups = list(groups)
        self._sources = list(sources)

    # --- cache lifecycle & queries -------------------------------------------

    def rebuild_cache(self) -> None:
        self._close_conn()
        db_path = self._layout.cache_db
        db_path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db_path) + suffix)
            candidate.unlink(missing_ok=True)

        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(SCHEMA_DDL)
            self._insert_groups(conn)
            self._insert_sources(conn)
            self._insert_items(conn)
            self._insert_item_keywords(conn)
            self._insert_digests(conn)
            self._insert_fetch_log(conn)
            self._insert_llm_log(conn)
            conn.commit()
        except sqlite3.Error as exc:
            conn.close()
            raise RepositoryError(f"cache rebuild failed: {exc}") from exc
        self._conn = conn

    def count_items(self) -> int:
        return self._scalar_count("items")

    def count_item_keywords(self) -> int:
        return self._scalar_count("item_keywords")

    def close(self) -> None:
        self._close_conn()

    # --- cache insert helpers (explicit columns, lowercase sql) --------------

    def _insert_groups(self, conn: sqlite3.Connection) -> None:
        conn.executemany(
            "insert into source_groups (group_id, name, category, enabled, builtin) "
            "values (?, ?, ?, ?, ?)",
            [
                (g.group_id, g.name, g.category, int(g.enabled), int(g.builtin))
                for g in sorted(self._groups, key=lambda g: g.group_id)
            ],
        )

    def _insert_sources(self, conn: sqlite3.Connection) -> None:
        conn.executemany(
            "insert into sources "
            "(source_id, name, kind, url, url_hash, group_id, enabled, added_at, config_json) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    s.source_id,
                    s.name,
                    str(s.kind),
                    s.url,
                    s.url_hash,
                    s.group_id,
                    int(s.enabled),
                    s.added_at,
                    s.config_json,
                )
                for s in sorted(self._sources, key=lambda s: s.source_id)
            ],
        )

    def _insert_items(self, conn: sqlite3.Connection) -> None:
        conn.executemany(
            "insert into items "
            "(item_id, source_id, kind, external_id, canonical_url, title, summary, "
            "author, published_at, fetched_at, content_hash, transcript_ref, lang) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    i.item_id,
                    i.source_id,
                    str(i.kind),
                    i.external_id,
                    i.canonical_url,
                    i.title,
                    i.summary,
                    i.author,
                    i.published_at,
                    i.fetched_at,
                    i.content_hash,
                    i.transcript_ref,
                    i.lang,
                )
                for i in self.iter_items()
            ],
        )

    def _insert_item_keywords(self, conn: sqlite3.Connection) -> None:
        conn.executemany(
            "insert into item_keywords "
            "(item_id, keyword, rank, method, model, extracted_at) "
            "values (?, ?, ?, ?, ?, ?)",
            [
                (k.item_id, k.keyword, k.rank, str(k.method), k.model, k.extracted_at)
                for k in self.iter_item_keywords()
            ],
        )

    def _insert_digests(self, conn: sqlite3.Connection) -> None:
        conn.executemany(
            "insert into digests "
            "(digest_id, kind, category, period_start, period_end, title, body_md, "
            "top_keywords, model, prompt_version, created_at) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    d.digest_id,
                    str(d.kind),
                    d.category,
                    d.period_start,
                    d.period_end,
                    d.title,
                    d.body_md,
                    d.top_keywords,
                    d.model,
                    d.prompt_version,
                    d.created_at,
                )
                for d in self.iter_digests()
            ],
        )

    def _insert_fetch_log(self, conn: sqlite3.Connection) -> None:
        conn.executemany(
            "insert into fetch_log "
            "(source_id, run_id, started_at, status, items_new, error, duration_ms) "
            "values (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    e.source_id,
                    e.run_id,
                    e.started_at,
                    str(e.status),
                    e.items_new,
                    e.error,
                    e.duration_ms,
                )
                for e in self.iter_fetch_log()
            ],
        )

    def _insert_llm_log(self, conn: sqlite3.Connection) -> None:
        rows = self._read_all(self._layout.llm_log_dir, LlmLogEntry)
        rows.sort(key=lambda e: (e.created_at, e.run_id, e.purpose))
        conn.executemany(
            "insert into llm_log "
            "(run_id, purpose, model, input_items, tokens_in, tokens_out, status, created_at) "
            "values (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    e.run_id,
                    e.purpose,
                    e.model,
                    e.input_items,
                    e.tokens_in,
                    e.tokens_out,
                    e.status,
                    e.created_at,
                )
                for e in rows
            ],
        )

    # --- low-level helpers ---------------------------------------------------

    def _scalar_count(self, table: str) -> int:
        conn = self._require_conn()
        # table is a fixed internal literal, never user input.
        cursor = conn.execute(f"select count(*) from {table}")
        (count,) = cursor.fetchone()
        return int(count)

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        db_path = self._layout.cache_db
        if not db_path.exists():
            raise RepositoryError("cache not built; call rebuild_cache() first")
        self._conn = sqlite3.connect(db_path)
        return self._conn

    def _close_conn(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _append_line(self, path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _append_deduped(
        self,
        *,
        records: Sequence[_R],
        base_dir: Path,
        date_of: Callable[[_R], str],
        key_of: Callable[[_R], str],
        existing_keys: set[str],
    ) -> int:
        seen = set(existing_keys)
        written = 0
        for record in records:
            key = key_of(record)
            if key in seen:
                continue
            seen.add(key)
            path = self._layout.dated_file(base_dir, date_of(record))
            self._append_line(path, record.model_dump_json())
            written += 1
        return written

    def _existing_keys(self, directory: Path, key_of: Callable[[dict[str, Any]], str]) -> set[str]:
        keys: set[str] = set()
        if not directory.is_dir():
            return keys
        for path in sorted(directory.rglob("*.jsonl")):
            for line in self._read_lines(path):
                try:
                    keys.add(key_of(json.loads(line)))
                except (json.JSONDecodeError, KeyError) as exc:
                    raise RepositoryError(f"corrupt truth file {path}: {exc}") from exc
        return keys

    def _read_all(self, directory: Path, model: type[_R]) -> list[_R]:
        records: list[_R] = []
        if not directory.is_dir():
            return records
        for path in sorted(directory.rglob("*.jsonl")):
            for line in self._read_lines(path):
                records.append(self._parse_one(path, line, model))
        return records

    def _parse_one(self, path: Path, raw: str, model: type[_R]) -> _R:
        try:
            # model_validate_json is typed to the TypeVar's bound (a union); the
            # concrete `model` guarantees the narrower `_R`, so cast is safe here.
            return cast(_R, model.model_validate_json(raw))
        except ValidationError as exc:
            raise RepositoryError(f"invalid {model.__name__} in {path}: {exc}") from exc

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    @staticmethod
    def _truth_files(directory: Path) -> list[Path]:
        return sorted(directory.rglob("*.jsonl")) if directory.is_dir() else []

    def _line_item_id(self, path: Path, line: str) -> str:
        try:
            return str(json.loads(line)["item_id"])
        except (json.JSONDecodeError, KeyError) as exc:
            raise RepositoryError(f"corrupt truth file {path}: {exc}") from exc

    @staticmethod
    def _write_lines(path: Path, lines: Sequence[str]) -> None:
        # Crash-safe: write a sibling temp file then atomically replace, so a
        # truth partition is never left half-written by an interrupted rewrite.
        # An emptied partition (all rows deleted) is removed rather than left as
        # a 0-byte file, keeping truth tidy.
        if not lines:
            path.unlink(missing_ok=True)
            return
        tmp = path.parent / f"{path.name}.tmp"
        tmp.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        tmp.replace(path)
