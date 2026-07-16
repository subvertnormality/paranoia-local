"""Engine abstraction — each engine drives a local coding-agent CLI in a
headless, read-only mode over the user's subscription.

The CLI *is* the reviewer: it has full read access to the repo at `cwd` and
decides what to open. This module only builds the argv, feeds the prompt on
stdin, and parses the final message + a session reference (for `rebut`).

`build_argv` / `build_resume_argv` / `parse_output` are pure and unit-tested.
The impure subprocess call is injected via `runner` (see runner.py).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .runner import RunResult, run_capture

Runner = Callable[[list[str], str, Path, int], RunResult]


@dataclass(frozen=True)
class Review:
    """The reviewer's final message plus a token to resume the same session."""

    text: str
    session_ref: str | None
    raw: str


class Engine(ABC):
    name: str
    default_model: str

    @abstractmethod
    def build_argv(self, cwd: Path, model: str, effort: str, web_search: bool) -> list[str]:
        ...

    @abstractmethod
    def build_resume_argv(
        self, session_ref: str, cwd: Path, model: str, effort: str, web_search: bool
    ) -> list[str]:
        ...

    @abstractmethod
    def parse_output(self, stdout: str) -> Review:
        ...

    def run(
        self,
        prompt: str,
        cwd: Path,
        model: str,
        effort: str,
        web_search: bool,
        runner: Runner = run_capture,
        timeout: int | None = None,
    ) -> Review:
        argv = self.build_argv(cwd, model, effort, web_search)
        return self._execute(argv, prompt, cwd, runner, timeout)

    def resume(
        self,
        session_ref: str,
        prompt: str,
        cwd: Path,
        model: str,
        effort: str,
        web_search: bool,
        runner: Runner = run_capture,
        timeout: int | None = None,
    ) -> Review:
        argv = self.build_resume_argv(session_ref, cwd, model, effort, web_search)
        return self._execute(argv, prompt, cwd, runner, timeout)

    def _execute(
        self, argv: list[str], prompt: str, cwd: Path, runner: Runner, timeout: int | None
    ) -> Review:
        from .runner import DEFAULT_TIMEOUT_SEC

        result = runner(argv, prompt, cwd, timeout or DEFAULT_TIMEOUT_SEC)
        if result.returncode != 0 and not result.stdout.strip():
            return Review(
                text=(
                    f"[paranoia-local error] {self.name} exited {result.returncode}: "
                    f"{result.stderr.strip()[:2000]}"
                ),
                session_ref=None,
                raw=result.stderr,
            )
        return self.parse_output(result.stdout)


class CodexEngine(Engine):
    name = "codex"
    default_model = "gpt-5.6-sol"

    def build_argv(self, cwd: Path, model: str, effort: str, web_search: bool) -> list[str]:
        argv = [
            "codex", "exec",
            "--json",
            "--skip-git-repo-check",
            "-s", "read-only",
            "-C", str(cwd),
            "-m", model,
            "-c", f'model_reasoning_effort="{effort}"',
        ]
        if web_search:
            argv += ["-c", "tools.web_search=true"]
        argv.append("-")  # read prompt from stdin
        return argv

    def build_resume_argv(
        self, session_ref: str, cwd: Path, model: str, effort: str, web_search: bool
    ) -> list[str]:
        # `codex exec resume` does NOT accept -s/-C: a resumed session inherits
        # its original sandbox (read-only) and cwd. We pass -C nowhere and rely
        # on the process cwd (set by the runner). --skip-git-repo-check keeps it
        # working even if the original cwd (an isolated worktree) is gone.
        argv = [
            "codex", "exec", "resume", session_ref,
            "--json",
            "--skip-git-repo-check",
            "-m", model,
            "-c", f'model_reasoning_effort="{effort}"',
        ]
        if web_search:
            argv += ["-c", "tools.web_search=true"]
        argv.append("-")
        return argv

    def parse_output(self, stdout: str) -> Review:
        thread_id: str | None = None
        last_message: str | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id") or thread_id
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    last_message = text
        return Review(text=last_message or "", session_ref=thread_id, raw=stdout)


# Read-only tool allowlist for the Claude engine. In `-p` mode a tool that
# needs permission and isn't allowlisted is auto-denied (no human to prompt),
# so this is the reviewer's whole capability surface.
CLAUDE_RO_TOOLS = [
    "Read", "Grep", "Glob", "LS", "NotebookRead", "TodoWrite",
    "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)",
    "Bash(git status:*)", "Bash(git blame:*)", "Bash(git ls-files:*)",
    "Bash(git rev-parse:*)", "Bash(git cat-file:*)", "Bash(git shortlog:*)",
]
CLAUDE_WEB_TOOLS = ["WebSearch", "WebFetch"]
CLAUDE_DENY_TOOLS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]


class ClaudeEngine(Engine):
    name = "claude"
    default_model = "claude-fable-5"

    def _allowed(self, web_search: bool) -> str:
        tools = list(CLAUDE_RO_TOOLS)
        if web_search:
            tools += CLAUDE_WEB_TOOLS
        return ",".join(tools)

    def build_argv(self, cwd: Path, model: str, effort: str, web_search: bool) -> list[str]:
        return [
            "claude", "-p",
            "--output-format", "json",
            "--model", model,
            "--effort", effort,
            "--permission-mode", "default",
            # Hermetic read-only: load NO settings files, so the reviewed repo's
            # (or the user's global) .claude allow-lists cannot widen the
            # reviewer beyond paranoia's --allowedTools. This is a flag on the
            # spawned subprocess only — it does not affect any other `claude`.
            "--setting-sources", "",
            "--allowedTools", self._allowed(web_search),
            "--disallowedTools", ",".join(CLAUDE_DENY_TOOLS),
        ]

    def build_resume_argv(
        self, session_ref: str, cwd: Path, model: str, effort: str, web_search: bool
    ) -> list[str]:
        return [
            "claude", "-p",
            "--resume", session_ref,
            "--output-format", "json",
            "--model", model,
            "--effort", effort,
            "--permission-mode", "default",
            "--setting-sources", "",
            "--allowedTools", self._allowed(web_search),
            "--disallowedTools", ",".join(CLAUDE_DENY_TOOLS),
        ]

    def parse_output(self, stdout: str) -> Review:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return Review(text=stdout.strip(), session_ref=None, raw=stdout)
        if not isinstance(data, dict):
            return Review(text=stdout.strip(), session_ref=None, raw=stdout)
        return Review(
            text=str(data.get("result", "")),
            session_ref=data.get("session_id"),
            raw=stdout,
        )


_ENGINES: dict[str, type[Engine]] = {
    "codex": CodexEngine,
    "claude": ClaudeEngine,
}


def get_engine(name: str) -> Engine:
    try:
        return _ENGINES[name]()
    except KeyError:
        raise ValueError(
            f"unknown engine {name!r}; choose one of {sorted(_ENGINES)}"
        ) from None
