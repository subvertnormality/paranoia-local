from pathlib import Path

import pytest

from paranoia_local import orientation as o
from tests.conftest import commit_all


class TestGitPrimitives:
    def test_diffstat_names_touched_files(self, repo_with_branch: Path) -> None:
        stat = o.diffstat(repo_with_branch, "main", "feature")
        assert "app.py" in stat
        assert "extra.py" in stat

    def test_commit_subjects_lists_branch_commits(self, repo_with_branch: Path) -> None:
        subjects = o.commit_subjects(repo_with_branch, "main", "feature")
        assert "friendlier greeting" in subjects

    def test_full_diff_contains_hunks(self, repo_with_branch: Path) -> None:
        diff = o.full_diff(repo_with_branch, "main", "feature")
        assert "hello {name}!" in diff
        assert "+def add(a, b):" in diff

    def test_touched_files_lists_changed_paths(self, repo_with_branch: Path) -> None:
        touched = o.touched_files(repo_with_branch, "main", "feature")
        assert set(touched) == {"app.py", "extra.py"}

    def test_working_tree_diff_sees_uncommitted(self, repo: Path) -> None:
        (repo / "app.py").write_text("# dirtied\n")
        diff = o.working_tree_diff(repo)
        assert "dirtied" in diff

    def test_working_tree_diff_includes_untracked(self, repo: Path) -> None:
        (repo / "brand_new.py").write_text("x = 1\n")
        diff = o.working_tree_diff(repo)
        assert "brand_new.py" in diff


class TestResolveTarget:
    def test_committed_range(self, repo_with_branch: Path) -> None:
        t = o.resolve_target(repo_with_branch, base_ref="main", head_ref="feature")
        assert t.description.startswith("committed diff")
        assert "main" in t.description and "feature" in t.description
        assert not t.is_dirty

    def test_uncommitted_working_tree(self, repo: Path) -> None:
        (repo / "app.py").write_text("# changed\n")
        t = o.resolve_target(repo, base_ref="main", head_ref=None, include_uncommitted=True)
        assert t.is_dirty
        assert "working tree" in t.description


class TestBuildOrientation:
    def test_embeds_diffstat_and_intent(self, repo_with_branch: Path) -> None:
        packet = o.build_orientation(
            repo_with_branch,
            target=o.resolve_target(repo_with_branch, "main", "feature"),
            project_summary="A greeting library.",
            diff_intent="Make the greeting friendlier.",
            focus=None,
            already_raised=[],
        )
        assert "A greeting library." in packet
        assert "Make the greeting friendlier." in packet
        assert "AUTHOR-STATED DIFF INTENT" in packet
        assert "app.py" in packet  # diffstat present

    def test_already_raised_rendered_as_claims(self, repo_with_branch: Path) -> None:
        packet = o.build_orientation(
            repo_with_branch,
            target=o.resolve_target(repo_with_branch, "main", "feature"),
            project_summary=None,
            diff_intent=None,
            focus=None,
            already_raised=["app.py:5 — greeting not escaped"],
        )
        assert "Already-raised" in packet
        assert "app.py:5 — greeting not escaped" in packet

    def test_focus_included_when_present(self, repo_with_branch: Path) -> None:
        packet = o.build_orientation(
            repo_with_branch,
            target=o.resolve_target(repo_with_branch, "main", "feature"),
            project_summary=None,
            diff_intent=None,
            focus="Look only at input validation.",
            already_raised=[],
        )
        assert "input validation" in packet

    def test_huge_diff_is_truncated_with_note(self, repo: Path, monkeypatch) -> None:
        target = o.resolve_target(repo, "main", "main")
        monkeypatch.setattr(o, "full_diff", lambda *a, **k: "X" * (o.MAX_EMBED_CHARS + 500))
        packet = o.build_orientation(
            repo, target=target, project_summary=None, diff_intent=None,
            focus=None, already_raised=[],
        )
        assert "truncated" in packet.lower()
        assert "git diff" in packet  # instructs reviewer to read the rest itself
