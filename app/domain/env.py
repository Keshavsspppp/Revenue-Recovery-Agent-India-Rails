"""Read `.env` into the environment.

The README tells you to `cp .env.example .env` and put your key in it, so something has
to actually read the file. Eight lines of standard library rather than a dependency: the
repo has to install and run where `pip install python-dotenv` is not an option, and one
key is not worth breaking that for.

The real environment wins over the file, which is the usual precedence and the one that
matters in CI — an exported key should not be silently overridden by a stale checkout.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / ".env"


def load_dotenv(path: str | Path = DEFAULT_PATH, override: bool = False) -> list[str]:
    """Returns the names of the variables it set, never their values."""
    file = Path(path)
    if not file.exists():
        return []
    loaded: list[str] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if not value:
            continue                       # an empty placeholder is not a setting
        if override or key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
