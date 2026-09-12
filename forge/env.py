"""
.env loading, without a dependency.

Handles the subset of .env syntax people actually write: KEY=value, optional
`export` prefix, quoted values, inline comments outside quotes, blank lines.

Existing environment variables always win. A key already set in the real
environment is never overwritten by the file, so a shell export or a CI
secret beats a stale committed .env.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILENAME = ".env"


def parse_env(text: str) -> dict[str, str]:
    """Parse .env text into a mapping. Malformed lines are skipped."""
    values: dict[str, str] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()

        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            # Quoted: take it literally, but expand \n in double quotes only.
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = value.replace("\\n", "\n").replace("\\t", "\t")
        else:
            # Unquoted: a # starts a comment.
            hash_pos = value.find(" #")
            if hash_pos != -1:
                value = value[:hash_pos]
            value = value.strip()

        values[key] = value

    return values


def find_env_file(start: Path | None = None) -> Path | None:
    """
    Look for a .env next to the project, then walk up a few directories.

    Walking up means running the tool from a subfolder still finds the key,
    which is the behaviour people expect from every other tool that reads
    .env files.
    """
    start = (start or Path(__file__).resolve().parent.parent).resolve()
    for directory in [start, *start.parents][:5]:
        candidate = directory / ENV_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_env(path: str | Path | None = None, override: bool = False) -> dict[str, str]:
    """
    Load a .env into os.environ and return what was found in the file.

    Returns the file's contents whether or not each key was applied, so the
    caller can report "found a key in .env" accurately.
    """
    env_path = Path(path) if path else find_env_file()
    if env_path is None or not env_path.is_file():
        return {}

    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    values = parse_env(text)
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values
