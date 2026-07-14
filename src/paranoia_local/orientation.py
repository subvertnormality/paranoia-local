"""Build the orientation packet: a neutral briefing that tells the reviewer
*what* to review and *what claims to test*, then gets out of the way.

Unlike the API-era paranoia, this does NOT assemble the full evidence base —
the reviewer has read access to the repo and gathers evidence itself. The
packet is a map, not the territory: refs, diffstat, commit subjects, a bounded
embed of the diff, and the author-stated context/intent framed as claims.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# Above this, we stop embedding the raw diff and tell the reviewer to run
# `git diff` itself. The reviewer has repo access, so nothing is lost — this
# only keeps the prompt from ballooning on large branches.
MAX_EMBED_CHARS = 200_000


def _run(cmd: list[str], cwd: Path) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")
    return r.stdout


def diffstat(repo: Path, base: str, head: str) -> str:
    return _run(["git", "diff", "--stat", f"{base}...{head}"], repo).strip()


def commit_subjects(repo: Path, base: str, head: str) -> str:
    """Oldest-first one-line subjects — the author's own narrative of intent."""
    try:
        return _run(
            ["git", "log", "--reverse", "--pretty=format:%h %s", f"{base}..{head}"], repo
        ).strip()
    except RuntimeError:
        return ""


def full_diff(repo: Path, base: str, head: str) -> str:
    return _run(["git", "diff", f"{base}...{head}"], repo)


def touched_files(repo: Path, base: str, head: str) -> list[str]:
    out = _run(["git", "diff", "--name-only", f"{base}...{head}"], repo)
    return [line for line in out.splitlines() if line.strip()]


def _untracked(repo: Path) -> list[str]:
    out = _run(["git", "ls-files", "--others", "--exclude-standard"], repo)
    return [line for line in out.splitlines() if line.strip()]


def working_tree_diff(repo: Path) -> str:
    """Diff of the dirty working tree against HEAD.

    Non-invasive: it does not touch the index. Tracked changes come from
    `git diff HEAD`; untracked files are listed by name (the reviewer has
    read access and opens them itself) rather than staged with `git add -N`,
    which would mutate the author's index.
    """
    tracked = _run(["git", "diff", "HEAD"], repo)
    untracked = _untracked(repo)
    if untracked:
        listing = "\n".join(f"+ (untracked) {p}" for p in untracked)
        return f"{tracked}\n\n=== UNTRACKED FILES (read them directly) ===\n{listing}"
    return tracked


def working_tree_stat(repo: Path) -> str:
    stat = _run(["git", "diff", "--stat", "HEAD"], repo).strip()
    untracked = _untracked(repo)
    if untracked:
        stat += f"\n(+ {len(untracked)} untracked file(s))"
    return stat


@dataclass(frozen=True)
class Target:
    """What is under review."""

    description: str
    base_ref: str | None
    head_ref: str | None
    is_dirty: bool


def resolve_target(
    repo: Path,
    base_ref: str = "main",
    head_ref: str | None = "HEAD",
    include_uncommitted: bool = False,
) -> Target:
    """Decide whether we're reviewing a committed range or the working tree."""
    if include_uncommitted or head_ref is None:
        return Target(
            description=f"uncommitted working tree (dirty changes vs HEAD) in {repo.name}",
            base_ref="HEAD",
            head_ref=None,
            is_dirty=True,
        )
    return Target(
        description=f"committed diff {base_ref}...{head_ref} in {repo.name}",
        base_ref=base_ref,
        head_ref=head_ref,
        is_dirty=False,
    )


def build_orientation(
    repo: Path,
    target: Target,
    project_summary: str | None,
    diff_intent: str | None,
    focus: str | None,
    already_raised: list[str],
) -> str:
    """Assemble the orientation packet as labelled sections."""
    if target.is_dirty:
        stat = working_tree_stat(repo)
        diff = working_tree_diff(repo)
        subjects = ""
        diff_cmd = "git diff HEAD"
    else:
        base, head = target.base_ref, target.head_ref
        stat = diffstat(repo, base, head)
        diff = full_diff(repo, base, head)
        subjects = commit_subjects(repo, base, head)
        diff_cmd = f"git diff {base}...{head}"

    parts: list[str] = [f"=== UNDER REVIEW ===\n{target.description}"]

    if project_summary:
        parts.append(
            "=== AUTHOR-STATED PROJECT CONTEXT (description, not advocacy) ===\n"
            + project_summary
        )
    if diff_intent:
        parts.append(
            "=== AUTHOR-STATED DIFF INTENT (a CLAIM to test, not a fact to accept) ===\n"
            + diff_intent
        )
    if focus:
        parts.append(f"=== REVIEWER FOCUS ===\n{focus}")

    if subjects:
        parts.append(f"=== COMMIT SUBJECTS (oldest first) ===\n{subjects}")
    if stat:
        parts.append(f"=== DIFFSTAT ===\n{stat}")

    if len(diff) > MAX_EMBED_CHARS:
        parts.append(
            f"=== DIFF (truncated at {MAX_EMBED_CHARS} chars — run `{diff_cmd}` "
            f"in your working directory to read the rest) ===\n"
            + diff[:MAX_EMBED_CHARS]
        )
    else:
        parts.append(f"=== DIFF ({diff_cmd}) ===\n{diff}")

    if already_raised:
        rendered = "\n".join(f"- {claim}" for claim in already_raised)
        parts.append(
            "=== Already-raised — do NOT restate these; hunt for what they missed ===\n"
            + rendered
        )

    return "\n\n".join(parts)
