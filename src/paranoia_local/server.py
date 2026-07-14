"""MCP stdio server. Exposes four tools that drive a local coding-agent CLI
(Codex or Claude Code) as an adversarial reviewer with full repo access.

The heavy lifting lives in `handlers`; this module is the MCP glue plus a
`dispatch` router that resolves the engine (per-call override > server default)
and turns any handler failure into readable error text instead of a protocol
error.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import handlers
from .engines import get_engine
from .logs import DEFAULT_LOG_DIR

Clock = Callable[[], str]

# Shared arg fragments reused across tool schemas.
_COMMON = {
    "engine": {
        "type": "string",
        "enum": ["codex", "claude"],
        "description": "Override which local engine reviews (default: the server's configured engine).",
    },
    "model": {
        "type": "string",
        "description": "Override the reviewer model (default: the engine's strongest — gpt-5.6-sol / claude-fable-5).",
    },
    "effort": {
        "type": "string",
        "enum": ["low", "medium", "high"],
        "description": "Reasoning effort. Reviews default to high; query defaults to medium.",
    },
    "web_search": {
        "type": "boolean",
        "description": "Allow the reviewer to cross-check external methodology/library claims on the web (default true).",
    },
}

_ALREADY_RAISED = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Convergence loop: one-line, file:line-cited claims already accepted from prior rounds. "
        "The reviewer is told NOT to restate these and to hunt for what they missed. Never paste prior reviewers' prose."
    ),
}

TOOLS: list[Tool] = [
    Tool(
        name="critique_branch",
        description=(
            "Adversarially review a git branch/diff. A cold, strongest-frontier reviewer on the "
            "OTHER engine reads the repo directly (full read access, its own subscription) and returns "
            "a five-section critique with severity tags. Reviews an isolated worktree of the ref by "
            "default; can review the dirty working tree with include_uncommitted."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Absolute path to the git repo."},
                "base_ref": {"type": "string", "description": "Base ref for the diff (default main / .paranoia.toml)."},
                "head_ref": {"type": "string", "description": "Head ref (default HEAD)."},
                "include_uncommitted": {
                    "type": "boolean",
                    "description": "Review the dirty working tree vs HEAD instead of a committed range (runs in the live repo, not a worktree).",
                },
                "isolate": {
                    "type": "boolean",
                    "description": "Review inside a throwaway worktree of head_ref so it can't collide with ongoing work (default true; ignored for uncommitted).",
                },
                "project_summary": {"type": "string", "description": "Neutral factual description of the project (not advocacy). The reviewer tests the diff against it."},
                "diff_intent": {"type": "string", "description": "What the diff is SUPPOSED to achieve — treated as a claim to verify."},
                "focus": {"type": "string", "description": "Narrow the review to a specific concern."},
                "already_raised": _ALREADY_RAISED,
                **_COMMON,
            },
            "required": ["repo_path"],
        },
    ),
    Tool(
        name="critique_plan",
        description=(
            "Adversarially review a plan or design doc. With repo_path the reviewer reads the actual "
            "code to test the plan's premises about current behaviour — a plan built on an inverted "
            "premise is the most dangerous kind. Returns the five-section critique with FATAL/MAJOR/MINOR tags."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "plan_text": {"type": "string", "description": "The plan as markdown. Provide this OR plan_path."},
                "plan_path": {"type": "string", "description": "Absolute path to a markdown plan file. Provide this OR plan_text."},
                "context": {"type": "string", "description": "Background the reviewer needs to judge the plan fairly."},
                "repo_path": {"type": "string", "description": "Repo the plan concerns — enables grounding the critique in real code (strongly recommended)."},
                "focus": {"type": "string", "description": "Narrow the review to a specific concern."},
                "already_raised": _ALREADY_RAISED,
                **_COMMON,
            },
        },
    ),
    Tool(
        name="query",
        description=(
            "Quick adversarial double-check of a single fact or point — NOT a full review, no five-section "
            "scaffold, lower reasoning effort. The reviewer reads the repo (if given) and returns a direct "
            "answer with citations and a stated confidence level."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The specific question to double-check."},
                "repo_path": {"type": "string", "description": "Absolute path to the git repo to ground the answer (optional)."},
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "reason": {"type": "string"}},
                        "required": ["path"],
                    },
                    "description": "Optional files to suggest the reviewer look at first (hints, not a payload — it can read anything).",
                },
                "focus": {"type": "string", "description": "Extra framing for the question."},
                **_COMMON,
            },
            "required": ["question"],
        },
    ),
    Tool(
        name="rebut",
        description=(
            "Dispute a specific finding from a prior review. Resumes the SAME reviewer session (cheaper and "
            "higher-resolution than a cold re-round) with your counter-evidence; it concedes or holds with fresh citations."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Absolute path to the git repo (same one the review ran against)."},
                "session_ref": {"type": "string", "description": "The session_ref printed in the prior review's footer."},
                "rebuttal": {"type": "string", "description": "Your counter-evidence for the disputed finding."},
                **_COMMON,
            },
            "required": ["repo_path", "session_ref", "rebuttal"],
        },
    ),
]

_HANDLERS: dict[str, Callable[..., str]] = {
    "critique_branch": handlers.critique_branch,
    "critique_plan": handlers.critique_plan,
    "query": handlers.query,
    "rebut": handlers.rebut,
}


def dispatch(
    name: str,
    arguments: dict[str, Any],
    *,
    default_engine_name: str,
    log_dir: Path = DEFAULT_LOG_DIR,
    now: Clock | None = None,
) -> str:
    try:
        handler = _HANDLERS.get(name)
        if handler is None:
            raise ValueError(f"unknown tool: {name}")
        engine_name = arguments.get("engine") or default_engine_name
        engine = get_engine(engine_name)
        kwargs: dict[str, Any] = {"engine": engine, "log_dir": log_dir}
        if now is not None:
            kwargs["now"] = now
        return handler(arguments, **kwargs)
    except Exception as exc:  # noqa: BLE001 — surface any failure as readable text
        return f"[paranoia-local error] {type(exc).__name__}: {exc}"


def build_server(
    default_engine_name: str,
    log_dir: Path = DEFAULT_LOG_DIR,
    now: Clock | None = None,
) -> Server:
    srv: Server = Server("paranoia-local")

    @srv.list_tools()
    async def _list_tools() -> list[Tool]:
        return TOOLS

    @srv.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        result = await asyncio.to_thread(
            dispatch, name, arguments,
            default_engine_name=default_engine_name, log_dir=log_dir, now=now,
        )
        return [TextContent(type="text", text=result)]

    return srv


async def run_stdio(server_obj: Server) -> None:
    async with stdio_server() as (read, write):
        await server_obj.run(read, write, server_obj.create_initialization_options())
