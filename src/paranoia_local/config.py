"""Per-repo defaults from an optional `.paranoia.toml` at the repo root.

Lets a project stop retyping its `project_summary`, base ref, and review
defaults on every call. Keys may sit at the top level or under a `[paranoia]`
table. A missing or malformed file is not an error — it just means no defaults.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

CONFIG_FILENAME = ".paranoia.toml"


def load_repo_config(repo: Path) -> dict[str, Any]:
    path = repo / CONFIG_FILENAME
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return {}
    try:
        data = tomllib.loads(raw.decode("utf-8", errors="replace"))
    except tomllib.TOMLDecodeError:
        return {}
    if isinstance(data.get("paranoia"), dict):
        return data["paranoia"]
    return data


def resolve(key: str, explicit: Any, cfg: dict[str, Any], default: Any) -> Any:
    """Precedence: explicit call arg > repo config > hardcoded default."""
    if explicit is not None:
        return explicit
    if key in cfg and cfg[key] is not None:
        return cfg[key]
    return default
