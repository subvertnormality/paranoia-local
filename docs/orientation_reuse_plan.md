# Plan: deterministic orientation packet (Phase 1 — the shipped scope)

Status: FINAL — scope decided by the owner after 5 adversarial-review rounds.
**The Claude fork (former Phase 2) is dropped.** The review established that a fork's
review effectiveness cannot be guaranteed by construction (a deterministic manifest
cannot be a true superset — rename-only deps, beyond-cap callers), only measured, and its
correct implementation (registry, PID/heartbeat lease, stable-path rematerialization,
change-sets, cross-worktree session resume) carried every FATAL found. Phase 1 captures
the bulk of the measured saving with none of that surface. This doc is now Phase 1 only.

## 1. Problem (measured)

Convergence loops spawn a cold reviewer every round (`handlers.py:113-119`; only `rebut`
resumes). The dominant waste is the reviewer's **multi-turn re-gathering**: on stereogram,
one file was re-read **18×** in a 3-round loop, one doc **41×** in a 20-round loop, with
**23–38 git/grep calls per round** — repeated every round. The reasoning is not
duplicated. Most of the saving comes from eliminating those gather *turns*, which a
server-prebuilt evidence packet + a packet-aware prompt can do **statelessly, per
request, for both engines**.

## 2. Design — stateless, per request

No handles, sessions, registry, leases, or cross-round state. Each `converge:true` call:

1. **Resolve once, snapshot immutably.** `H = git rev-parse HEAD`. Dirty tree: build the
   snapshot **from `H`** (not the symbol `HEAD`, so a concurrent HEAD move can't corrupt
   it) — `GIT_INDEX_FILE=<tmp> git read-tree H → git add -A → git write-tree` → `<tree>`
   (captures tracked incl. tracked-but-ignored + untracked; verified) — then wrap it:
   `git commit-tree <tree> -p H -m paranoia-snapshot` with **`-m` (never bare — `commit-tree`
   reads its message from stdin, which is the MCP transport)** and identity+dates pinned
   via `GIT_*_NAME/EMAIL/DATE` env. Committed ranges use the resolved commits directly.
2. **Unreferenced objects (as built — repo ODB, not a temp ODB).** The snapshot tree and
   wrapper commit are written to the repo's own object store but **no ref is created**, so
   they are unreferenced objects that `git gc` reclaims. This was chosen over a temporary
   ODB after verifying that a temp ODB requires threading `GIT_ALTERNATE_OBJECT_DIRECTORIES`
   into the *reviewer* subprocess (else its git sees `bad object HEAD` in the worktree) —
   an unverifiable dependency on the CLI propagating env to its git children. Trade-off:
   a clean exit leaves nothing; a hard crash can leave the worktree registration + its
   objects until `git worktree prune`/`gc`. The author's working tree and index are never
   touched.
3. **Materialize a separate checkout.** `git worktree add --detach <path> <wrapper-commit>`
   (reused `worktree.py::worktree_at`). No `chmod` — the reviewer is already read-only
   (Write/Edit denied by the allowlist; Codex OS-sandboxed), and the worktree is a *separate*
   checkout, so live-repo edits during the review can't perturb it. Consistent point-in-time
   evidence without the `chmod` teardown hazards.
4. **Revision-stable git.** Every git command the server renders **and every command shown
   to the reviewer** uses resolved ids / `<base_id>..<head_id>` (two-dot endpoint comparison,
   used consistently for the file list, diffstat, diff, and labels) — never `main...HEAD` or
   `git diff HEAD`. Inside the worktree that range reproduces exactly the dirty patch.
5. **Deterministic file evidence (as built).** Phase 1 embeds the touched files themselves:
   `changed_files` (`--name-status -z -M`, so deletions/renames are typed) + each file's
   current content in the snapshot. Binary / non-UTF-8 / submodule-gitlink / unreadable
   entries are represented by a marker, never raw bytes. This is the high-value, fully
   deterministic core; a symbol/call-site *manifest* (the weaker, non-superset part) is
   **deferred** — the reviewer retains full repo read access to grep call-sites itself.
6. **Budgeted packet** (`build_packet`): file evidence under a `MAX_PACKET_CHARS` evidence
   budget (separators + truncation marker accounted for). The header + `already_raised` are
   **mandatory and always included** (the firewall list is never dropped); only file evidence
   is trimmed, with an `=== EVIDENCE TRUNCATED ===` marker.
7. **Packet-aware review prompt.** Inert unless the instructions change:
   `CODE_REVIEW_INSTRUCTIONS` currently orders "read every touched file… follow the blast
   radius… read history" (`prompts.py:43-47`). Add a variant — *"the files, call-sites,
   and history below were gathered for you; verify and go deeper only where warranted; do
   not re-gather what is already provided"* — so the reviewer skips the routine gather
   turns. This is the mechanism that produces the saving.
8. **Instrumentation + fallback.** `Review` gains `returncode`, `error`,
   `usage`/`duration` from engine JSON; `_execute` stops discarding non-zero
   (`engines.py:113-122`); `parse_output` reads Claude `is_error`/`subtype` + Codex error.
   Logs get a **unique filename suffix** (`logs.py:27` collides at second precision).
9. **Opt-in & compat.** Activates on `converge:true` (requires `repo_path`; else error);
   omitted ⇒ byte-for-byte today's single `engine.run`. `converge:true` **overrides
   `isolate=false`** (`handlers.py:98,113`) — materialization is mandatory.

The packet is facts only (no findings); `already_raised` remains the caller-driven
convergence channel exactly as today.

**Known Phase-1 limitations (accepted, documented):** a touched **symlink** is embedded
as its target string without being flagged as a symlink; `ChangeEntry` carries no git
object mode, so a reviewer could follow a link to live/external bytes rather than the
snapshot. Low risk (the reviewer is read-only and the worktree is a private checkout) —
marking symlink modes is deferred. Worktree teardown is **best-effort** (return codes
not checked); a teardown failure can leave a registration until `git worktree prune`.

## 3. Implementation (TDD; pure core, injected impure edge)

RED→GREEN→refactor, then a mutation pass on the new pure logic per repo convention.

- `orientation.py`: `resolve_head`, `snapshot_tree` (dirty via temp-ODB read-tree/add/
  write-tree; committed via rev-parse), `wrap_commit` (`-m`, pinned identity/dates, temp
  ODB), `build_manifest` (pure, deterministic), `build_packet` (reserve `already_raised`,
  `MAX_PACKET_CHARS`), and resolved-id rendering for every git command. Pure functions
  over an injected `_run`.
- `worktree.py`: read-only materialization of a wrapper commit (`chmod -R a-w` after
  checkout), temp-ODB env plumbing, always-clean teardown.
- `engines.py`: `Review.returncode/error/usage/duration`; error+usage parsing for both
  engines; `_execute` preserves status.
- `prompts.py`: packet-aware `CODE_REVIEW_INSTRUCTIONS` variant.
- `handlers.py`: `converge` branch (snapshot → materialize → packet → packet-aware
  review in the worktree → teardown), `converge`-requires-`repo_path`,
  `converge`-overrides-`isolate`, instrumentation into the log record.
- `logs.py`: unique filename suffix.
- `config.py`: `MAX_PACKET_CHARS`, manifest call-site cap.
- `server.py`: `converge` input on `critique_branch`; doc the packet behaviour.
- README: correct the "audit logs and nothing else" line if the temp-ODB/worktree
  lifecycle warrants a note (it leaves nothing persistent, so likely a one-line clarify).

## 4. Tests

Snapshot: tracked / untracked / tracked-but-ignored / deleted / renamed / empty; **git
identity unset** (recipe needs none); a HEAD move between `rev-parse` and `read-tree` does
not corrupt the wrapper range. `commit-tree` never blocks on or consumes MCP stdin (uses
`-m`). Materialization: review reads current snapshot bytes; live-repo edits during a
review don't change what the reviewer sees; worktree is read-only; **never leaks a
worktree** (`git worktree list` clean); temp ODB leaves no objects in the reviewed repo,
even on simulated crash. Manifest: deterministic (same tree ⇒ byte-identical manifest);
symbol/call-site/doc enumeration + cap. Packet: `MAX_PACKET_CHARS` truncates evidence but
**never `already_raised`**. Reviewer-shown git commands use resolved ids, not `HEAD`.
Compat: omitted `converge` ⇒ identical argv, single `engine.run`; `converge` without
`repo_path` ⇒ error; `converge` overrides `isolate=false`. Failure: non-zero + `is_error`
surface as errors; usage/duration persisted; sub-second same-tool logs don't collide.
Packet is facts only (no severity/evaluation vocabulary).

## 5. Acceptance

Full suite green; §4 tests; mutation pass on `snapshot_tree`, `build_manifest`,
`build_packet`, resolved-id rendering. **Performance gate (the reason to ship):** a paired
before/after benchmark — packet-aware prompt vs today's — on a real convergence loop must
show the reviewer's gather turns (file reads + git/grep calls) actually drop and
wall-clock/tokens fall, with review quality not worse (`already_raised` still respected,
no new false negatives on a spot-check). Until that benchmark passes, the saving is a
hypothesis, not a shipped win.
