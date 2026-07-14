from pathlib import Path

import pytest

from paranoia_local import handlers
from paranoia_local.engines import Review
from tests.conftest import git


class FakeEngine:
    name = "fake"
    default_model = "fake-model"

    def __init__(self, text: str = "REVIEW BODY", session_ref: str = "sess-1") -> None:
        self.calls: list[dict] = []
        self._text = text
        self._session = session_ref

    def run(self, prompt, cwd, model, effort, web_search, runner=None, timeout=None):
        self.calls.append(
            {"kind": "run", "prompt": prompt, "cwd": cwd, "model": model,
             "effort": effort, "web_search": web_search}
        )
        return Review(text=self._text, session_ref=self._session, raw="")

    def resume(self, session_ref, prompt, cwd, model, effort, web_search, runner=None, timeout=None):
        self.calls.append(
            {"kind": "resume", "session_ref": session_ref, "prompt": prompt, "cwd": cwd}
        )
        return Review(text="REBUTTAL VERDICT", session_ref=session_ref, raw="")


def fixed_clock() -> str:
    return "20260714T120000"


class TestCritiqueBranch:
    def test_runs_reviewer_in_isolated_worktree(self, repo_with_branch: Path, tmp_path: Path) -> None:
        eng = FakeEngine()
        out = handlers.critique_branch(
            {"repo_path": str(repo_with_branch), "base_ref": "main", "head_ref": "feature",
             "diff_intent": "friendlier greeting"},
            engine=eng, log_dir=tmp_path, now=fixed_clock,
        )
        assert "REVIEW BODY" in out
        call = eng.calls[0]
        # reviewer ran in a worktree, NOT the author's checkout
        assert call["cwd"] != repo_with_branch
        assert "paranoia-wt-" in str(call["cwd"])
        # orientation reached the reviewer
        assert "friendlier greeting" in call["prompt"]
        assert "What doesn't work" in call["prompt"]  # the instructions are composed in

    def test_footer_exposes_session_for_rebut(self, repo_with_branch: Path, tmp_path: Path) -> None:
        out = handlers.critique_branch(
            {"repo_path": str(repo_with_branch), "base_ref": "main", "head_ref": "feature"},
            engine=FakeEngine(session_ref="abc-999"), log_dir=tmp_path, now=fixed_clock,
        )
        assert "abc-999" in out

    def test_dirty_tree_runs_in_repo_not_worktree(self, repo: Path, tmp_path: Path) -> None:
        (repo / "app.py").write_text("# uncommitted edit\n")
        eng = FakeEngine()
        handlers.critique_branch(
            {"repo_path": str(repo), "include_uncommitted": True},
            engine=eng, log_dir=tmp_path, now=fixed_clock,
        )
        assert eng.calls[0]["cwd"] == repo

    def test_isolate_false_runs_in_repo(self, repo_with_branch: Path, tmp_path: Path) -> None:
        eng = FakeEngine()
        handlers.critique_branch(
            {"repo_path": str(repo_with_branch), "base_ref": "main", "head_ref": "feature",
             "isolate": False},
            engine=eng, log_dir=tmp_path, now=fixed_clock,
        )
        assert eng.calls[0]["cwd"] == repo_with_branch

    def test_already_raised_passed_through(self, repo_with_branch: Path, tmp_path: Path) -> None:
        eng = FakeEngine()
        handlers.critique_branch(
            {"repo_path": str(repo_with_branch), "base_ref": "main", "head_ref": "feature",
             "already_raised": ["app.py:5 — greeting not escaped"]},
            engine=eng, log_dir=tmp_path, now=fixed_clock,
        )
        assert "greeting not escaped" in eng.calls[0]["prompt"]

    def test_writes_audit_log(self, repo_with_branch: Path, tmp_path: Path) -> None:
        handlers.critique_branch(
            {"repo_path": str(repo_with_branch), "base_ref": "main", "head_ref": "feature"},
            engine=FakeEngine(), log_dir=tmp_path, now=fixed_clock,
        )
        logs = list(tmp_path.glob("*.json"))
        assert len(logs) == 1
        assert "critique_branch" in logs[0].name

    def test_repo_config_supplies_base_ref(self, repo_with_branch: Path, tmp_path: Path) -> None:
        # config sets base_ref so the caller can omit it
        (repo_with_branch / ".paranoia.toml").write_text('base_ref = "main"\n')
        git(["add", "-A"], repo_with_branch)
        eng = FakeEngine()
        handlers.critique_branch(
            {"repo_path": str(repo_with_branch), "head_ref": "feature"},
            engine=eng, log_dir=tmp_path, now=fixed_clock,
        )
        # a diff was computed against main (feature's change is visible)
        assert "hello {name}!" in eng.calls[0]["prompt"]

    def test_missing_repo_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not a git repo|does not exist"):
            handlers.critique_branch(
                {"repo_path": "/no/such/repo"}, engine=FakeEngine(),
                log_dir=tmp_path, now=fixed_clock,
            )


class TestCritiquePlan:
    def test_rejects_both_text_and_path(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not both"):
            handlers.critique_plan(
                {"plan_text": "x", "plan_path": "/tmp/y.md"},
                engine=FakeEngine(), log_dir=tmp_path, now=fixed_clock,
            )

    def test_rejects_neither(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="plan_text or plan_path"):
            handlers.critique_plan(
                {}, engine=FakeEngine(), log_dir=tmp_path, now=fixed_clock,
            )

    def test_plan_text_reaches_reviewer(self, tmp_path: Path) -> None:
        eng = FakeEngine()
        handlers.critique_plan(
            {"plan_text": "Step 1: rewrite the auth layer."},
            engine=eng, log_dir=tmp_path, now=fixed_clock,
        )
        assert "rewrite the auth layer" in eng.calls[0]["prompt"]

    def test_plan_path_is_read(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\nDo the risky thing.\n")
        eng = FakeEngine()
        handlers.critique_plan(
            {"plan_path": str(plan)}, engine=eng, log_dir=tmp_path, now=fixed_clock,
        )
        assert "risky thing" in eng.calls[0]["prompt"]

    def test_repo_grounding_runs_in_repo(self, repo: Path, tmp_path: Path) -> None:
        eng = FakeEngine()
        handlers.critique_plan(
            {"plan_text": "change greet()", "repo_path": str(repo)},
            engine=eng, log_dir=tmp_path, now=fixed_clock,
        )
        assert eng.calls[0]["cwd"] == repo
        assert "REPOSITORY IS AVAILABLE" in eng.calls[0]["prompt"]


class TestQuery:
    def test_direct_question_lower_effort(self, repo: Path, tmp_path: Path) -> None:
        eng = FakeEngine()
        handlers.query(
            {"question": "Is greet() injection-safe?", "repo_path": str(repo)},
            engine=eng, log_dir=tmp_path, now=fixed_clock,
        )
        call = eng.calls[0]
        assert "injection-safe" in call["prompt"]
        assert call["effort"] == "medium"  # query uses lower effort than reviews
        assert call["cwd"] == repo

    def test_question_required(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="question"):
            handlers.query({}, engine=FakeEngine(), log_dir=tmp_path, now=fixed_clock)


class TestRebut:
    def test_resumes_session(self, repo: Path, tmp_path: Path) -> None:
        eng = FakeEngine()
        out = handlers.rebut(
            {"repo_path": str(repo), "session_ref": "sess-1",
             "rebuttal": "That line is unreachable because X."},
            engine=eng, log_dir=tmp_path, now=fixed_clock,
        )
        assert "REBUTTAL VERDICT" in out
        call = eng.calls[0]
        assert call["kind"] == "resume"
        assert call["session_ref"] == "sess-1"
        assert "unreachable because X" in call["prompt"]

    def test_requires_session_and_rebuttal(self, repo: Path, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            handlers.rebut(
                {"repo_path": str(repo), "rebuttal": "x"},
                engine=FakeEngine(), log_dir=tmp_path, now=fixed_clock,
            )
