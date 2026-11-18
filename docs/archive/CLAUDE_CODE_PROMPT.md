Paste this into Claude Code from the repo root. First put `API_CONTRACT.md`, `BUILD_PLAN.md` (v2) and
`tenantproof-prototype.html` in the root. Main session: Sonnet 5.5 (or Opus if your plan has room). Start in plan mode (Shift+Tab).

---

We're building TenantProof, our MVP for the Cognizant BrAInstorm Challenge (theme: Agentic AI for Good). The MVP is due Nov 18 and is judged on functionality, code quality, technical implementation, agentic AI usage, scalability, security and performance. Build for those criteria.

Read these fully first:
- `API_CONTRACT.md`: source of truth for endpoints and response shapes.
- `BUILD_PLAN.md`: architecture, contract fixes, agent design, rules, security, performance targets, schedule.
- `tenantproof-prototype.html`: the UI. It calls everything through `request(method, path, body)` with `USE_MOCK` and `API_BASE`.

## Setup (do this first)

1. Create `CLAUDE.md` from section 12 of `BUILD_PLAN.md`, `CONTRACT_CHANGES.md` from section 3, and a `SECURITY.md` stub from section 7.
2. Create these three subagents in `.claude/agents/`:

`.claude/agents/scaffolder.md`
```
---
name: scaffolder
description: Writes boilerplate from an exact spec given by the main session: routers, seed data, docker-compose, CI config, simple CRUD. Use for mechanical code where the spec names every file and shape.
tools: Read, Write, Edit, Grep, Glob, Bash
model: haiku
---
You write boilerplate for TenantProof. Follow CLAUDE.md. Only create or edit the files named in your task. Match API_CONTRACT.md field names exactly; import shapes from backend/app/schemas.py, never redefine them. If the spec is ambiguous, stop and report the question instead of guessing. End with: files changed, and anything you skipped.
```

`.claude/agents/test-writer.md`
```
---
name: test-writer
description: Writes pytest tests for a named module or endpoint: unit tests for pure rule functions, contract tests that validate responses against schemas, security tests (org isolation, caseworker_note never leaks). Use after a module is written.
tools: Read, Write, Edit, Grep, Glob, Bash
model: haiku
---
You write tests for TenantProof. Follow CLAUDE.md. Only edit files under backend/tests/. Test behavior from BUILD_PLAN.md sections 3, 6 and 7, including edge cases (month-precision dates, zero sources, rejected events, other org's ids). Tests never hit the network; use LLM_MODE=fixture. Run the tests you wrote and report pass/fail. Never change application code to make a test pass; report the bug instead.
```

`.claude/agents/checker.md`
```
---
name: checker
description: Runs ruff, pyright and pytest and returns a short failure list. Use before every commit and after any subagent finishes.
tools: Bash, Read, Grep
model: haiku
---
Run from backend/: `uv run ruff check .`, `uv run pyright`, `uv run pytest -q`. Do not edit any file. Report at most 20 lines: each failure as file:line, the error, and a one-line likely cause. If everything passes, say "all green" and the test count.
```

3. Inspect the prototype (use the built-in Explore agent): extract its mock data for `seed.py`, tell me whether the tenant intake view is in the same file (serve it at `/i/{token}`) or needs splitting, and list any UI calls the contract doesn't cover.

## How to split the work

You (main session) own and write yourself: `schemas.py`, `models.py`, `db.py`, `config.py`, `auth.py`, `deps.py`, everything under `services/agent/` and `services/legal/`, `verifier.py`, `gating.py`, and all security code. You also review every subagent diff before committing.

Delegate to subagents only with a written spec naming every file, the contract section, and what "done" means:
- `scaffolder`: routers once schemas exist, `seed.py`, `docker-compose.yml`, `.github/workflows/ci.yml`, Electron `desktop/main.js`, bench scripts.
- `test-writer`: tests for each module right after it lands.
- `checker`: before every commit.

Run subagents in parallel only when their files don't overlap. Never let two agents edit the same file.

## Milestones

Start with M0 and M1 from section 10. **Stop after M1** and wait for my review. For each milestone: propose the plan, wait for my approval, build, have `checker` report all green, commit, then give me a 3-line summary of what's demoable and what's next.

## Hard constraints

- Response shapes match the contract field-for-field (plus the logged changes). Pydantic schemas mirror the TS types; one contract test per endpoint.
- Stack: FastAPI + SQLModel/Alembic on Postgres, arq worker on Redis, all in `docker compose`. The API stays stateless.
- Priority, strength, dates, narrative, gating and the verifier are pure, unit-tested functions with no LLM calls.
- Every mutation writes an activity entry. Every by-id handler checks org ownership.
- `sha256` is of the original bytes. The model only gets EXIF-stripped copies.
- `LLM_MODE=live` is the app default; tests and CI use `fixture` and never touch the network.
- Keep `USE_MOCK=true` working in the UI; only change config and routing in `web/index.html`.
- Electron loads the app origin over HTTP(S), never `file://`, with contextIsolation, sandbox and no Node in the renderer.

If the contract is ambiguous or contradicts the plan, use the plan's resolution, log it in `CONTRACT_CHANGES.md`, and keep going. Ask me only about decisions that change the demo flow or the schedule.
