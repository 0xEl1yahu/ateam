"""Glob matching shared by records, contracts and the worker policy."""

from __future__ import annotations

import glob as _glob
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath


@lru_cache(maxsize=1024)
def _regex(pattern: str) -> re.Pattern[str]:
    return re.compile(_glob.translate(pattern, recursive=True, include_hidden=True))


def matches(pattern: str, path: str) -> bool:
    """True when `path` (posix, repo-relative) matches the glob `pattern`."""
    return _regex(pattern).match(path) is not None


def is_valid_pattern(pattern: str) -> bool:
    """Relative, no leading './', no trailing '/', no '..' segment, not empty."""
    if not pattern or pattern.startswith(("/", "./")) or pattern.endswith("/"):
        return False
    return ".." not in PurePosixPath(pattern).parts


def expand(patterns: list[str], root: Path) -> list[str]:
    """Existing files under `root` matching any pattern, as sorted relative posix paths."""
    found: set[str] = set()
    for pattern in patterns:
        for hit in root.glob(pattern):
            if hit.is_file():
                found.add(hit.relative_to(root).as_posix())
    return sorted(found)
