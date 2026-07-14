"""Sanitized acquisition-trace rows shared by the ingest fetchers.

Rows are compact dicts (provider, method, redacted URL, coarse outcome)
serialized into ``FetchOutcome.acquisition_trace`` and persisted on
``fetch_log``. They never carry response bodies, upstream headers, or
unredacted URLs. Error classification sniffs the formatted ``FetchError``
text because the transport collapses structured causes into one exception
type; categories are coarse by design and shared across fetchers so
``fetch_log`` triage never needs per-provider vocabularies.

Failure modes
-------------
None. These helpers perform no I/O and never raise: classification falls
back to ``fetch_error`` / ``http_<status>``, and ``trace_json`` serializes
plain dicts of JSON-safe scalars built here.
"""

from __future__ import annotations

import json

from grepify.ingest.http import safe_url_for_log

_TEXT_CATEGORIES = (
    (("429",), "rate_limited"),
    (("403",), "runner_blocked_or_forbidden"),
    (("too large", "exceeded size limit"), "oversized"),
    (("timeout",), "timeout"),
    (("unsafe", "credential", "scheme"), "policy_blocked"),
    (("unparseable", "malformed"), "parse_error"),
)


def trace_row(
    provider: str, method: str, url: str, outcome: str, **fields: object
) -> dict[str, object]:
    row: dict[str, object] = {
        "provider": provider,
        "method": method,
        "url": safe_url_for_log(url),
        "outcome": outcome,
    }
    row.update(fields)
    return row


def trace_json(rows: list[dict[str, object]]) -> str:
    return json.dumps(rows, sort_keys=True, separators=(",", ":"))


def coarse_error(text: str) -> str:
    lowered = text.lower()
    for needles, category in _TEXT_CATEGORIES:
        if any(needle in lowered for needle in needles):
            return category
    return "fetch_error"


def status_reason(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status == 403:
        return "runner_blocked_or_forbidden"
    return f"http_{status}"
