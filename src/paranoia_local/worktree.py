"""Snapshot a git ref into a throwaway detached worktree.

Reviewing a committed branch inside a temporary worktree means the reviewer
gets a clean, isolated checkout of exactly that ref — it can't collide with
the author's ongoing work, and the ref need not be the currently-checked-out
branch. (Dirty working-tree reviews cannot use this — uncommitted changes
live in no ref — so those run against the live repo directory.)
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _git(args: list[str], cwd: Path) -> None:
    # Capture bytes, not text: `git worktree add` runs a non-quiet `reset --hard` that echoes
    # the checked-out commit's subject, which for a committed review is author-controlled and
    # may not be valid UTF-8 — a strict text decode there would crash the review.
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {r.stderr.decode('utf-8', errors='replace').strip()}"
        )


@contextmanager
def worktree_at(repo: Path, ref: str) -> Iterator[Path]:
    """Create a detached worktree of `ref`, yield its path, always clean up."""
    tmp = Path(tempfile.mkdtemp(prefix="paranoia-wt-"))
    # git worktree add refuses to use an existing non-empty dir; mkdtemp made
    # it, so hand git a fresh child path.
    wt = tmp / "tree"
    try:
        _git(["worktree", "add", "--detach", str(wt), ref], repo)
        yield wt
    finally:
        # Best-effort teardown: remove the worktree registration, then the dir.
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt)],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        shutil.rmtree(tmp, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"], cwd=repo, capture_output=True, text=True
        )
