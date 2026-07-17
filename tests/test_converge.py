"""The opt-in `converge` path on critique_branch: pre-gather a deterministic packet and
review it against an immutable materialized worktree (never the live mutable tree). See
docs/orientation_reuse_plan.md.
"""

import json
from pathlib import Path

from paranoia_local import handlers
from paranoia_local.engines import Review
from tests.conftest import git


class FakeEngine:
    name = "fake"
    default_model = "fake-model"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, prompt, cwd, model, effort, web_search, **kw):
        self.calls.append({"prompt": prompt, "cwd": Path(cwd)})
        return Review(
            text="## What works\nok", session_ref="sess", raw="{}",
            usage={"cost_usd": 0.01}, duration_ms=42,
        )


def _dirty(repo: Path) -> None:
    (repo / "app.py").write_text("# dirty edit MARKER\n")


def _args(repo: Path, **extra) -> dict:
    return {"repo_path": str(repo), "include_uncommitted": True, **extra}


class TestConvergeBranch:
    def test_runs_in_materialized_worktree_not_live_repo(self, repo: Path, tmp_path: Path) -> None:
        _dirty(repo)
        fake = FakeEngine()
        handlers.critique_branch(_args(repo, converge=True), engine=fake, log_dir=tmp_path)
        cwd = fake.calls[0]["cwd"]
        assert cwd != repo
        assert "paranoia-wt" in str(cwd)
        assert not cwd.exists()  # throwaway worktree cleaned up after the review

    def test_packet_prompt_embeds_evidence(self, repo: Path, tmp_path: Path) -> None:
        _dirty(repo)
        fake = FakeEngine()
        handlers.critique_branch(_args(repo, converge=True), engine=fake, log_dir=tmp_path)
        prompt = fake.calls[0]["prompt"]
        assert "gathered for you" in prompt.lower()  # packet-aware instructions
        assert "# dirty edit MARKER" in prompt        # full file evidence embedded

    def test_overrides_isolate_false(self, repo: Path, tmp_path: Path) -> None:
        _dirty(repo)
        fake = FakeEngine()
        handlers.critique_branch(_args(repo, converge=True, isolate=False), engine=fake, log_dir=tmp_path)
        assert "paranoia-wt" in str(fake.calls[0]["cwd"])  # still materialized

    def test_logs_mode_and_usage(self, repo: Path, tmp_path: Path) -> None:
        _dirty(repo)
        fake = FakeEngine()
        handlers.critique_branch(_args(repo, converge=True), engine=fake, log_dir=tmp_path)
        rec = json.loads(next(tmp_path.glob("*.json")).read_text())
        assert rec["mode"] == "converge-packet"
        assert rec["usage"] == {"cost_usd": 0.01}

    def test_default_off_runs_in_live_repo_for_dirty(self, repo: Path, tmp_path: Path) -> None:
        _dirty(repo)
        fake = FakeEngine()
        handlers.critique_branch(_args(repo), engine=fake, log_dir=tmp_path)  # no converge
        assert fake.calls[0]["cwd"] == repo  # unchanged behaviour: live repo for dirty

    def test_converge_on_unborn_repo(self, tmp_path: Path) -> None:
        # A repo with files but no first commit must not crash on resolve_head.
        repo = tmp_path / "unborn"
        repo.mkdir()
        git(["init", "-q", "-b", "main"], repo)
        (repo / "a.py").write_text("UNBORN_MARKER = 1\n")
        fake = FakeEngine()
        handlers.critique_branch(
            {"repo_path": str(repo), "include_uncommitted": True, "converge": True},
            engine=fake, log_dir=tmp_path / "logs",
        )
        assert "UNBORN_MARKER = 1" in fake.calls[0]["prompt"]  # new file's content packaged

    def test_failed_review_is_surfaced_and_logged(self, repo: Path, tmp_path: Path) -> None:
        _dirty(repo)

        class FailEngine(FakeEngine):
            def run(self, prompt, cwd, model, effort, web_search, **kw):
                self.calls.append({"prompt": prompt, "cwd": Path(cwd)})
                return Review(text="boom", session_ref=None, raw="", returncode=1, error=True)

        fe = FailEngine()
        out = handlers.critique_branch(_args(repo, converge=True), engine=fe, log_dir=tmp_path)
        assert "REVIEW FAILED" in out
        rec = json.loads(next(tmp_path.glob("*.json")).read_text())
        assert rec["error"] is True and rec["returncode"] == 1
