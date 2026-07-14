"""Thin subprocess wrapper — the one impure edge, isolated so engines stay
unit-testable with an injected fake runner."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# An agentic review is many turns of tool use; it can run for many minutes.
DEFAULT_TIMEOUT_SEC = 3600


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


def run_capture(
    argv: list[str],
    stdin_text: str,
    cwd: Path,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> RunResult:
    try:
        proc = subprocess.run(
            argv,
            input=stdin_text,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return RunResult(
            returncode=124,
            stdout="",
            stderr=f"timed out after {timeout}s",
        )
    except FileNotFoundError as exc:
        return RunResult(
            returncode=127,
            stdout="",
            stderr=f"executable not found: {exc}",
        )
    return RunResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
