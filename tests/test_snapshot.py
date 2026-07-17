"""Phase-1 snapshot/plumbing core: capture the reviewed working state as an immutable
git commit whose `<parent>..<wrapper>` range reproduces the dirty patch, and enumerate
structured change-sets. Objects land in the repo's own store but NO ref is created, so
git GC reclaims them and a worktree can read the wrapper with no special env. See
docs/orientation_reuse_plan.md.
"""

import subprocess
from pathlib import Path

from paranoia_local import orientation as o
from paranoia_local.worktree import worktree_at
from tests.conftest import commit_all, git


def _dirty(repo: Path) -> None:
    """Dirty the working tree: edit a tracked file, add an untracked one."""
    (repo / "app.py").write_text('"""App."""\n\n\ndef greet(name):\n    return f"yo {name}"\n')
    (repo / "brand_new.py").write_text("x = 1\n")


def _read_git(args: list[str], cwd: Path) -> str:
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(cwd),
    }
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


class TestResolveHead:
    def test_matches_rev_parse(self, repo: Path) -> None:
        assert o.resolve_head(repo) == _read_git(["rev-parse", "HEAD"], repo).strip()


class TestStateless:
    def test_snapshot_and_wrap_create_no_ref(self, repo: Path) -> None:
        # Round-5's concern was refs pinning content indefinitely. We create NONE:
        # the wrapper is an unreferenced (GC-able) object, not anchored by any ref.
        _dirty(repo)
        head = o.resolve_head(repo)
        refs_before = _read_git(["for-each-ref"], repo)
        wrap = o.wrap_commit(repo, o.snapshot_tree(repo, head), head)
        assert _read_git(["for-each-ref"], repo) == refs_before  # no ref created
        assert wrap in _read_git(["fsck", "--unreachable", "--no-reflogs"], repo)


class TestSnapshotTree:
    def test_captures_dirty_tracked_and_untracked(self, repo: Path) -> None:
        _dirty(repo)
        tree = o.snapshot_tree(repo, o.resolve_head(repo))
        listing = _read_git(["ls-tree", "-r", "--name-only", tree], repo)
        assert "app.py" in listing and "brand_new.py" in listing
        assert "yo {name}" in _read_git(["show", f"{tree}:app.py"], repo)

    def test_captures_tracked_but_ignored_dirty(self, repo: Path) -> None:
        # A tracked file that also matches .gitignore, with a dirty change, must still be
        # snapshotted — this is why the recipe does `read-tree HEAD` before `add -A`.
        (repo / ".gitignore").write_text("secret.txt\n")
        (repo / "secret.txt").write_text("v1\n")
        git(["add", "-f", "secret.txt", ".gitignore"], repo)
        commit_all(repo, "track ignored file")
        (repo / "secret.txt").write_text("v2-dirty\n")
        tree = o.snapshot_tree(repo, o.resolve_head(repo))
        assert "v2-dirty" in _read_git(["show", f"{tree}:secret.txt"], repo)

    def test_does_not_touch_author_index(self, repo: Path) -> None:
        _dirty(repo)
        before = _read_git(["status", "--porcelain"], repo)
        o.snapshot_tree(repo, o.resolve_head(repo))
        assert _read_git(["status", "--porcelain"], repo) == before


class TestWrapCommit:
    def test_wraps_on_head_with_message(self, repo: Path) -> None:
        _dirty(repo)
        head = o.resolve_head(repo)
        wrap = o.wrap_commit(repo, o.snapshot_tree(repo, head), head)
        parents = _read_git(["rev-list", "--parents", "-n", "1", wrap], repo).split()
        assert parents[1] == head  # wrapper's sole parent is the resolved HEAD
        assert _read_git(["log", "-1", "--format=%s", wrap], repo).strip() == "paranoia-snapshot"

    def test_range_reproduces_dirty_patch(self, repo: Path) -> None:
        _dirty(repo)
        head = o.resolve_head(repo)
        wrap = o.wrap_commit(repo, o.snapshot_tree(repo, head), head)
        names = _read_git(["diff", "--name-status", f"{head}..{wrap}"], repo)
        assert "M\tapp.py" in names
        assert "A\tbrand_new.py" in names

    def test_deterministic_same_state_same_sha(self, repo: Path) -> None:
        # Pinned identity + dates ⇒ a given working state always wraps to the same sha.
        _dirty(repo)
        head = o.resolve_head(repo)
        a = o.wrap_commit(repo, o.snapshot_tree(repo, head), head)
        b = o.wrap_commit(repo, o.snapshot_tree(repo, head), head)
        assert a == b


class TestMaterialize:
    """The review runs against a throwaway worktree of the wrapper commit, so evidence is
    a consistent snapshot — the reviewer reads current bytes, git works with no special
    env, and edits to the live repo during a review can't perturb it."""

    def test_worktree_of_wrapper_has_dirty_snapshot(self, repo: Path) -> None:
        _dirty(repo)
        head = o.resolve_head(repo)
        wrap = o.wrap_commit(repo, o.snapshot_tree(repo, head), head)
        with worktree_at(repo, wrap) as wt:
            assert (wt / "brand_new.py").exists()               # untracked captured
            assert "yo {name}" in (wt / "app.py").read_text()   # dirty edit present
            # git works inside the worktree with ZERO special object-store env
            assert _read_git(["log", "-1", "--format=%s"], wt).strip() == "paranoia-snapshot"
            assert "M\tapp.py" in _read_git(["diff", "--name-status", f"{head}..HEAD"], wt)
        assert "paranoia-wt" not in _read_git(["worktree", "list"], repo)  # cleaned up

    def test_live_edit_during_review_does_not_change_the_worktree(self, repo: Path) -> None:
        _dirty(repo)
        head = o.resolve_head(repo)
        wrap = o.wrap_commit(repo, o.snapshot_tree(repo, head), head)
        with worktree_at(repo, wrap) as wt:
            (repo / "app.py").write_text("# the author kept editing the LIVE repo\n")
            assert "yo {name}" in (wt / "app.py").read_text()  # worktree is unaffected


class TestChangedFiles:
    def test_modified_added_deleted(self, repo: Path) -> None:
        base = o.resolve_head(repo)
        (repo / "app.py").write_text("# changed\n")
        (repo / "newf.py").write_text("y = 2\n")
        (repo / "README.md").unlink()
        commit_all(repo, "modify+add+delete")
        entries = {e.path: e for e in o.changed_files(repo, base, o.resolve_head(repo))}
        assert entries["app.py"].status == "M"
        assert entries["newf.py"].status == "A"
        assert entries["README.md"].status == "D"

    def test_rename_carries_old_and_new_path(self, repo: Path) -> None:
        base = o.resolve_head(repo)
        git(["mv", "app.py", "renamed.py"], repo)
        commit_all(repo, "rename app.py")
        renames = [e for e in o.changed_files(repo, base, o.resolve_head(repo)) if e.status == "R"]
        assert renames, "rename not detected"
        assert renames[0].old_path == "app.py"
        assert renames[0].path == "renamed.py"
