@AGENTS.md

# Working with subagents

How a session works in this repo with subagents, on top of the rules in `AGENTS.md`. It applies to Claude Code, which loads this file, and to Codex, which `AGENTS.md` sends here. Codex uses the agents of the same name in `.codex/agents/`.

## Subagents in `.claude/agents/` and `.codex/agents/`

| Agent | Claude Code model | Codex model | Use it for |
|---|---|---|---|
| `scoper` | sonnet | `gpt-6-luna` | Start of a non-trivial task in code you have not read yet: goal, acceptance criteria, files, tests, possible split. Read-only. |
| `implementer` | sonnet | `gpt-6.1-sol` | Substantial, well-defined code or test changes once the plan is clear, boilerplate and new tests included. |
| `reviewer` | opus | `gpt-6-astra` | Independent review, only when asked or for the risky changes listed under Escalation. |
| `shipper` | sonnet | `gpt-6-luna` | Commit, push and open the draft PR, only when the user asked for it. |

This is a standing request to use subagents. Do not wait to be asked.

## Scope unfamiliar tasks with `scoper`

- At the start of a non-trivial task whose code you do not know yet, run `scoper` to collect the goal, acceptance criteria, relevant files, tests and a possible split. Skip it only when you expect to read 3 or fewer files you have not already read in this session.
- Treat its report as input, not as a decision. It runs on a small model, so spot-check the files it names before you plan on them. When it flags the task as complex, ambiguous or risky, read the key files yourself.
- You always write the plan. Checks scoper cannot run (`make up`, the UI click-through, anything needing Docker) stay with the main session and must not be skipped.

## Delegate implementation

- Once the task is understood and the solution is clear, hand substantial code or test changes to `implementer`: new features, bug fixes, refactors, boilerplate, tests.
- Make the edit yourself when it is small and fully known, even a few dozen lines: a handoff plus a diff review would cost about as much.
- The main session writes these itself, never a subagent: `schemas.py`, `models.py`, `db.py`, `config.py`, `auth.py`, `deps.py`, `services/agent/`, `services/legal/`, `verifier.py`, `gating.py`, and all security code.
- A brief gives the issue number, acceptance criteria, the workstream's allowed paths (from `docs/NEXT_PROMPTS.md`), the `API_CONTRACT.md` section, decisions already made, edge cases, and the validation commands (`make lint`, `make test`, plus `cd desktop && node check.js` for desktop changes). The subagent starts with no context, so do not make it rediscover what you know.
- Keep architecture, ambiguous scope and root-cause analysis in the main session. The implementer executes a defined solution; it does not redesign the task.
- Split work larger than one subsystem into file-owned slices, one subagent run each. Run slices in parallel only when their files do not overlap; never let two agents edit the same file. Start dependent slices only after the ones they depend on are done.
- Pass the "Local environment" rules from `AGENTS.md` on in every brief.

## Review after implementation

Do not take a subagent's summary or its "tests pass" as proof. Before reporting a task complete:

1. Inspect the actual `git diff` and read the important changed code.
2. Compare it with the request, the acceptance criteria and the plan.
3. Check callers, interfaces and integration points where defects could come in.
4. Check that tests cover the changed behavior, including edge and failure cases (other org's ids, month-precision dates, zero sources, rejected events).
5. Check the TenantProof rules: contract field names match exactly or the change is logged in `CONTRACT_CHANGES.md`; every by-id handler checks org ownership; every mutation calls `activity.log(...)`; `caseworker_note` never leaves the caseworker workspace; the pure modules make no LLM calls; edits stay inside the workstream's allowed paths and shared files change only in a `schema:` PR.
6. Look for wrong assumptions, missing cases, regressions, scope creep, weakened tests, and behavior that passes tests but misses the real requirement.
7. Run `make lint` and `make test` yourself (without `make`, from `backend/`: `uv run ruff check .`, `uv run pyright`, `uv run pytest -q`), plus `cd desktop && node check.js` for desktop changes and targeted tests when the diff raises doubts.

If you find a defect, send substantial fixes back to the same `implementer` (Claude Code: `SendMessage`; Codex: a follow-up in the same agent thread), spawning a new one only if it is gone, and review the new diff. Fix small things yourself.

This review is the only default review. Do not also run `reviewer` on the same change unless the user asks or the change falls under Escalation.

## Escalation

Keep implementation in the main session, or cut it into smaller defined pieces first, when it needs unresolved judgment: architecture-heavy refactors, concurrency, data migrations, state machines, broad API changes, tightly coupled subsystems, or an implementation that keeps drifting from the plan.

Run `reviewer` after these, in addition to your own review: auth, sessions and intake tokens; org isolation; Alembic migrations; upload handling and EXIF stripping; agent tools and prompt-injection handling; the Electron shell's security flags; anything listed in `SECURITY.md`.

## Ship with `shipper`

- When the user asks to commit, push or open a PR, hand the git and GitHub steps to `shipper` after your review and checks are done. Tell it which steps the user asked for, which files belong to the change, the issue number if any, and the verification you ran. It does not run tests or edit files.
- Branches start from `dev` and draft PRs target `dev` (`--base dev`). `dev` merges into `main` only for releases. Never push to `main` or `dev`.
- Update the workstream's row in `docs/STATUS.md` before you hand off, since shipper will not.
- If it stops on a pre-flight problem (an unrelated file, a shared file outside a `schema:` PR, a missing STATUS update), fix it and run it again.
- Do small git operations yourself when a handoff would cost more, such as one follow-up commit on an open PR.
- Print the PR link it returns in your final response.

## Main session responsibilities

Understand the request; find the root cause before delegating where practical; decide scope, architecture and strategy; set up the branch; review every subagent diff; run the full relevant checks; do the end-to-end check on the real surface (`make up`, click through with `USE_MOCK=false`); decide whether the task is done.
