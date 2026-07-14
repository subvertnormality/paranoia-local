"""Best-effort audit trail. Each review writes a JSON record (engine, model,
session ref, target, review text) so a finding's provenance survives and
`rebut` has a session reference to resume. Logging must never crash a review,
so all failures are swallowed and reported as a `None` return.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_LOG_DIR = Path.home() / ".paranoia" / "logs"


def write_log(
    log_dir: Path,
    tool: str,
    record: dict[str, Any],
    timestamp: str,
) -> Path | None:
    # Logging is strictly best-effort: a completed review is the expensive
    # artifact, so nothing here may raise into the caller and discard it.
    try:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{timestamp}-{tool}.json"
        payload = {"timestamp": timestamp, "tool": tool, **record}
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path
    except Exception:  # noqa: BLE001 — never let audit logging break a review
        return None
