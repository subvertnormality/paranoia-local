"""Console entry point: `paranoia-local --engine {codex|claude}`.

Installed into a coding-agent CLI as an MCP server. `--engine` names the OTHER
agent — the one that will perform reviews — so from Claude Code you run
`--engine codex`, and from Codex you run `--engine claude`.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .logs import DEFAULT_LOG_DIR
from .server import build_server, run_stdio


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="paranoia-local",
        description="Cross-agent adversarial review over local coding-agent CLIs.",
    )
    parser.add_argument(
        "--engine",
        required=True,
        choices=["codex", "claude"],
        help="Which local engine performs reviews (the OTHER agent from the caller).",
    )
    parser.add_argument(
        "--log-dir",
        default=str(DEFAULT_LOG_DIR),
        help=f"Audit-log directory (default {DEFAULT_LOG_DIR}).",
    )
    args = parser.parse_args()

    server_obj = build_server(
        default_engine_name=args.engine,
        log_dir=Path(args.log_dir).expanduser(),
    )
    asyncio.run(run_stdio(server_obj))


if __name__ == "__main__":
    main()
