"""Keyword normalization + alias/mute application (GRP-23).

Pure, deterministic transforms applied to already-extracted keyword strings
(GRP-21/22 already trimmed + sanity-checked them; this is the fuller
normalization the PRD reserves for downstream consumers, F-EXT-03/F-EXT-05).
Per PRD §6, alias/mute is "applied at trend-computation time, not extraction
time, so remaps are retroactive and non-destructive" - this module has no
Repository/ConfigProvider dependency and does not run inside the extract
batcher; it is the shared utility the pipeline wiring (GRP-25) and the trend
queries (E3 GRP-31) call when reading ``item_keywords`` rows back out.

Pipeline: :func:`normalize_keyword` (lowercase, trim, collapse whitespace,
strip trailing punctuation) → alias substitution → mute drop. Singularization
is mentioned in the PRD §6 schema comment but not in the F-EXT-03 requirement
list; omitted here (a stemmer/lemmatizer is non-trivial and false-positive
prone - flagged as a PRD-diff candidate rather than guessed at).

Failure modes
-------------
None - every function is a pure string/mapping transform over already-valid
input (``ItemKeyword.keyword`` strings, ``KeywordsConfig`` values); nothing
here raises or performs I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from grepify.config.schemas import KeywordsConfig
from grepify.models import ItemKeyword

_WHITESPACE = re.compile(r"\s+")
_TRAILING_PUNCT = re.compile(r"[.,;:!?'\"]+$")


def normalize_keyword(raw: str) -> str:
    """Lowercase, trim, collapse internal whitespace, strip trailing punctuation.

    F-EXT-03. ``"  Gen   AI!! "`` -> ``"gen ai"``.
    """
    collapsed = _WHITESPACE.sub(" ", raw.strip()).lower()
    return _TRAILING_PUNCT.sub("", collapsed)


@dataclass(frozen=True)
class KeywordRules:
    """Precomputed, normalized alias map + mute set for one config snapshot.

    Both sides of ``keywords.yml``'s ``aliases`` map and every ``mute`` entry
    are run through :func:`normalize_keyword` once at construction, so lookups
    against already-normalized keywords are exact-match and case/whitespace
    insensitive on the raw config text.
    """

    alias_map: dict[str, str]
    mute_set: frozenset[str]

    @classmethod
    def from_config(cls, config: KeywordsConfig) -> KeywordRules:
        alias_map = {
            normalize_keyword(alias): normalize_keyword(canonical)
            for alias, canonical in config.aliases.items()
        }
        mute_set = frozenset(normalize_keyword(m) for m in config.mute)
        return cls(alias_map=alias_map, mute_set=mute_set)

    def apply(self, raw: str) -> str | None:
        """Normalize -> alias -> mute. ``None`` if the result is muted (F-EXT-05).

        Mute is checked against the post-alias canonical form, so muting a
        canonical term also drops every keyword that aliases to it.
        """
        normalized = normalize_keyword(raw)
        canonical = self.alias_map.get(normalized, normalized)
        if canonical in self.mute_set:
            return None
        return canonical


def apply_to_keyword(row: ItemKeyword, rules: KeywordRules) -> ItemKeyword | None:
    """Apply :class:`KeywordRules` to one stored keyword row.

    Returns a copy with ``keyword`` replaced by its canonical form, or ``None``
    if the row is muted (the caller drops it - truth itself is never mutated).
    """
    canonical = rules.apply(row.keyword)
    if canonical is None:
        return None
    return row.model_copy(update={"keyword": canonical})
