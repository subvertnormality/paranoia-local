"""Thin subprocess wrapper — the one impure edge, isolated so engines stay
unit-testable with an injected fake runner."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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


def run_streaming(
    argv: list[str],
    stdin_text: str,
    cwd: Path,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    on_line: Callable[[str], None] | None = None,
) -> RunResult:
    """Like ``run_capture`` but surfaces stdout lines to ``on_line`` AS THEY ARRIVE,
    so a long agentic review can report progress instead of a silent multi-minute
    wait. Semantics match ``run_capture``: timeout → rc 124 with empty stdout;
    missing executable → rc 127. ``on_line`` failures are swallowed — progress
    must never break a review."""
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            text=True,
        )
    except FileNotFoundError as exc:
        return RunResult(
            returncode=127,
            stdout="",
            stderr=f"executable not found: {exc}",
        )

    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(timeout, _kill)
    watchdog.daemon = True
    watchdog.start()

    # stdin and stderr are pumped on their own threads: a child that floods
    # stdout before consuming stdin (or vice versa) must not deadlock on full
    # pipe buffers while the main thread streams stdout.
    def _feed_stdin() -> None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # child exited early; its return code tells the story

    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        for line in proc.stderr:
            stderr_chunks.append(line)

    feeder = threading.Thread(target=_feed_stdin, daemon=True)
    drainer = threading.Thread(target=_drain_stderr, daemon=True)
    feeder.start()
    drainer.start()

    out_chunks: list[str] = []
    for line in proc.stdout:
        out_chunks.append(line)
        if on_line is not None:
            try:
                on_line(line)
            except Exception:  # noqa: BLE001 — progress must never break a review
                pass

    returncode = proc.wait()
    watchdog.cancel()
    feeder.join(timeout=5)
    drainer.join(timeout=5)

    if timed_out.is_set():
        return RunResult(returncode=124, stdout="", stderr=f"timed out after {timeout}s")
    return RunResult(
        returncode=returncode, stdout="".join(out_chunks), stderr="".join(stderr_chunks)
    )
