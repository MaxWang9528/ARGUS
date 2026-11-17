"""Load ``KEY=value`` lines from a ``.env`` file into the environment.

Variables already set in the environment win, so ``export X=...`` still overrides the
file. Blank lines and ``#`` comments are skipped; an optional ``export`` prefix and
matching quotes around the value are removed. Values are never logged.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        key, value = m.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()  # trailing comment on an unquoted value
        out[key] = value
    return out


def load(path: str | Path = ".env") -> list[str]:
    """Set the file's variables that aren't already set (empty values are skipped).
    Returns the names that were set."""
    p = Path(path)
    if not p.is_file():
        return []
    added = []
    for key, value in parse(p.read_text(encoding="utf-8")).items():
        if value and not os.environ.get(key):
            os.environ[key] = value
            added.append(key)
    return added
