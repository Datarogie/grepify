"""Storage layer: the ``Repository`` interface and its v1 JSONL+SQLite impl."""

from __future__ import annotations

from grepify.repository.base import Repository
from grepify.repository.jsonl_sqlite import JsonlSqliteRepository

__all__ = ["JsonlSqliteRepository", "Repository"]
