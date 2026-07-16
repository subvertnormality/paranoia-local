"""Progress streaming: runner line-callback, engine event translation,
handler/server plumbing, and the no-delegation prompt rule.

The MCP client sees nothing until a tool call returns; these tests pin the
chain that turns the engine CLI's streaming JSON events into MCP progress
notifications so a 10-20 minute review is visibly alive."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

from paranoia_local import prompts, server
from paranoia_local.engines import ClaudeEngine, CodexEngine, Review
from paranoia_local.handlers import critique_plan
from paranoia_local.runner import run_streaming


class TestRunStreaming:
    def test_lines_surface_as_they_arrive_and_stdout_is_complete(self, tmp_path: Path) -> None:
        script = textwrap.dedent(
            """
            import sys
            data = sys.stdin.read()
            for i in range(3):
                print(f"line-{i}")
            sys.stderr.write("warn\\n")
            """
        )
        seen: list[str] = []
        result = run_streaming(
            [sys.executable, "-c", script], "the prompt", tmp_path,
            timeout=30, on_line=seen.append,
        )
        assert result.returncode == 0
        assert [s.strip() for s in seen] == ["line-0", "line-1", "line-2"]
        assert result.stdout.splitlines() == ["line-0", "line-1", "line-2"]
        assert "warn" in result.stderr

    def test_on_line_exception_never_breaks_the_run(self, tmp_path: Path) -> None:
        def boom(_line: str) -> None:
            raise RuntimeError("progress must never kill a review")

        result = run_streaming(
            [sys.executable, "-c", "import sys; sys.stdin.read(); print('ok')"],
            "", tmp_path, timeout=30, on_line=boom,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "ok"

    def test_timeout_kills_and_matches_run_capture_semantics(self, tmp_path: Path) -> None:
        result = run_streaming(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            "", tmp_path, timeout=1, on_line=None,
        )
        assert result.returncode == 124
        assert result.stdout == ""
        assert "timed out" in result.stderr

    def test_missing_executable_matches_run_capture_semantics(self, tmp_path: Path) -> None:
        result = run_streaming(["definitely-not-a-real-binary-xyz"], "", tmp_path, timeout=5)
        assert result.returncode == 127
        assert "executable not found" in result.stderr

    def test_large_stdin_does_not_deadlock(self, tmp_path: Path) -> None:
        # Child floods stdout BEFORE reading stdin — a naive write-then-read
        # implementation deadlocks on full pipe buffers.
        script = textwrap.dedent(
            """
            import sys
            for i in range(20000):
                print("x" * 64)
            sys.stdin.read()
            """
        )
        result = run_streaming(
            [sys.executable, "-c", script], "y" * 300_000, tmp_path, timeout=60,
        )
        assert result.returncode == 0
        assert len(result.stdout.splitlines()) == 20000


class TestCodexProgressTranslation:
    def setup_method(self) -> None:
        self.engine = CodexEngine()

    def test_thread_started(self) -> None:
        line = json.dumps({"type": "thread.started", "thread_id": "t-1"})
        assert self.engine.progress_from_line(line) == "reviewer session started"

    def test_command_execution_started(self) -> None:
        line = json.dumps(
            {"type": "item.started",
             "item": {"type": "command_execution", "command": "rg -n foo bar"}}
        )
        msg = self.engine.progress_from_line(line)
        assert msg is not None and "rg -n foo bar" in msg

    def test_long_command_truncated(self) -> None:
        line = json.dumps(
            {"type": "item.started",
             "item": {"type": "command_execution", "command": "x" * 500}}
        )
        msg = self.engine.progress_from_line(line)
        assert msg is not None and len(msg) <= 120

    def test_agent_message_completed(self) -> None:
        line = json.dumps(
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": "Scouting the dispatch path now."}}
        )
        msg = self.engine.progress_from_line(line)
        assert msg is not None and "Scouting the dispatch path" in msg

    def test_mcp_tool_call_started(self) -> None:
        line = json.dumps(
            {"type": "item.started",
             "item": {"type": "mcp_tool_call", "server": "paranoia", "tool": "critique_plan"}}
        )
        msg = self.engine.progress_from_line(line)
        assert msg is not None and "paranoia" in msg and "critique_plan" in msg

    def test_noise_lines_are_silent(self) -> None:
        assert self.engine.progress_from_line("not json at all") is None
        assert self.engine.progress_from_line(json.dumps({"type": "turn.started"})) is None
        assert self.engine.progress_from_line(json.dumps(["a", "list"])) is None
        assert self.engine.progress_from_line("") is None

    def test_claude_engine_has_no_line_progress(self) -> None:
        # claude -p --output-format json emits ONE blob at the end — nothing to stream.
        assert ClaudeEngine().progress_from_line('{"type": "anything"}') is None


class TestEngineProgressPlumbing:
    def test_run_with_on_progress_passes_on_line_and_translates(self, tmp_path: Path) -> None:
        events = [
            json.dumps({"type": "thread.started", "thread_id": "t-9"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "final review text"}}),
        ]

        def fake_runner(argv, stdin_text, cwd, timeout, on_line=None):
            assert on_line is not None, "on_progress must reach the runner as on_line"
            from paranoia_local.runner import RunResult
            for e in events:
                on_line(e)
            return RunResult(returncode=0, stdout="\n".join(events), stderr="")

        seen: list[str] = []
        review = CodexEngine().run(
            "prompt", tmp_path, model="m", effort="high", web_search=False,
            runner=fake_runner, on_progress=seen.append,
        )
        assert review.text == "final review text"
        assert review.session_ref == "t-9"
        assert len(seen) == 2  # turn.started is silent
        assert seen[0] == "reviewer session started"
        assert "final review text" in seen[1]

    def test_run_without_on_progress_keeps_legacy_runner_contract(self, tmp_path: Path) -> None:
        def legacy_runner(argv, stdin_text, cwd, timeout):  # 4-positional, no kwargs
            from paranoia_local.runner import RunResult
            return RunResult(returncode=0, stdout="", stderr="")

        review = CodexEngine().run(
            "prompt", tmp_path, model="m", effort="high", web_search=False,
            runner=legacy_runner,
        )
        assert review.text == ""


class TestHandlerAndDispatchPlumbing:
    class SpyEngine:
        name = "codex"
        default_model = "spy"

        def __init__(self) -> None:
            self.received_on_progress = "UNSET"

        def run(self, prompt, cwd, model, effort, web_search,
                runner=None, timeout=None, on_progress=None):
            self.received_on_progress = on_progress
            if on_progress:
                on_progress("engine says hi")
            return Review(text="R", session_ref="s", raw="")

        def resume(self, session_ref, prompt, cwd, model, effort, web_search,
                   runner=None, timeout=None, on_progress=None):
            self.received_on_progress = on_progress
            return Review(text="R", session_ref=session_ref, raw="")

    def test_critique_plan_threads_on_progress(self, tmp_path: Path) -> None:
        spy = self.SpyEngine()
        seen: list[str] = []
        critique_plan(
            {"plan_text": "# plan"}, engine=spy, log_dir=tmp_path,
            now=lambda: "t", on_progress=seen.append,
        )
        assert spy.received_on_progress is not None
        assert spy.received_on_progress != "UNSET"
        assert "engine says hi" in seen

    def test_dispatch_forwards_on_progress(self, tmp_path: Path, monkeypatch) -> None:
        spy = self.SpyEngine()
        monkeypatch.setattr(server, "get_engine", lambda name: spy)
        seen: list[str] = []
        out = server.dispatch(
            "critique_plan", {"plan_text": "# plan"},
            default_engine_name="codex", log_dir=tmp_path, now=lambda: "t",
            on_progress=seen.append,
        )
        assert "R" in out
        assert "engine says hi" in seen

    def test_dispatch_without_on_progress_unchanged(self, tmp_path: Path, monkeypatch) -> None:
        spy = self.SpyEngine()
        monkeypatch.setattr(server, "get_engine", lambda name: spy)
        out = server.dispatch(
            "critique_plan", {"plan_text": "# plan"},
            default_engine_name="codex", log_dir=tmp_path, now=lambda: "t",
        )
        assert "R" in out
        assert spy.received_on_progress is None


class TestNoDelegationRule:
    def test_plan_review_forbids_delegating_to_mcp_reviewers(self) -> None:
        assert "You ARE the reviewer" in prompts.PLAN_REVIEW_INSTRUCTIONS
        assert "paranoia" in prompts.PLAN_REVIEW_INSTRUCTIONS

    def test_code_review_forbids_delegating_to_mcp_reviewers(self) -> None:
        assert "You ARE the reviewer" in prompts.CODE_REVIEW_INSTRUCTIONS
        assert "paranoia" in prompts.CODE_REVIEW_INSTRUCTIONS

    def test_query_forbids_delegating_too(self) -> None:
        assert "You ARE the reviewer" in prompts.QUERY_INSTRUCTIONS
