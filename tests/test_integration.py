"""End-to-end plumbing test: the REAL subprocess runner against fake `codex`
and `claude` binaries on PATH. Proves stdin piping, cwd/-C wiring, and output
parsing connect all the way through server.dispatch — without spending any
real subscription quota.
"""

import os
import stat
from pathlib import Path

import pytest

from paranoia_local import engines, server

FAKE_CODEX = """#!/bin/bash
prompt="$(cat)"
{ echo "ARGS: $@"; echo "PWD: $(pwd)"; echo "PROMPT<<"; echo "$prompt"; } > "$PARANOIA_FAKE_OUT"
printf '%s\\n' '{"type":"thread.started","thread_id":"fake-thread-1"}'
printf '%s\\n' '{"type":"item.completed","item":{"type":"agent_message","text":"FAKE CODEX REVIEW"}}'
printf '%s\\n' '{"type":"turn.completed","usage":{}}'
"""

FAKE_CLAUDE = """#!/bin/bash
prompt="$(cat)"
{ echo "ARGS: $@"; echo "PWD: $(pwd)"; echo "PROMPT<<"; echo "$prompt"; } > "$PARANOIA_FAKE_OUT"
printf '%s\\n' '{"type":"result","subtype":"success","is_error":false,"result":"FAKE CLAUDE REVIEW","session_id":"fake-sess-1"}'
"""


@pytest.fixture
def fake_bins(tmp_path: Path, monkeypatch):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    out = tmp_path / "fake_out.txt"
    for name, body in (("codex", FAKE_CODEX), ("claude", FAKE_CLAUDE)):
        p = bindir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("PARANOIA_FAKE_OUT", str(out))
    return out


class TestRealRunnerAgainstFakeCLIs:
    def test_codex_engine_pipes_prompt_sets_cwd_and_parses(self, repo, fake_bins):
        review = engines.get_engine("codex").run(
            prompt="HELLO-REVIEW-MARKER", cwd=repo, model="gpt-5.6-sol",
            effort="high", web_search=True,
        )
        assert review.text == "FAKE CODEX REVIEW"
        assert review.session_ref == "fake-thread-1"
        debug = fake_bins.read_text()
        assert "HELLO-REVIEW-MARKER" in debug            # stdin reached the CLI
        assert f"PWD: {repo}" in debug                    # cwd was set
        assert "-s read-only" in debug                    # read-only sandbox
        assert 'model_reasoning_effort="high"' in debug

    def test_claude_engine_parses_json_result(self, repo, fake_bins):
        review = engines.get_engine("claude").run(
            prompt="MARK-2", cwd=repo, model="claude-fable-5", effort="high", web_search=True,
        )
        assert review.text == "FAKE CLAUDE REVIEW"
        assert review.session_ref == "fake-sess-1"
        debug = fake_bins.read_text()
        assert "--output-format json" in debug
        assert "MARK-2" in debug


class TestDispatchEndToEnd:
    def test_query_full_stack(self, repo, tmp_path, fake_bins):
        out = server.dispatch(
            "query",
            {"question": "is greet() injection-safe?", "repo_path": str(repo)},
            default_engine_name="codex", log_dir=tmp_path / "logs", now=lambda: "t1",
        )
        assert "FAKE CODEX REVIEW" in out
        assert "fake-thread-1" in out  # session footer for rebut
        assert "injection-safe" in fake_bins.read_text()

    def test_critique_branch_runs_in_worktree(self, repo_with_branch, tmp_path, fake_bins):
        out = server.dispatch(
            "critique_branch",
            {"repo_path": str(repo_with_branch), "base_ref": "main", "head_ref": "feature"},
            default_engine_name="codex", log_dir=tmp_path / "logs", now=lambda: "t1",
        )
        assert "FAKE CODEX REVIEW" in out
        # the reviewer ran inside an isolated worktree, not the author's checkout
        debug = fake_bins.read_text()
        assert "paranoia-wt-" in debug
        assert f"PWD: {repo_with_branch}\n" not in debug

    def test_rebut_resumes_via_dispatch(self, repo, tmp_path, fake_bins):
        out = server.dispatch(
            "rebut",
            {"repo_path": str(repo), "session_ref": "fake-thread-1",
             "rebuttal": "that branch is unreachable"},
            default_engine_name="codex", log_dir=tmp_path / "logs", now=lambda: "t1",
        )
        assert "FAKE CODEX REVIEW" in out
        debug = fake_bins.read_text()
        assert "ARGS: exec resume fake-thread-1" in debug
        assert "unreachable" in debug
