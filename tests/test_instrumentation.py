"""Engine instrumentation: capture usage/duration and detect real failures — including
Claude's in-band is_error with a zero exit code, which the old gate swallowed. This is
what lets the caller reproduce a cost decision and lets a fallback fire. See
docs/orientation_reuse_plan.md.
"""

from pathlib import Path

from paranoia_local import engines
from paranoia_local.runner import RunResult

CLAUDE_OK = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"result":"## What works\\nok","session_id":"s2","total_cost_usd":0.02,'
    '"duration_ms":1234,"usage":{"input_tokens":10}}'
)
CLAUDE_ERR = (
    '{"type":"result","subtype":"error_during_execution","is_error":true,'
    '"result":"boom","session_id":"s1"}'
)
CODEX_OK = (
    '{"type":"thread.started","thread_id":"t1"}\n'
    '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":7}}\n'
)


def _fake(rc: int, stdout: str, stderr: str = ""):
    def runner(argv, stdin, cwd, timeout):
        return RunResult(returncode=rc, stdout=stdout, stderr=stderr)
    return runner


class TestClaudeInstrumentation:
    def test_success_captures_usage_and_duration(self) -> None:
        r = engines.get_engine("claude").parse_output(CLAUDE_OK)
        assert r.error is False
        assert r.usage == {"tokens": {"input_tokens": 10}, "cost_usd": 0.02}
        assert r.duration_ms == 1234

    def test_in_band_error_flagged_even_with_text_and_rc0(self) -> None:
        r = engines.get_engine("claude").run(
            prompt="x", cwd=Path("/repo"), model="m", effort="high",
            web_search=False, runner=_fake(0, CLAUDE_ERR),
        )
        assert r.error is True       # rc 0 + is_error:true ⇒ failed (old gate missed this)
        assert r.returncode == 0
        assert "boom" in r.text      # text preserved so a fallback can inspect it


class TestCodexInstrumentation:
    def test_captures_usage_and_no_error(self) -> None:
        r = engines.get_engine("codex").parse_output(CODEX_OK)
        assert r.session_ref == "t1"
        assert r.usage == {"input_tokens": 5, "output_tokens": 7}
        assert r.error is False


class TestExecuteReturncode:
    def test_success_sets_returncode_zero_and_no_error(self) -> None:
        r = engines.get_engine("claude").run(
            prompt="x", cwd=Path("/repo"), model="m", effort="high",
            web_search=False, runner=_fake(0, CLAUDE_OK),
        )
        assert r.returncode == 0 and r.error is False

    def test_hard_failure_with_no_text_synthesizes_error(self) -> None:
        r = engines.get_engine("claude").run(
            prompt="x", cwd=Path("/repo"), model="m", effort="high",
            web_search=False, runner=_fake(1, "", "auth failed: not logged in"),
        )
        assert r.error is True and r.returncode == 1
        assert "auth failed" in r.text
