from pathlib import Path

import pytest

from paranoia_local import engines
from paranoia_local.runner import RunResult


CODEX_JSONL = (
    '{"type":"thread.started","thread_id":"abc-123"}\n'
    '{"type":"turn.started"}\n'
    '{"type":"item.completed","item":{"id":"i0","type":"reasoning","text":"thinking"}}\n'
    '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"## What works\\nNothing notable."}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":10}}\n'
)

CLAUDE_JSON = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"result":"## What works\\nNothing notable.","session_id":"sess-xyz",'
    '"total_cost_usd":0.01}'
)


class TestFactory:
    def test_get_codex(self) -> None:
        assert engines.get_engine("codex").name == "codex"

    def test_get_claude(self) -> None:
        assert engines.get_engine("claude").name == "claude"

    def test_unknown_engine_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown engine"):
            engines.get_engine("gemini")

    def test_default_models(self) -> None:
        assert "gpt-5.6" in engines.get_engine("codex").default_model
        assert "fable" in engines.get_engine("claude").default_model


class TestCodexArgv:
    def test_build_argv_read_only_and_model_and_effort(self) -> None:
        e = engines.get_engine("codex")
        argv = e.build_argv(cwd=Path("/repo"), model="gpt-5.6-sol", effort="high", web_search=True)
        assert argv[:2] == ["codex", "exec"]
        assert "--json" in argv
        joined = " ".join(argv)
        assert "-s read-only" in joined
        assert "-C /repo" in joined
        assert "-m gpt-5.6-sol" in joined
        assert 'model_reasoning_effort="high"' in joined
        assert "tools.web_search=true" in joined
        assert argv[-1] == "-"  # prompt read from stdin

    def test_web_search_off_omits_flag(self) -> None:
        e = engines.get_engine("codex")
        argv = e.build_argv(cwd=Path("/repo"), model="m", effort="high", web_search=False)
        assert "tools.web_search=true" not in " ".join(argv)

    def test_resume_argv_targets_session(self) -> None:
        e = engines.get_engine("codex")
        argv = e.build_resume_argv(session_ref="abc-123", cwd=Path("/repo"), model="m", effort="high", web_search=False)
        assert argv[:3] == ["codex", "exec", "resume"]
        assert "abc-123" in argv
        assert argv[-1] == "-"
        # `codex exec resume` rejects -s and -C; they must not appear
        assert "-s" not in argv
        assert "-C" not in argv

    def test_parse_output_extracts_final_message_and_thread(self) -> None:
        e = engines.get_engine("codex")
        review = e.parse_output(CODEX_JSONL)
        assert review.text == "## What works\nNothing notable."
        assert review.session_ref == "abc-123"

    def test_parse_tolerates_garbage_lines(self) -> None:
        e = engines.get_engine("codex")
        review = e.parse_output("not json\n" + CODEX_JSONL + "trailing noise\n")
        assert review.text == "## What works\nNothing notable."


class TestClaudeArgv:
    def test_build_argv_print_json_model_effort(self) -> None:
        e = engines.get_engine("claude")
        argv = e.build_argv(cwd=Path("/repo"), model="claude-fable-5", effort="high", web_search=True)
        assert argv[0] == "claude"
        assert "-p" in argv
        joined = " ".join(argv)
        assert "--output-format json" in joined
        assert "--model claude-fable-5" in joined
        assert "--effort high" in joined

    def test_allowlist_is_read_only(self) -> None:
        e = engines.get_engine("claude")
        argv = e.build_argv(cwd=Path("/repo"), model="m", effort="high", web_search=True)
        allowed = argv[argv.index("--allowedTools") + 1]
        assert "Read" in allowed
        assert "Bash(git diff:*)" in allowed
        assert "WebSearch" in allowed
        # write tools must be denied
        disallowed = argv[argv.index("--disallowedTools") + 1]
        assert "Write" in disallowed
        assert "Edit" in disallowed

    def test_web_search_off_drops_web_tools(self) -> None:
        e = engines.get_engine("claude")
        argv = e.build_argv(cwd=Path("/repo"), model="m", effort="high", web_search=False)
        allowed = argv[argv.index("--allowedTools") + 1]
        assert "WebSearch" not in allowed

    def test_loads_no_settings_sources(self) -> None:
        # Hermetic read-only: the reviewed repo's (or the user's) own
        # .claude settings must NOT widen the reviewer's permissions. Loading
        # zero settings sources makes paranoia's --allowedTools the sole
        # authority. Empty value = load none (verified against the real CLI).
        e = engines.get_engine("claude")
        argv = e.build_argv(cwd=Path("/repo"), model="m", effort="high", web_search=True)
        i = argv.index("--setting-sources")
        assert argv[i + 1] == ""

    def test_resume_also_loads_no_settings_sources(self) -> None:
        e = engines.get_engine("claude")
        argv = e.build_resume_argv(session_ref="s", cwd=Path("/repo"), model="m", effort="high", web_search=False)
        i = argv.index("--setting-sources")
        assert argv[i + 1] == ""

    def test_resume_argv_targets_session(self) -> None:
        e = engines.get_engine("claude")
        argv = e.build_resume_argv(session_ref="sess-xyz", cwd=Path("/repo"), model="m", effort="high", web_search=False)
        assert "--resume" in argv
        assert "sess-xyz" in argv

    def test_parse_output_extracts_result_and_session(self) -> None:
        e = engines.get_engine("claude")
        review = e.parse_output(CLAUDE_JSON)
        assert review.text == "## What works\nNothing notable."
        assert review.session_ref == "sess-xyz"

    def test_parse_non_json_falls_back_to_raw(self) -> None:
        e = engines.get_engine("claude")
        review = e.parse_output("plain text review, no json")
        assert "plain text" in review.text
        assert review.session_ref is None


class TestRunWithInjectedRunner:
    def test_run_pipes_prompt_to_stdin_and_parses(self) -> None:
        e = engines.get_engine("codex")
        captured = {}

        def fake_runner(argv, stdin_text, cwd, timeout):
            captured["argv"] = argv
            captured["stdin"] = stdin_text
            captured["cwd"] = cwd
            return RunResult(returncode=0, stdout=CODEX_JSONL, stderr="")

        review = e.run(
            prompt="REVIEW THIS", cwd=Path("/repo"), model="m", effort="high",
            web_search=False, runner=fake_runner,
        )
        assert captured["stdin"] == "REVIEW THIS"
        assert captured["cwd"] == Path("/repo")
        assert review.text == "## What works\nNothing notable."

    def test_run_surfaces_nonzero_exit_as_error_text(self) -> None:
        e = engines.get_engine("claude")

        def fake_runner(argv, stdin_text, cwd, timeout):
            return RunResult(returncode=1, stdout="", stderr="auth failed: not logged in")

        review = e.run(
            prompt="x", cwd=Path("/repo"), model="m", effort="high",
            web_search=False, runner=fake_runner,
        )
        assert "error" in review.text.lower()
        assert "auth failed" in review.text

    def test_resume_uses_resume_argv(self) -> None:
        e = engines.get_engine("claude")
        captured = {}

        def fake_runner(argv, stdin_text, cwd, timeout):
            captured["argv"] = argv
            return RunResult(returncode=0, stdout=CLAUDE_JSON, stderr="")

        e.resume(
            session_ref="sess-xyz", prompt="rebuttal", cwd=Path("/repo"),
            model="m", effort="high", web_search=False, runner=fake_runner,
        )
        assert "--resume" in captured["argv"]
