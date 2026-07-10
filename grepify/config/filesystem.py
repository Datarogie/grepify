"""Filesystem-YAML ``ConfigProvider`` (v1) - reads ``sources/`` (PRD §7).

Layout::

    <config_root>/
      settings.yml         # SettingsConfig
      keywords.yml         # KeywordsConfig
      groups/*.yml         # one GroupFile each

Group files are read in sorted filename order so output is deterministic.

Failure modes
-------------
- Missing ``settings.yml`` / ``keywords.yml`` or a malformed YAML/schema →
  :class:`~grepify.errors.ConfigError` from the accessors (fail-fast).
- :meth:`validate` catches all of the above per-file and adds cross-file checks
  (duplicate ``source_id`` or ``url_hash`` across groups), returning a
  :class:`~grepify.config.provider.ValidationReport` without raising.

Note: v1 filesystem config does not track a per-source *added_at* timestamp (that
is a v2 DB concern); the projected ``Source.added_at`` uses the file's declared
value or an empty string. Live feed liveness pinging (PRD §7) is deferred until a
fetcher exists (GRP-10/11) and is not performed here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from grepify.config.provider import ConfigProvider, ValidationReport
from grepify.config.schemas import GroupFile, KeywordsConfig, SettingsConfig, SourceSpec
from grepify.errors import ConfigError
from grepify.models import Source, SourceGroup

_M = TypeVar("_M", bound=BaseModel)


class FilesystemConfigProvider(ConfigProvider):
    """Reads config from a directory of YAML files."""

    def __init__(self, config_root: Path) -> None:
        self._root = Path(config_root)

    # --- accessors (fail-fast) ----------------------------------------------

    def groups(self) -> list[SourceGroup]:
        return [
            SourceGroup(
                group_id=gf.group,
                name=gf.name,
                category=gf.category,
                enabled=gf.enabled,
                builtin=gf.builtin,
            )
            for _, gf in self._group_files()
        ]

    def sources(self) -> list[Source]:
        result: list[Source] = []
        for _, gf in self._group_files():
            for spec in gf.sources:
                result.append(self._to_source(gf, spec))
        return result

    def keywords(self) -> KeywordsConfig:
        return self._load_model(self._root / "keywords.yml", KeywordsConfig)

    def settings(self) -> SettingsConfig:
        return self._load_model(self._root / "settings.yml", SettingsConfig)

    # --- validation (lenient, aggregating) ----------------------------------

    def validate(self) -> ValidationReport:
        errors: list[str] = []
        warnings: list[str] = []

        for name in ("settings.yml", "keywords.yml"):
            path = self._root / name
            model = SettingsConfig if name == "settings.yml" else KeywordsConfig
            try:
                self._load_model(path, model)
            except ConfigError as exc:
                errors.append(str(exc))

        parsed_groups: list[GroupFile] = []
        group_ids: dict[str, str] = {}
        source_ids: dict[str, str] = {}
        url_hashes: dict[str, str] = {}
        source_count = 0

        for path in self._group_paths():
            try:
                gf = self._load_model(path, GroupFile)
            except ConfigError as exc:
                errors.append(str(exc))
                continue
            parsed_groups.append(gf)

            if gf.group in group_ids:
                errors.append(
                    f"{path.name}: duplicate group id {gf.group!r} (also in {group_ids[gf.group]})"
                )
            else:
                group_ids[gf.group] = path.name

            for spec in gf.sources:
                source_count += 1
                if spec.id in source_ids:
                    errors.append(
                        f"{path.name}: duplicate source id {spec.id!r} "
                        f"(also in {source_ids[spec.id]})"
                    )
                else:
                    source_ids[spec.id] = path.name

                uh = spec.url_hash
                if uh in url_hashes:
                    errors.append(
                        f"{path.name}: duplicate feed {spec.canonical_url!r} "
                        f"(source {spec.id!r} collides with {url_hashes[uh]})"
                    )
                else:
                    url_hashes[uh] = spec.id

        if not self._group_paths():
            warnings.append("no group files under groups/ - nothing to ingest yet")

        return ValidationReport(
            ok=not errors,
            errors=errors,
            warnings=warnings,
            group_count=len(parsed_groups),
            source_count=source_count,
        )

    # --- internals -----------------------------------------------------------

    def _to_source(self, gf: GroupFile, spec: SourceSpec) -> Source:
        return Source(
            source_id=spec.id,
            name=spec.display_name,
            kind=spec.kind,
            url=spec.canonical_url,
            url_hash=spec.url_hash,
            group_id=gf.group,
            enabled=spec.enabled,
            added_at=spec.added_at or "",
            config_json=spec.config_json,
        )

    def _group_paths(self) -> list[Path]:
        groups_dir = self._root / "groups"
        if not groups_dir.is_dir():
            return []
        return sorted(p for p in groups_dir.glob("*.yml"))

    def _group_files(self) -> list[tuple[Path, GroupFile]]:
        return [(path, self._load_model(path, GroupFile)) for path in self._group_paths()]

    def _load_model(self, path: Path, model: type[_M]) -> _M:
        raw = self._read_yaml(path)
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"{path.name}: {exc}") from exc

    def _read_yaml(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise ConfigError(f"missing config file: {path}")
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path.name}: invalid YAML: {exc}") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path.name}: expected a mapping at top level")
        return loaded
