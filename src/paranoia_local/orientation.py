"""Build the orientation packet: a neutral briefing that tells the reviewer
*what* to review and *what claims to test*, then gets out of the way.

Unlike the API-era paranoia, this does NOT assemble the full evidence base —
the reviewer has read access to the repo and gathers evidence itself. The
packet is a map, not the territory: refs, diffstat, commit subjects, a bounded
embed of the diff, and the author-stated context/intent framed as claims.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Above this, we stop embedding the raw diff and tell the reviewer to run
# `git diff` itself. The reviewer has repo access, so nothing is lost — this
# only keeps the prompt from ballooning on large branches.
MAX_EMBED_CHARS = 200_000


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    # env, when given, is layered over the ambient environment — callers pass
    # only the git overrides they need (GIT_INDEX_FILE, GIT_OBJECT_DIRECTORY,
    # snapshot identity) without having to reconstruct PATH etc.
    r = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True,
        env={**os.environ, **env} if env else None,
    )
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")
    return r.stdout


# Snapshot git commands are hermetic (ignore the user's global/system config, so a
# stray commit.gpgsign or diff setting can't perturb them) and carry a fixed identity
# and dates, so a given working state always wraps to the same commit sha.
_HERMETIC_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
_SNAPSHOT_IDENTITY = {
    "GIT_AUTHOR_NAME": "paranoia",
    "GIT_AUTHOR_EMAIL": "paranoia@localhost",
    "GIT_COMMITTER_NAME": "paranoia",
    "GIT_COMMITTER_EMAIL": "paranoia@localhost",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00 +0000",
}


def resolve_head(repo: Path) -> str:
    """The current commit id — resolved ONCE so a concurrent HEAD move can't make
    later snapshot/diff commands describe a different revision."""
    return _run(["git", "rev-parse", "HEAD"], repo).strip()


def resolve_ref(repo: Path, ref: str) -> str:
    """Resolve a ref/rev to its object id, so packet + review commands are pinned."""
    return _run(["git", "rev-parse", ref], repo).strip()


def snapshot_tree(repo: Path, head_id: str) -> str:
    """Write a tree of the current working state and return its sha.

    Captures tracked (including tracked-but-ignored) AND untracked files: a private
    alternate index is seeded from `head_id` (`read-tree`) so tracked-but-ignored paths
    survive, then `add -A` stages everything. The author's real index is never touched.
    Objects land in the repo's own store, loose and unreferenced (see `wrap_commit`).
    """
    idx_dir = Path(tempfile.mkdtemp(prefix="paranoia-idx-"))
    env = {**_HERMETIC_ENV, "GIT_INDEX_FILE": str(idx_dir / "index")}
    try:
        _run(["git", "read-tree", head_id], repo, env=env)
        _run(["git", "add", "-A"], repo, env=env)
        return _run(["git", "write-tree"], repo, env=env).strip()
    finally:
        shutil.rmtree(idx_dir, ignore_errors=True)


def has_head(repo: Path) -> bool:
    """Whether the repo has a resolvable HEAD (False for an unborn repo with no commit)."""
    r = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=repo, capture_output=True, text=True
    )
    return r.returncode == 0


def empty_tree(repo: Path) -> str:
    """The empty-tree object id in THIS repo's object format — the base for an unborn repo.
    Computed (not a hard-coded SHA-1 constant) so SHA-256 repositories also work."""
    return _run(["git", "hash-object", "-t", "tree", "/dev/null"], repo).strip()


def wrap_commit(
    repo: Path, tree: str, parent: str | None, message: str = "paranoia-snapshot"
) -> str:
    """Wrap `tree` as a commit parented on `parent` (or parentless, for an unborn repo);
    `<parent-or-empty-tree>..<wrapper>` then reproduces the captured dirty patch. NO ref is
    created, so the wrapper is an unreferenced object that git GC reclaims once no worktree
    checks it out — nothing is pinned in the reviewed repo, and a materialized worktree can
    read it with no special object-store env. The message is passed explicitly (`-m`): bare
    `git commit-tree` reads it from stdin, which for the MCP server is the live transport.
    """
    env = {**_HERMETIC_ENV, **_SNAPSHOT_IDENTITY}
    args = ["git", "commit-tree", tree]
    if parent:
        args += ["-p", parent]
    args += ["-m", message]
    return _run(args, repo, env=env).strip()


@dataclass(frozen=True)
class ChangeEntry:
    """One entry of a structured change-set. `status` is a single letter (M/A/D/T/R/C).
    `path`/`old_path` are decoded with `surrogateescape`, so the exact bytes round-trip back
    to git (for `git show`) even for a non-UTF-8 filename — never render them into the packet
    directly; use `_display()` for a JSON/UTF-8-safe label."""

    status: str
    path: str
    old_path: str | None = None


def _display(name: str) -> str:
    """A display-safe, INJECTIVE rendering of a possibly `surrogateescape`-decoded path.
    Valid Unicode (e.g. `café.py`) is kept; a literal backslash is doubled first so it can't
    be confused with an escape introducer, then non-UTF-8 bytes carried as surrogates become
    `\\u….` escapes. The result therefore can't (a) collide two distinct paths onto one label
    (a real `a\\udcff` vs a `0xff`-byte path stay distinct), or (b) inject lone surrogates that
    crash JSON/UTF-8 encoding of the packet or the reviewer prompt stdin."""
    return name.replace("\\", "\\\\").encode("utf-8", "backslashreplace").decode("utf-8")


def changed_files(repo: Path, from_ref: str, to_ref: str) -> list[ChangeEntry]:
    """Structured file delta from `from_ref` to `to_ref` via `--name-status -z` (so
    deletions and renames are typed, not just listed). `-M` enables rename detection.

    `-z` emits pathnames verbatim, so this reads bytes and decodes with replacement — a
    non-UTF-8 filename must not crash the review. A path mangled by replacement won't
    round-trip to `git show`, so `file_evidence` will mark it `[not embeddable]` rather
    than embed it, which is the correct graceful degradation."""
    r = subprocess.run(
        ["git", "diff", "--name-status", "-M", "-z", from_ref, to_ref],
        cwd=repo, capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            "git diff --name-status failed: " + r.stderr.decode("utf-8", errors="replace").strip()
        )
    # surrogateescape keeps the exact bytes so a non-UTF-8 path round-trips back to `git show`
    # (no lossy collision onto another real file); `_display()` sanitizes it for the packet.
    return _parse_name_status(r.stdout.decode("utf-8", errors="surrogateescape"))


def _parse_name_status(out: str) -> list[ChangeEntry]:
    tokens = out.split("\0")
    entries: list[ChangeEntry] = []
    i, n = 0, len(tokens)
    while i < n:
        status = tokens[i]
        if not status:  # trailing NUL / blank
            i += 1
            continue
        code = status[0]
        if code in ("R", "C") and i + 2 < n:
            entries.append(ChangeEntry(status=code, path=tokens[i + 2], old_path=tokens[i + 1]))
            i += 3
        elif i + 1 < n:
            entries.append(ChangeEntry(status=code, path=tokens[i + 1]))
            i += 2
        else:
            break
    return entries


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


# ── Phase-1 deterministic packet ──────────────────────────────────────────────
# The packet pre-gathers the evidence a cold reviewer would otherwise spend turns
# re-reading every round: the full contents of every touched file in the exact
# snapshot under review, plus the diff. Paired with the packet-aware review prompt
# (prompts.CODE_REVIEW_INSTRUCTIONS_PACKET) so the reviewer skips the gather step.

MAX_PACKET_CHARS = 400_000  # evidence budget; header + `already_raised` are always included
MAX_FILE_CHARS = 50_000     # per-file evidence cap
_TRUNCATION_MARKER = (
    "=== EVIDENCE TRUNCATED ===\nPacket budget reached — open the remaining touched files "
    "in your worktree."
)


def _show_bytes(repo: Path, spec: str) -> bytes | None:
    """Raw bytes of a blob, or None if git can't produce one (e.g. a submodule gitlink)."""
    r = subprocess.run(["git", "show", spec], cwd=repo, capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _safe_diff(repo: Path, base: str, head: str) -> str:
    """`git diff base head` decoded defensively. Git's own text/binary heuristic sniffs
    only the first ~8000 bytes, so it can embed raw content (incl. NUL or non-UTF-8 bytes)
    for a file whose binary marker appears later. Read bytes and decode with replacement,
    and neutralize NUL, so the packet can never carry control bytes or crash on decode."""
    r = subprocess.run(["git", "diff", base, head], cwd=repo, capture_output=True)
    return r.stdout.decode("utf-8", errors="replace").replace("\x00", "�")


def file_evidence(
    repo: Path, head_id: str, entries: list[ChangeEntry], max_file_chars: int = MAX_FILE_CHARS
) -> list[tuple[ChangeEntry, str | None]]:
    """Current content of each touched file *in the snapshot* (`head_id`), capped. Deletions
    carry `None`. Binary blobs, unreadable paths, and submodule gitlinks are represented by a
    short marker instead of their raw bytes — embedding raw bytes would corrupt the packet and
    (with text decoding) crash the review."""
    out: list[tuple[ChangeEntry, str | None]] = []
    for e in entries:
        if e.status == "D":
            out.append((e, None))
            continue
        shown = _display(e.path)
        data = _show_bytes(repo, f"{head_id}:{e.path}")  # e.path (surrogateescape) → exact bytes
        if data is None:
            out.append((e, f"[not embeddable ({shown}) — submodule gitlink or unreadable; open it in your worktree]"))
            continue
        if b"\x00" in data:  # full scan — a NUL anywhere means binary (a late NUL is still binary)
            out.append((e, f"[binary file, {len(data)} bytes — not embedded; open {shown} in your worktree]"))
            continue
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            out.append((e, f"[non-UTF-8 file, {len(data)} bytes — not embedded; open {shown} in your worktree]"))
            continue
        if len(content) > max_file_chars:
            content = (
                content[:max_file_chars]
                + f"\n… [TRUNCATED at {max_file_chars} chars — open {shown} to read the rest]\n"
            )
        out.append((e, content))
    return out


def build_packet(
    repo: Path,
    base_id: str,
    head_id: str,
    *,
    project_summary: str | None = None,
    diff_intent: str | None = None,
    focus: str | None = None,
    already_raised: list[str] | None = None,
    max_chars: int = MAX_PACKET_CHARS,
    max_file_chars: int = MAX_FILE_CHARS,
) -> str:
    """Assemble the deterministic evidence packet for `base_id..head_id` (resolved ids, two-dot
    endpoint comparison used consistently for diffstat, diff, file list, and labels).

    Layout: mandatory header (context/intent/focus/diffstat) + budgeted evidence (diff, then
    each touched file's content) + mandatory `already_raised` block. `max_chars` bounds the
    packet whenever it exceeds the mandatory header + `already_raised`; those two are ALWAYS
    included (the firewall list is never dropped) even if they alone exceed `max_chars`. When
    evidence is trimmed a marker is emitted, and the marker's own size is reserved so the
    final packet stays within `max_chars` in the normal case."""
    already_raised = already_raised or []
    entries = changed_files(repo, base_id, head_id)

    head_parts = [
        f"=== UNDER REVIEW ===\nworking-tree snapshot {base_id[:12]}..{head_id[:12]} in {_display(repo.name)}"
    ]
    if project_summary:
        head_parts.append(
            "=== AUTHOR-STATED PROJECT CONTEXT (description, not advocacy) ===\n" + project_summary
        )
    if diff_intent:
        head_parts.append(
            "=== AUTHOR-STATED DIFF INTENT (a CLAIM to test, not a fact to accept) ===\n"
            + diff_intent
        )
    if focus:
        head_parts.append(f"=== REVIEWER FOCUS ===\n{focus}")
    stat_proc = subprocess.run(["git", "diff", "--stat", base_id, head_id], cwd=repo, capture_output=True)
    stat = stat_proc.stdout.decode("utf-8", errors="replace").strip()  # byte-safe: a non-UTF-8 path in --stat must not crash
    if stat:
        head_parts.append(f"=== DIFFSTAT ===\n{stat}")
    header = "\n\n".join(head_parts)

    reserved = ""
    if already_raised:
        reserved = "=== Already-raised — do NOT restate these; hunt for what they missed ===\n" + "\n".join(
            f"- {c}" for c in already_raised
        )

    evidence: list[str] = [
        f"=== DIFF (git diff {base_id[:12]}..{head_id[:12]}) ===\n{_safe_diff(repo, base_id, head_id)}"
    ]
    for e, content in file_evidence(repo, head_id, entries, max_file_chars):
        if content is None:
            evidence.append(f"=== FILE {_display(e.path)} [DELETED] ===\n(this file no longer exists in the snapshot)")
        else:
            label = (
                f"{_display(e.old_path or '')} → {_display(e.path)}"
                if e.status.startswith("R")
                else _display(e.path)
            )
            evidence.append(f"=== FILE {label} [{e.status}] ===\n{content}")

    sep = "\n\n"
    mandatory = len(header) + (len(sep) + len(reserved) if reserved else 0)
    marker_cost = len(sep) + len(_TRUNCATION_MARKER)
    available = max_chars - mandatory
    # First fit evidence WITHOUT reserving marker space, so evidence that fits on its own is
    # never dropped just to hold a marker that won't be needed.
    included: list[str] = []
    used = 0
    truncated = False
    for part in evidence:
        cost = len(part) + len(sep)
        if used + cost <= available:
            included.append(part)
            used += cost
        else:
            truncated = True
            break
    # If anything was omitted, an EVIDENCE TRUNCATED notice is MANDATORY (a silent omission
    # would make the reviewer treat the packet as complete). Backtrack evidence to make room;
    # if the budget can't even hold the notice alone, emit it anyway — that (max_chars <
    # mandatory + marker) is the documented too-small-budget case where the packet may exceed
    # max_chars rather than drop the omission signal.
    if truncated:
        while included and used + marker_cost > available:
            used -= len(included.pop()) + len(sep)

    sections = [header, *included]
    if truncated:
        sections.append(_TRUNCATION_MARKER)
    if reserved:
        sections.append(reserved)
    return sep.join(sections)
