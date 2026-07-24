"""Tool dispatch logic, separated from MCP wiring so it is unit-testable with
an injected fake engine and clock.

Each handler: resolve inputs (call arg > `.paranoia.toml` > default), build the
task body, compose it with the adversarial instructions, run the reviewer in
the right working directory (an isolated worktree for committed reviews, the
live repo for dirty ones), write an audit record, and return the review with a
footer exposing the session reference for `rebut`.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import logs, orientation, prompts
from .config import load_repo_config, resolve
from .engines import Engine, Review
from .worktree import worktree_at

Clock = Callable[[], str]


def _default_clock() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _require_repo(arguments: dict[str, Any]) -> Path:
    rp = arguments.get("repo_path")
    if not rp:
        raise ValueError("repo_path is required")
    repo = Path(rp).resolve()
    if not repo.exists():
        raise ValueError(f"repo_path does not exist: {repo}")
    if not (repo / ".git").exists():
        raise ValueError(f"not a git repo (no .git): {repo}")
    return repo


def _no_repo_cwd() -> Path:
    return Path(tempfile.gettempdir())


def _footer(review: Review, engine: Engine) -> str:
    if review.session_ref:
        note = (
            f"\n\n---\n_paranoia-local · engine={engine.name} · "
            f"session_ref=`{review.session_ref}` — to dispute a finding, call `rebut` "
            f"with this session_ref and your counter-evidence._"
        )
    else:
        note = f"\n\n---\n_paranoia-local · engine={engine.name}_"
    # Surface a failed run explicitly — a non-zero exit or an in-band engine error means
    # the text below may be an error message or a truncated/aborted review, not a verdict.
    prefix = ""
    if review.error:
        prefix = (
            f"⚠️ REVIEW FAILED (engine={engine.name}, exit={review.returncode}) — treat the "
            f"output below as an error, not a completed review.\n\n"
        )
    return prefix + (review.text or "[empty review]") + note


def _progress_kwargs(on_progress: Callable[[str], None] | None) -> dict[str, Any]:
    """Pass on_progress only when set — injected engines may predate the kwarg."""
    return {"on_progress": on_progress} if on_progress is not None else {}


def _calibration(stakes: str | None, review_round: int | None) -> str | None:
    """Render the reviewer-calibration block. STAKES bounds legitimate concern
    (findings beyond it are out of scope); ROUND sets the severity floor across a
    convergence loop (round >=3 reports MAJOR-or-higher only, withholding MINOR
    and OUT-OF-SCOPE). Both optional; absent → the reviewer assumes a modest
    internal tool and reports everything."""
    lines: list[str] = []
    if stakes:
        lines.append(f"STAKES: {stakes}")
    if review_round is not None and review_round >= 1:
        lines.append(f"ROUND: {review_round}")
    if not lines:
        return None
    return "=== REVIEW CALIBRATION ===\n" + "\n".join(lines)


def _prepend(block: str | None, body: str) -> str:
    return f"{block}\n\n{body}" if block else body


def _log(
    log_dir: Path,
    tool: str,
    engine: Engine,
    review: Review,
    now: Clock,
    extra: dict[str, Any],
) -> None:
    logs.write_log(
        log_dir,
        tool=tool,
        record={
            "engine": engine.name,
            "session_ref": review.session_ref,
            "returncode": review.returncode,
            "error": review.error,
            "text": review.text,
            **extra,
        },
        timestamp=now(),
    )


def critique_branch(
    arguments: dict[str, Any],
    *,
    engine: Engine,
    log_dir: Path = logs.DEFAULT_LOG_DIR,
    now: Clock = _default_clock,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    repo = _require_repo(arguments)
    cfg = load_repo_config(repo)

    base_ref = resolve("base_ref", arguments.get("base_ref"), cfg, "main")
    head_ref = arguments.get("head_ref", "HEAD")
    include_unc = bool(arguments.get("include_uncommitted", False))
    isolate = bool(resolve("isolate", arguments.get("isolate"), cfg, True))
    project_summary = resolve("project_summary", arguments.get("project_summary"), cfg, None)
    diff_intent = arguments.get("diff_intent")
    focus = arguments.get("focus")
    already = list(arguments.get("already_raised", []))
    model = resolve("model", arguments.get("model"), cfg, engine.default_model)
    effort = resolve("effort", arguments.get("effort"), cfg, "high")
    web_search = bool(resolve("web_search", arguments.get("web_search"), cfg, True))
    # Converge (packet) mode is ON by default: pre-gather a deterministic evidence packet
    # and review it against an immutable materialized snapshot. Pass converge=false (call
    # arg or .paranoia.toml) to fall back to the legacy in-place review.
    converge = bool(resolve("converge", arguments.get("converge"), cfg, True))
    max_packet_chars = int(
        resolve("max_packet_chars", arguments.get("max_packet_chars"), cfg, orientation.MAX_PACKET_CHARS)
    )
    # Calibration: STAKES (project-level, so also honoured from .paranoia.toml) bounds
    # scope; ROUND (per-call, raised each convergence round) sets the severity floor.
    calibration = _calibration(
        resolve("stakes", arguments.get("stakes"), cfg, None), arguments.get("round")
    )

    target = orientation.resolve_target(repo, base_ref, head_ref, include_unc)

    if converge:
        return _converge_branch_review(
            repo, engine, target=target, base_ref=base_ref, head_ref=head_ref,
            project_summary=project_summary, diff_intent=diff_intent, focus=focus,
            already=already, model=model, effort=effort, web_search=web_search,
            max_packet_chars=max_packet_chars, calibration=calibration,
            log_dir=log_dir, now=now, on_progress=on_progress,
        )

    packet = orientation.build_orientation(
        repo, target, project_summary, diff_intent, focus, already
    )
    prompt = prompts.compose(prompts.CODE_REVIEW_INSTRUCTIONS, _prepend(calibration, packet))

    if target.is_dirty or not isolate:
        review = engine.run(prompt, repo, model, effort, web_search,
                            **_progress_kwargs(on_progress))
    else:
        with worktree_at(repo, head_ref) as wt:
            review = engine.run(prompt, wt, model, effort, web_search,
                                **_progress_kwargs(on_progress))

    _log(log_dir, "critique_branch", engine, review, now,
         {"target": target.description, "model": model})
    return _footer(review, engine)


def _converge_branch_review(
    repo: Path,
    engine: Engine,
    *,
    target: orientation.Target,
    base_ref: str,
    head_ref: str | None,
    project_summary: str | None,
    diff_intent: str | None,
    focus: str | None,
    already: list[str],
    model: str,
    effort: str,
    web_search: bool,
    max_packet_chars: int,
    calibration: str | None,
    log_dir: Path,
    now: Clock,
    on_progress: Callable[[str], None] | None,
) -> str:
    """Opt-in convergence path: pre-gather a deterministic packet so the reviewer skips
    the re-read/re-grep turns, and review it against an IMMUTABLE materialized worktree
    (which always applies here, overriding isolate=false — mixed-revision evidence off a
    live mutable tree is exactly what this prevents)."""
    if target.is_dirty:
        if orientation.has_head(repo):
            base_id = orientation.resolve_head(repo)
            parent: str | None = base_id
        else:
            # Unborn repo (files, no commit yet): base off git's empty tree, parentless wrapper.
            base_id = orientation.empty_tree(repo)
            parent = None
        head_id = orientation.wrap_commit(repo, orientation.snapshot_tree(repo, base_id), parent)
    else:
        base_id = orientation.resolve_ref(repo, base_ref)
        head_id = orientation.resolve_ref(repo, head_ref or "HEAD")

    packet = orientation.build_packet(
        repo, base_id, head_id,
        project_summary=project_summary, diff_intent=diff_intent, focus=focus,
        already_raised=already, max_chars=max_packet_chars,
    )
    prompt = prompts.compose(prompts.CODE_REVIEW_INSTRUCTIONS_PACKET, _prepend(calibration, packet))

    with worktree_at(repo, head_id) as wt:
        review = engine.run(prompt, wt, model, effort, web_search,
                            **_progress_kwargs(on_progress))

    _log(log_dir, "critique_branch", engine, review, now,
         {"target": target.description, "model": model, "mode": "converge-packet",
          "usage": review.usage, "duration_ms": review.duration_ms})
    return _footer(review, engine)


def _plan_body(
    plan_text: str,
    context: str | None,
    focus: str | None,
    already: list[str],
    repo_grounded: bool,
) -> str:
    parts: list[str] = []
    if repo_grounded:
        parts.append(
            "=== REPOSITORY IS AVAILABLE ===\n"
            "You are inside the repository this plan concerns. Read the code to test "
            "every premise the plan makes about current behaviour."
        )
    if context:
        parts.append(f"=== CONTEXT ===\n{context}")
    if focus:
        parts.append(f"=== REVIEWER FOCUS ===\n{focus}")
    parts.append(f"=== PLAN ===\n{plan_text}")
    if already:
        rendered = "\n".join(f"- {c}" for c in already)
        parts.append(
            "=== Already-raised — do NOT restate; hunt for what they missed ===\n" + rendered
        )
    return "\n\n".join(parts)


def critique_plan(
    arguments: dict[str, Any],
    *,
    engine: Engine,
    log_dir: Path = logs.DEFAULT_LOG_DIR,
    now: Clock = _default_clock,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    plan_text = arguments.get("plan_text")
    plan_path = arguments.get("plan_path")
    if plan_text and plan_path:
        raise ValueError("critique_plan takes plan_text OR plan_path, not both")
    if not plan_text and not plan_path:
        raise ValueError("critique_plan requires plan_text or plan_path")
    if plan_path:
        try:
            plan_text = Path(plan_path).read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as exc:
            raise ValueError(f"cannot read plan_path: {exc}") from exc

    context = arguments.get("context")
    focus = arguments.get("focus")
    already = list(arguments.get("already_raised", []))
    repo_path = arguments.get("repo_path")
    repo = _require_repo(arguments) if repo_path else None
    cwd = repo if repo else _no_repo_cwd()
    cfg = load_repo_config(repo) if repo else {}

    model = resolve("model", arguments.get("model"), cfg, engine.default_model)
    effort = resolve("effort", arguments.get("effort"), cfg, "high")
    web_search = bool(resolve("web_search", arguments.get("web_search"), cfg, True))

    calibration = _calibration(
        resolve("stakes", arguments.get("stakes"), cfg, None), arguments.get("round")
    )
    body = _plan_body(plan_text, context, focus, already, repo_grounded=bool(repo))
    prompt = prompts.compose(prompts.PLAN_REVIEW_INSTRUCTIONS, _prepend(calibration, body))
    review = engine.run(prompt, cwd, model, effort, web_search,
                        **_progress_kwargs(on_progress))

    _log(log_dir, "critique_plan", engine, review, now, {"grounded": bool(repo), "model": model})
    return _footer(review, engine)


def _query_body(
    question: str, files: list[dict], focus: str | None, repo_grounded: bool
) -> str:
    parts: list[str] = []
    if repo_grounded:
        parts.append(
            "=== REPOSITORY IS AVAILABLE ===\n"
            "Answer by reading the actual code, data, and git history in your working "
            "directory — not from assumption."
        )
    if files:
        hints = "\n".join(
            f"- {f.get('path', '?')}" + (f" ({f['reason']})" if f.get("reason") else "")
            for f in files
        )
        parts.append(f"=== FILES THE CALLER SUGGESTS LOOKING AT ===\n{hints}")
    if focus:
        parts.append(f"=== FOCUS ===\n{focus}")
    parts.append(f"=== QUESTION ===\n{question}")
    return "\n\n".join(parts)


def query(
    arguments: dict[str, Any],
    *,
    engine: Engine,
    log_dir: Path = logs.DEFAULT_LOG_DIR,
    now: Clock = _default_clock,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    question = arguments.get("question")
    if not question:
        raise ValueError("query requires a question")

    repo_path = arguments.get("repo_path")
    repo = _require_repo(arguments) if repo_path else None
    cwd = repo if repo else _no_repo_cwd()
    cfg = load_repo_config(repo) if repo else {}
    files = list(arguments.get("files", []))
    focus = arguments.get("focus")

    model = resolve("model", arguments.get("model"), cfg, engine.default_model)
    # query is a quick double-check, not a full review — lower reasoning effort.
    effort = resolve("effort", arguments.get("effort"), cfg, "medium")
    web_search = bool(resolve("web_search", arguments.get("web_search"), cfg, True))

    body = _query_body(question, files, focus, repo_grounded=bool(repo))
    prompt = prompts.compose(prompts.QUERY_INSTRUCTIONS, body)
    review = engine.run(prompt, cwd, model, effort, web_search,
                        **_progress_kwargs(on_progress))

    _log(log_dir, "query", engine, review, now, {"model": model})
    return _footer(review, engine)


def rebut(
    arguments: dict[str, Any],
    *,
    engine: Engine,
    log_dir: Path = logs.DEFAULT_LOG_DIR,
    now: Clock = _default_clock,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    session_ref = arguments.get("session_ref")
    rebuttal = arguments.get("rebuttal")
    if not session_ref:
        raise ValueError("rebut requires session_ref (from a prior review's footer)")
    if not rebuttal:
        raise ValueError("rebut requires rebuttal (your counter-evidence)")
    repo = _require_repo(arguments)
    cfg = load_repo_config(repo)

    model = resolve("model", arguments.get("model"), cfg, engine.default_model)
    effort = resolve("effort", arguments.get("effort"), cfg, "high")
    web_search = bool(resolve("web_search", arguments.get("web_search"), cfg, True))

    body = f"=== AUTHOR'S COUNTER-EVIDENCE ===\n{rebuttal}"
    prompt = prompts.compose(prompts.REBUT_INSTRUCTIONS, body)
    review = engine.resume(session_ref, prompt, repo, model, effort, web_search,
                           **_progress_kwargs(on_progress))

    _log(log_dir, "rebut", engine, review, now, {"session_ref": session_ref, "model": model})
    return _footer(review, engine)
