"""Reusable filesystem path containment checks.

Failure modes
-------------
Invalid roots or generated paths raise :class:`ValueError` with the path that
failed validation. Filesystem permission errors from callers still propagate as
``OSError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ContainedPath:
    """A root that only yields resolved paths contained by that root."""

    root: Path

    @classmethod
    def create(cls, root: Path) -> ContainedPath:
        resolved = root.resolve(strict=False)
        return cls(resolved)

    def resolve(self, path: Path) -> Path:
        if path.is_absolute():
            raise ValueError(f"generated path must be relative: {path}")
        candidate = (self.root / path).resolve(strict=False)
        if not is_relative_to(candidate, self.root):
            raise ValueError(f"generated path escapes output root: {path}")
        return candidate

    def join(self, *parts: str) -> Path:
        return self.resolve(Path(*parts))


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def contains_or_equals(path: Path, child: Path) -> bool:
    return is_relative_to(child, path)


def ensure_safe_output_dir(
    output_dir: Path, *, cwd: Path, protected_roots: tuple[Path, ...]
) -> Path:
    resolved = output_dir.resolve(strict=False)
    cwd_root = cwd.resolve(strict=False)
    protected = tuple(p.resolve(strict=False) for p in protected_roots)
    if ".." in output_dir.parts or output_dir.is_symlink() or resolved.parent == resolved:
        raise ValueError(f"unsafe output directory: {output_dir}")
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError(f"unsafe output directory: {output_dir}")
    if resolved == cwd_root or contains_or_equals(resolved, cwd_root):
        raise ValueError(f"unsafe output directory: {output_dir}")
    for root in protected:
        if (
            resolved == root
            or contains_or_equals(resolved, root)
            or contains_or_equals(root, resolved)
        ):
            raise ValueError(f"unsafe output directory: {output_dir}")
    return resolved
