# Project status

Single source of truth. Update the relevant section in every PR.

**Date:** 2026-11-15 (MVP due Nov 18)
**Branch:** `dev` is the integration branch (`main` only for releases). Start work from `origin/dev`.
**Current milestone:** M1 is merged. M2 is half done: WS1 (evidence ingest and tenant uploads) is merged on `dev`, WS2 (deploy) has no owner. M3's WS3 runtime is merged except the worker and the `ingest`, `rules`, `gaps` and `draft` steps; WS4 (legal sources, gaps agent) has no owner.

## Done

| Milestone | What | Commit |
|---|---|---|
| M0 | Skeleton, all GET endpoints on Postgres, UI served by the API, seeded demo case | `e26e881` |
| M0 review | Random hashed intake tokens, configurable DB port, Makefile | `6fa3af5` |
| M1 | Argon2 login + server-side sessions, review/gap/form mutations, package build with versions and sha256, tenant submit (attestation only), Electron shell | `b1212b4` |
| M1 hardening | Seed only when DB is empty (`make reset`), 422 for sourceless accepts, Redis in CI, test audit, concurrent-build test | `9d56e2f` |
| M1 follow-up | Electron 33 to 45.2.3 (`npm audit` clean, Node 22.12+, no install-script pin needed); Claude Code workflow and `scoper`/`implementer`/`reviewer`/`shipper` subagents; local-environment rules in AGENTS.md | PR #1 |
| M1 test fixes | From NineJZ356's Windows run: the seed reads `seed_data.json` as UTF-8 (tests and `make seed` failed on Windows code pages); tests ignore `backend/.env`, so the quickstart's `COOKIE_SECURE=false` no longer fails the Secure-cookie test | PR #1 |
| M2 (WS1) | Evidence ingest and tenant uploads: upload routes, type check by signature, metadata, stripped copy, text as `evidence_page` rows, evidence in the zip | PRs #6, #7, #8 (`24a343d`) |
| M3 (WS3) | Agent tool bodies and loop, `POST /cases/{id}/runs`, the `extract` step writing `evidence_fact`, `POST /runs/{id}/answer`, demo evals for prompt injection and fabricated quotes | PRs #5, #15, #16, #18, #19, #20 |

Details of every deviation from `API_CONTRACT.md`: `CONTRACT_CHANGES.md` (#1 to #29).

## In progress

Open draft PRs into `dev` (2026-11-15), all Lameda12's. None of them waits on WS1.

| PR | Area | What | Depends on |
|---|---|---|---|
| #21 | WS3 | Prompt caching on the Anthropic provider (`services/agent/`) | none |
| #22 | infra | `make up` without an API key and with a broken IPv6 resolver (Dockerfile, compose) | none |
| #23 | demo | Synthetic demo files for the live run (`demo/landlord_repair_notice.pdf`, `demo/chat_zh.txt`, a stock photo kept out of git) and an empty seeded case `case_live_001` | WS1 uploads (merged) |

Open issues: #9 (tell tenants that original photos, GPS included, go into the package), #10 (about 500 MB can be spooled to disk before the intake token is checked; WS2), #11 (Big5 and GBK TXT outside GB2312 get 415). #13, #14 and #4 track WS3 work that looks done but is not closed. WS2, WS4 and WS5 are unassigned.

## Known issues

- **No production guard.** The app starts with dev defaults (`SEED_PASSWORD`, compose passwords, `COOKIE_SECURE=false` in compose). It must refuse to start when `ENV=production` and `SEED_PASSWORD` or `SECRET_KEY` is unset or a dev default. Neither `ENV` nor `SECRET_KEY` exists in `config.py` yet. Tracked in WS2's "Done when".
- **Blob storage is a local volume** (`STORAGE_DIR`). Several API replicas need shared storage (WS2).
- **Rate limits behind a proxy** key on the socket IP; needs `--proxy-headers` and trusted proxy config (WS2).
- **Expired sessions are never purged** (WS2).
- **Font size:** the bundled Noto Sans SC adds about 10 MB to the repo and image.
- **`docker compose up --scale api=2` does not work yet:** the api service publishes fixed host port 8000. WS2 needs a proxy service.
- **The filing package files original photos, GPS included.** The HEIC→JPG copy keeps the original EXIF on purpose (`services/package.py`, `heic_to_jpeg`), so location metadata reaches the zip. Issue #9 covers warning the tenant on the intake page and an optional per-file "file the stripped copy instead"; both wait on the open decision below.
- **An unauthenticated upload can spool about 500 MB to disk** (20 × 25 MB + 1 MB) before the intake token is checked. Needs a per-client cap at a proxy (issue #10, WS2).
- **Big5 and GBK TXT files with characters outside GB2312 answer 415** (issue #11). The reason now says "re-save it as UTF-8", which fixes every one of them. Big5 stays refused on purpose: it decodes as GB18030 into private-use characters and no byte pattern tells the two code pages apart, so a guess would file traditional Chinese as different characters. Windows-1252 and Shift-JIS are refused for the same reason.
- **Verified by an agent on 2026-11-06 (Playwright + rendered pages, see the PR #1 comments):** Chinese glyphs render in the built PDF, second-org isolation in two separate browser contexts, `?mock=1`, the empty-caseload state. **Electron 45 window (2026-11-06, Windows, against `web/index.html?mock=1` over the DevTools protocol):** the mock UI renders, the renderer has no Node, `window.open` is denied, off-origin navigation is blocked. **Electron 45 window against the real API (2026-11-07, Windows 11, NineJZ356, PR #1):** `npm install`, `node check.js` and `npm start` work, login works in the window, the renderer has no Node, `window.open` is denied. **Mac: pending Lameda12's check by eye** (`make desktop` starts the window; install and `check.js` pass).
- **Throughput work is deferred:** `WEB_CONCURRENCY` and gzip belong to WS2; the load re-benchmark (`backend/bench/load.py`, p50/p95/p99 on the compose stack) to WS5. `GET /api/cases` already runs a fixed number of queries (query-count test).
- **UI routes don't include the case id** (`#timeline`), so a case can't be deep-linked or shared.
- **Seed data has a zh-language tenant but all-English content.** The demo case needs real Chinese tenant messages and evidence text (fictional) so the video shows multilingual intake. Owner: WS5.
- **CI runs on GitHub** with Postgres and Redis service containers; both checks are green on PR #1 (re-checked after each push).
- Rate-limit counters only count requests that pass body validation.
- Two seeded demo logins share one password (`SEED_PASSWORD`, compose default `tenantproof-demo`). Local only.

## WS3 TODO

- `schema:` PR for `config.py` and `.env.example`: `anthropic_api_key`, `llm_model`, `record_fixtures`, `fixture_strict` (today `FIXTURE_STRICT` is read from the environment in `llm.py`; tests set it to 1). Until then `llm._live_api_key()` reads `ANTHROPIC_API_KEY` from the process environment only (not from `backend/.env`), and the model is the constant `llm.MODEL`.
- `RecordingProvider` (`RECORD_FIXTURES=1`) is not built yet. `Run.usage.est_cost_usd` is estimated from the `claude-sonnet-5-5` list price in `llm.py` (input and output tokens only, no cache pricing).
- **Text and facts store:** `SqlTextStore` (`services/agent/evidence_text.py`) reads the `evidence_page` and `evidence_fact` tables (PR #6); the in-memory store stays for tests. The `extract` step (`services/agent/extract.py`, issue #14) writes the facts at the start of a run; WS1 ingest (PR #8) writes the pages. `runner.py` callers pass `SqlTextStore(session)`.
- Not built: the arq worker (a run executes as a background task in the API process), and the `ingest`, `rules`, `gaps` and `draft` steps; `IMPLEMENTED_STEPS` is `extract` and `timeline`.
- `search_legal_sources` returns an empty `excerpt` until WS4 stores the official text.
- One random evidence nonce per run, saved on the steps so a resume reuses it. `request_hash` masks it, so recordings replay under any nonce.
- A photo has no text page, so it cannot be cited yet (the seeded "EXIF" locator is rejected); the store needs a metadata locator for photos. An `exif` date candidate must match a cited file's capture time.
- `flag_conflict` adds a `conflict` flag (contract change #30) and still appends to `strength_reasons`; the review screen shows a "Conflict" badge with the note. Case brief PDF for the caseworker (`GET /cases/{id}/case-brief`, contract change #34, draft PR). Demo-honesty fixes from the AI usability review (draft PR): "Not analysed yet" in the caseload and a banner, package 409 without a completed run or an accepted event (contract change #31), "Not in this prototype" for the unbuilt steps, duration and cost on the run page, Demo rows, a reason next to the disabled Send button, no Arabic link. The runner asks for the hearing date itself when the timeline step ends with none set and the model did not ask (the `ask_caseworker` pause, field `hearing_date`).

## Open decisions

- Who owns each workstream (table below).
- Whether the filed package carries stripped copies instead of originals. Originals keep GPS, but they also keep the sha256 chain and the EXIF that authenticate them. This decides issue #9.
- Whether WS1 writes an `EXIF` `evidence_page` so the agent can cite a photo's capture time (today a photo has no text page, so it cannot be cited).
- How tenants are told that originals are filed: #9's warning on the intake page.
- Whether `SECRET_KEY` is needed at all: sessions are server-side random tokens, so nothing signs with it today. Either WS2 adds a real use or drops it from the guard.
- Legal sources (WS4): a human must open every URL and confirm section, form name and wording before Nov 16.
- `backend/app/config.py` is now a shared file (settings change via `schema:` PRs); workstreams may add their own dependencies to `backend/pyproject.toml`.
- Whether to keep bundling the 10 MB CJK font or fetch it at build time.

## How to verify

Prerequisites: Docker, `uv`, Node 22.12+ (desktop only).

```bash
cp backend/.env.example backend/.env
make up                      # api + Postgres + Redis, migrates, seeds an empty DB
curl localhost:8000/healthz  # {"status":"ok",...}
make test                    # starts Postgres + Redis, runs pytest (about 2 minutes)
make lint                    # ruff + pyright
cd desktop && node check.js  # Electron security flags
```

Click-through (real API): open http://localhost:8000, sign in as `sam@example-clinic.test` (password `tenantproof-demo`), open Jordan Lee's case, review events, resolve both required items, tick the confirmations on Package, build twice, download v1 and v2. Mock mode: http://localhost:8000/?mock=1.

Reset local data: `make reset` (drops the dev database, migrates, reseeds).

## Workstreams

Prompts: `docs/NEXT_PROMPTS.md`.

| Workstream | Owner | Branch | Status | Blocked by |
|---|---|---|---|---|
| WS1 Evidence ingest + tenant uploads (M2) | opoeci6 | merged (`24a343d`) | done: upload routes, type check by signature, metadata, stripped copy, text as `evidence_page` rows, evidence in the zip (PRs #6, #7, #8). Follow-ups: #9 and #11 | none |
| WS2 Hosted deploy + infra hardening (M2) | TODO | `<github-username>/m2-deploy` | not started | none (S3 adapter test and QR demo need WS1 for uploads) |
| WS3 Agent runtime (M3) | Lameda12 | merged | day 1 merged (PR #2); day 2 merged as PRs #5, #15, #18, #19, #20: the 12 tool bodies, `loop.py`, `runner.py` (one nonce per run, 60 s whole-run deadline, pause/resume), `verifier.py`, `strength.py`, `dates.py`, refusal handling, `SqlTextStore` over the #6 tables, `POST /cases/{id}/runs` (issue #13), the `extract` step (issue #14), `POST /runs/{id}/answer` (contract in PR #19) with the run page's answer control, and the demo evals (PR #16). Prompt caching on the Anthropic provider (breakpoints on the last tool and the stable system text, the nonce in a run note after them); cache tokens are in the run's stored usage. Next: the arq worker and the `ingest`, `rules`, `gaps` and `draft` steps; prompt caching in PR #21 | nothing, for what is merged |
| WS4 Legal sources, rules, gaps agent (M3) | TODO | `<github-username>/m3-legal-gaps` | not started | none (the WS3 `tools.py` protocol is merged) |
| WS5 Evals, security, perf, demo (M3 to M4) | TODO | `<github-username>/m4-evals-security-perf` | not started | starts now; fills in as WS1 to WS4 land |
