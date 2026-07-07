"""Run identity + run-manifest I/O (PRD §8 F-OPS-04).

Every pipeline run gets a ``run_id`` and writes ``data/runs/<run_id>.json`` with
counts, durations, and (later) budget usage — the data behind the health page
and phone debugging. ``run_id`` is a sortable UTC timestamp plus a short entropy
suffix, so the lexically-largest manifest filename is the latest run.

Failure modes
-------------
- Reading a corrupt manifest → ``pydantic.ValidationError`` (surfaced to the
  operator by ``grepify health``); a missing runs dir is not an error
  (:func:`latest_manifest` returns ``None``).
- ``run_id`` uses ``secrets`` for entropy, kept out of the deterministic render
  path (PRD §5); tests inject a fixed entropy string for reproducibility.
"""

from __future__ import annotations

import secrets
from datetime import UTC

from grepify.clock import Clock
from grepify.models import RunManifest
from grepify.paths import DataLayout


def new_run_id(clock: Clock, *, entropy: str | None = None) -> str:
    """Return a sortable run id: ``YYYYMMDDTHHMMSSZ-<6hex>``."""
    stamp = clock.now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = entropy if entropy is not None else secrets.token_hex(3)
    return f"{stamp}-{suffix}"


def write_manifest(layout: DataLayout, manifest: RunManifest) -> None:
    """Persist a run manifest to ``data/runs/<run_id>.json``."""
    path = layout.run_manifest(manifest.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")


def latest_manifest(layout: DataLayout) -> RunManifest | None:
    """Return the most recent run manifest, or ``None`` if there are no runs."""
    runs_dir = layout.runs_dir
    if not runs_dir.is_dir():
        return None
    files = sorted(runs_dir.glob("*.json"))
    if not files:
        return None
    return RunManifest.model_validate_json(files[-1].read_text(encoding="utf-8"))
