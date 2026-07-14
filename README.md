# paranoia-local

A local MCP server that gets a **cold, adversarial second opinion** on your code
changes and plans — from the *other* frontier coding agent, running on its own
subscription, with **full read access to your repository**.

Install it into Claude Code and reviews are performed by Codex (GPT‑5.6). Install
it into Codex and reviews are performed by Claude Code (Fable 5). The MCP server
is just the channel between them.

```
┌──────────────┐   "paranoia: critique this branch"   ┌──────────────┐
│  Claude Code │ ───────────────────────────────────► │ paranoia-local│
│  (your work) │                                       │   (MCP, local)│
└──────────────┘                                       └──────┬───────┘
                                                              │ codex exec (read-only, your repo)
                                                       ┌──────▼───────┐
                                                       │ Codex / GPT-5.6│  ← reads the whole repo,
                                                       │  cold reviewer │    decides what to open
                                                       └──────────────┘
```

## Why

Claude (or Codex) can review its own work, but it reviews with the same biases it
wrote with. A *different* model with a *fresh* context is a genuine second opinion.
This tool packages that into one MCP call.

Compared to an API-key reviewer that only sees a hand-assembled payload, a local
agent reviewer is:

- **More powerful** — it has the *entire* repository and git history and decides
  what to read. It opens call-sites, follows the blast radius, checks tests and
  configs, and reads git history — the things a diff-only reviewer can't.
- **Cheaper** — it runs on your existing ChatGPT / Claude **subscription**, not
  metered API tokens.
- **Safer** — the reviewer runs read-only (OS sandbox for Codex; a read-only tool
  allowlist for Claude) inside a throwaway git worktree, so it can't touch your
  work.

## Tools

| Tool | What it does |
|---|---|
| `critique_branch` | Adversarial review of a git branch/diff or the dirty working tree. Five-section critique (What works / doesn't / Risks / Gaps / Improvements) with `[BLOCKER]`/`[MAJOR]`/`[MINOR]`/`[OUT-OF-SCOPE]` tags. |
| `critique_plan` | Adversarial review of a plan or design doc. With `repo_path`, the reviewer reads the real code to test the plan's premises about current behaviour — a plan built on an inverted premise is the most dangerous kind. |
| `query` | A quick double-check of a single fact or point. Not a full review — lower reasoning effort, a direct answer with citations and a stated confidence level. |
| `rebut` | Dispute a specific finding. Resumes the **same** reviewer session with your counter-evidence; it concedes or holds with fresh citations. Cheaper and higher-resolution than a cold re-round. |

Every review returns a `session_ref` in its footer — pass it to `rebut`.

### Convergence loop

`critique_branch` and `critique_plan` take an `already_raised` array: one-line,
`file:line`-cited claims already accepted from prior rounds. The reviewer is told
not to restate them and to hunt for what they missed. Drive the loop from the
caller — spawn a fresh review each round feeding the growing `already_raised`
list — until findings converge or drop to noise. (Never paste prior reviewers'
prose; just the deduplicated claim + citation.)

## Install

### Prerequisites

- Python 3.11+
- `git` on `PATH`
- The reviewing agent's CLI installed and signed in on a subscription:
  - [Codex CLI](https://developers.openai.com/codex) (`codex`, ≥ 0.144) signed in
    with a ChatGPT plan, **or**
  - [Claude Code](https://code.claude.com) (`claude`) signed in with a Claude plan.

### Install the server

```bash
git clone https://github.com/subvertnormality/paranoia-local
cd paranoia-local
pip install -e .
```

### Wire into Claude Code (reviews performed by Codex)

```bash
claude mcp add paranoia -- paranoia-local --engine codex
```

Add to your `~/.claude/CLAUDE.md` so it's only used on request:

```
Never call the paranoia MCP server unless I explicitly ask for adversarial review,
critique, or a second opinion.
```

### Wire into Codex (reviews performed by Claude Code)

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.paranoia]
command = "paranoia-local"
args = ["--engine", "claude"]
# REQUIRED: an agentic review takes minutes. Codex's default MCP tool timeout is
# 60s, which will abort every review. Give it an hour.
tool_timeout_sec = 3600
startup_timeout_sec = 30
```

> **Timeout gotcha.** A full review is many turns of tool use and can run for
> several minutes. Claude Code's stdio MCP idle timeout (~30 min) is fine out of
> the box; **Codex defaults to a 60-second tool timeout** and must be raised as
> shown above or every review fails.

## Usage

In Claude Code:

> "Use paranoia to critique this branch against main. Intent: add overdraft
> protection to `withdraw()`."

The agent calls:

```json
{
  "name": "critique_branch",
  "arguments": {
    "repo_path": "/Users/you/Work/my-project",
    "base_ref": "main",
    "head_ref": "HEAD",
    "diff_intent": "Add overdraft protection to withdraw()."
  }
}
```

## Per-repo defaults — `.paranoia.toml`

Drop a `.paranoia.toml` at the repo root so callers stop retyping context. Keys at
the top level or under `[paranoia]`. Precedence: **call arg > `.paranoia.toml` >
built-in default**.

```toml
project_summary = "A booking API. Python/FastAPI, Postgres. Auth via short-lived JWTs."
base_ref = "develop"
web_search = true      # allow external methodology/library cross-checks
isolate = true         # review inside a throwaway worktree
# model / effort overrides also honoured
```

## Common arguments

All tools accept:

- `engine` — override which engine reviews for this one call (`codex` | `claude`).
- `model` — override the reviewer model (defaults to the engine's strongest:
  `gpt-5.6-sol` / `claude-fable-5`).
- `effort` — `low` | `medium` | `high`. Reviews default to `high`; `query`
  defaults to `medium`.
- `web_search` — allow the reviewer to cross-check external methodology/library
  claims on the web (default `true`).

## Safety model

- **Read-only.** Codex runs under its OS sandbox (`--sandbox read-only`); Claude
  runs with a read-only tool allowlist (`Read`, `Grep`, `Glob`, scoped `git`
  reads, web search) and write tools explicitly denied. The reviewer cannot edit
  your code, run your test suite, or reach the network except for opt-in web
  search.
- **Isolated.** Committed reviews run inside a throwaway `git worktree` of the
  target ref, so they never collide with your working tree and can review a
  branch that isn't checked out. (Dirty-working-tree reviews necessarily run in
  the live repo, read-only.)
- **No API keys, no telemetry, no state.** The server shells out to a CLI you're
  already signed into. It writes a local audit record per review to
  `~/.paranoia/logs/` (provenance + the session ref for `rebut`) and nothing else.

## Rate limits

Reviews draw on your subscription's agentic-usage pool. A heavy convergence loop
is many agent turns — on smaller plans you can hit the 5-hour window. Use `query`
(lower effort) for quick checks, and reserve full multi-round `critique_branch`
loops for changes that warrant them.

## Development

```bash
pip install -e '.[dev]'
python -m pytest        # unit + integration (integration uses fake CLIs; no quota)
```

Every module is TDD'd. The engine subprocess boundary is dependency-injected, so
the whole stack is unit-tested without spending subscription quota; a separate
integration test drives the real subprocess runner against fake `codex`/`claude`
binaries on `PATH`.

## License

MIT © 2026 Andrew Hillel
