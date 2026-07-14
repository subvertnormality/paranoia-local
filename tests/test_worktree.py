from pathlib import Path

import pytest

from paranoia_local import worktree
from tests.conftest import git


class TestWorktreeAt:
    def test_yields_existing_path_with_ref_content(self, repo_with_branch: Path) -> None:
        with worktree.worktree_at(repo_with_branch, "feature") as wt:
            assert wt.exists()
            assert wt != repo_with_branch
            # content is the feature version
            assert "hello {name}!" in (wt / "app.py").read_text()
            assert (wt / "extra.py").exists()

    def test_base_ref_content_differs(self, repo_with_branch: Path) -> None:
        with worktree.worktree_at(repo_with_branch, "main") as wt:
            assert "hi {name}" in (wt / "app.py").read_text()
            assert not (wt / "extra.py").exists()

    def test_cleans_up_on_exit(self, repo_with_branch: Path) -> None:
        with worktree.worktree_at(repo_with_branch, "feature") as wt:
            captured = wt
        assert not captured.exists()
        listing = git(["worktree", "list"], repo_with_branch)
        assert str(captured) not in listing

    def test_cleans_up_on_exception(self, repo_with_branch: Path) -> None:
        captured = {}
        with pytest.raises(RuntimeError):
            with worktree.worktree_at(repo_with_branch, "feature") as wt:
                captured["p"] = wt
                raise RuntimeError("boom")
        assert not captured["p"].exists()

    def test_does_not_disturb_original_checkout(self, repo_with_branch: Path) -> None:
        # repo is on `feature`; snapshotting `main` must not switch it.
        before = git(["rev-parse", "--abbrev-ref", "HEAD"], repo_with_branch).strip()
        with worktree.worktree_at(repo_with_branch, "main"):
            pass
        after = git(["rev-parse", "--abbrev-ref", "HEAD"], repo_with_branch).strip()
        assert before == after == "feature"
