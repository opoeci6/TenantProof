# Project status

Single source of truth. Update the relevant section in every PR.

**Date:** 2026-11-17 (MVP due Nov 18)
**Branch:** `dev` is the integration branch (`main` only for releases). Start work from `origin/dev`.
**Current milestone:** M1 is merged. M2 is half done: WS1 (evidence ingest and tenant uploads) is merged on `dev`. For WS2 (deploy), the hosted demo was set up by hand and is described in `docs/DEPLOY.md`; four WS2 items are merged (#62 to #64: the intake QR code, rate limits on the real client IP, expired logins deleted) and the rest of WS2 has no owner. M3 is merged except the worker and the `ingest` step: WS3's runtime, WS4 (corpus, `rules`, `gaps`), the `draft` step, New case, and editing the request to the tenant. Two legal sources still wait for a person to confirm them (#54). The hosted demo (https://test.8596117.xyz) runs `dev` on Haiku.

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
| M3 (WS3) | Prompt caching; `LLM_MODEL` and `ANTHROPIC_BASE_URL`; a citable page for each photo's capture date; the runner asks for the hearing date; visible conflict flags; cost priced with the model actually used | PRs #21, #26 to #30, #34 |
| M3 (demo) | Demo-honesty fixes from the usability review, neutral "Nothing yet" states, past hearing dates refused, synthetic upload files in `demo/`, the case brief PDF | PRs #23, #31 to #33, #37 |
| M3 (WS4) | Legal corpus, URL check, the `rules` step (sources and the "must be received by" deadline), the `gaps` step that drafts requests to the tenant; source check (Reg 25A, received-by wording, no HEIC in the package) | PR #35 (with #39), `569348d` |
| M3 (#40) | The `draft` step fills missing form fields from verified quotes, as `needs_confirmation`, with a Confirm button | PRs #46, #42 |
| M3 (#41) | `POST /cases` and a New case form on the caseload | PRs #46, #47 |
| M3 (#36, #38) | Locale-independent tests; the Tenant view shows the new intake link | PRs #43 to #45 (NineJZ356) |
| M3 (#49) | `RUN_BUDGET_S` setting: the whole run may take 180 s by default, one model call 60 s | PRs #50, #51 |
| M3 (#48) | The caseworker edits the request to the tenant and translates it with one model call; the tenant page shows the English too; language names in English first; English preview on the Tenant view | PRs #52, #53 (`074748a`) |
| M2 (WS2: #56 to #59) | QR code of a new intake link; rate limits on the real client IP behind a trusted proxy (`FORWARDED_ALLOW_IPS`); expired logins deleted at login and hourly; `docs/DEPLOY.md` for the hosted demo | PRs #62 to #64, #55 (`596b455`) |
| Submission polish | README rewrite (pitch, architecture, live-mode setup, 3-minute demo, team), LICENSE, SECURITY.md statuses, security headers, system fonts, planning files moved to `docs/archive/`, API_CONTRACT rows; caseload wording ("Deadline passed (N days ago)", plurals, one "Must be received by" wording for stored labels) and seeded deadlines kept relative to today | `Lameda12/submission-polish` |
| UI fix (Tenant view) | The Tenant view lives in the case rail, not the top bar; re-sending a sent intake link asks first | PRs #70, #71 (`292a6d7`) |
| M3 (#54) | Form J and Regulations s. 25A confirmed by a person (all five sources confirmed); the Form J draft is labelled partial; `pypdf[crypto]` reads Form J's locked PDF | PR #66 |
| M3 (#68, #69) | Tenant languages Russian and Hindi: schema, New case list, tenant page strings (machine-translated, need native review), agent language names | `Lameda12/tenant-languages-ru-hi` |
| M3 (#69 review) | Russian tenant page wording reviewed (Lameda12, 2026-11-17); #69 closed | PR #79 (`8a213a9`) |
| M3 (#54 follow-up) | Only registered mail, express post or courier counts as received on the 3rd day, and e-mail sent at or after 4 p.m. counts the next day (leozhang5867's check): package reminders, source notes, mock | PRs #72, #73 (`6ad8d60`) |
| M3 (#61, OCR part) | Text in photos and scanned PDF pages is read on the server (RapidOCR, no network) and cited as `text in the photo (OCR)` or `page N (OCR)`; unsure lines dropped; a time budget per upload; demo screenshot `demo/landlord_text_zh.png` | PRs #81, #82 |
| WS2 (#10) | A bad intake link (404) or a caseworker upload without a login (401) is refused before the body is read; the first commit is YCL0609's | PR #83 (`fe6f7ab`) |
| UI fix (open case in the URL) | The open case is kept in the address (`?case=<id>`), so a refresh stays on it instead of opening the first case; an id the user can't see falls back to the caseload's first case; sign-out clears it; mock mode unchanged | PRs #84, #85 (`0aea6d9`) |
| UI fix (review count) | The page's refreshes run one at a time, in order: fast review clicks could let an older timeline reply land last, so a reviewed event showed as not reviewed and the count was one short (the server was right) | PR #86 (`475bbac`) |
| M3 fix (re-running the analysis, #88) | A new run no longer adds a full second copy of the timeline. When the timeline step finishes, its events replace the ones nobody worked on; if it fails or stops early, those stay and its partial events go. Accepted, edited and rejected events stay, as do pending ones with a caseworker note or date, and the agent is told not to propose them again. A new run closes a run still waiting for an answer, and a removed event's unsent "Support for" gap goes with it. Built on opoeci6's `a44143f`; checked on three live Haiku 5.5 runs | PR #98 (`83caf5f`, NineJZ356) |
| UI fix (package result, #90) | After a build the Package page shows a result panel right under the button (large download button, version, who, when, full SHA-256), scrolls to it and says "Package vN built". Once a package exists the button reads "Build a new version"; after a reload the page finds the newest package in the audit log | PRs #102, #103 (`72a16e8`, NineJZ356) |
| UI (#93) | The Package page says how many timeline events are not reviewed and are left out of the application draft, with a link to the timeline; it does not block the build | PR #100 (`9fcd2e4`, YCL0609) |
| M3 (#95, #96) | The tenant can type a short answer to an open request (stored as a tenant text evidence item and shown on the request); the caseworker can show a quoted source in English (one model call per quote, cached, labelled as a machine translation, never cited or filed). New migration `3c9f1a7be204` | PR #99 (`1996dbb`, YCL0609) |
| UI fix (Accept all, #87) | **Accept all** on the timeline also leaves events the agent flagged as a conflict (their strength can still be single-source), and the toast says how many it accepted and how many it left, with and without a conflict | PRs #104, #105 (`5b7e222`) |
| M3 (#97) | Photo previews for the caseworker: a thumbnail on each Evidence row and in the open "Based on" source, opening a larger view in the page. Served by `GET /cases/{id}/evidence/{evidence_id}/preview` from the copy made at upload without EXIF or GPS, never the original; `EvidenceItem.has_preview` says when there is one | `opoeci6/fix-97-photo-preview` |
| M3 fix (tenant confirmation item) | The tenant confirming on the intake page now closes the blocking item "The tenant must confirm their files" that an earlier run opened; before, the package could never be built (found in the hands-on demo test, 2026-11-18). New tenant files after the confirmation open it again, a run brings it in line with the case (fixing cases already stuck), and a dismissed item stays dismissed | `opoeci6/fix-tenant-confirm-gap` |
| UI fix (sources after a run) | When a run ends, the page also reloads the case and its official sources, so "Official sources the agent used" is filled straight away instead of after a page reload, and the case header shows the new stage and deadline (found in the hands-on demo test, 2026-11-18) | `opoeci6/fix-sources-after-run` |
| UI fix (silent failures) | A click, form or field change whose request fails now says why in a toast (for example "Description cannot be empty" or "An analysis is already running for this case"); an expired sign-in opens the sign-in page; no reply says the server could not be reached. The run poller stops on 401 or 404, and Accept all refreshes even when one accept fails (found in the hands-on demo test, 2026-11-18) | `opoeci6/fix-silent-ui-errors` |
| Demo test fixes (bugs 4, 5, 6, 8, 9, 10, 11, 13, 14) | Caseload "need your review" counts cases with events to review whatever their stage; a bad or replaced tenant link says so on the page instead of "Loading…"; caseload rows stack at phone width; an answered request leaves the tenant page until it is sent again (a sent request now stores `sent_at`) and a second answer to it is refused, and the tenant's chosen language is kept after a reload; sending a request before the intake link says the link is still needed; an unreadable file shows why instead of "Format OK"; an unreadable photo left out of the zip is named on the Package page and in the manifest with the real reason; received times name the viewer's time zone; a New case error is scrolled into view | `opoeci6/fix-demo-test-bugs` |

Details of every deviation from `API_CONTRACT.md`: `CONTRACT_CHANGES.md` (#1 to #40).

## In progress

Open PRs (2026-11-18), all into `dev`: this one (`opoeci6/fix-97-photo-preview`) and #101 (YCL0609: splitting `web/index.html` into `index.html`, `index.css` and `index.js`; not for the submission).

Open issues:
- **#54 (closed, PR #66):** Lameda12 confirmed Form J (the full title line, edition 2024-01) and Regulations s. 25A(2) on 2026-11-17, so all five sources are confirmed. The Form J confirmation covers the title and URL only: the draft is partial (seven tenant-applicant fields; the caseworker completes the rest; F4 is out of scope). Full coverage is a post-submission issue. leozhang5867 also checked 25A(3B) and (3C): the app's "mail" and "after 4 p.m." wording is fixed in #73.
- **#74:** intake tokens are written to the access logs in plain text (the API's, and a proxy's path and `Referer`).
- **#4:** WS3 follow-ups from the PR #2 review.
- **#68:** Hindi needs text shaping in the PDFs (uharfbuzz with fpdf2 `set_text_shaping`); everything else is in. Russian (#69) is done and closed.
- **#60:** remove personal details from evidence text before it goes to the model (to discuss).
- **#61 (OCR part built, see Done):** after the MVP: let the model see photos and scanned pages, with content checks first (malware, known illegal images, graphic content).

WS5 is unassigned. WS2's four items above are opoeci6's; the rest of WS2 has no owner.

## Known issues

- **No production guard.** The app starts with dev defaults (`SEED_PASSWORD`, compose passwords, `COOKIE_SECURE=false` in compose). It must refuse to start when `ENV=production` and `SEED_PASSWORD` or `SECRET_KEY` is unset or a dev default. Neither `ENV` nor `SECRET_KEY` exists in `config.py` yet. Tracked in WS2's "Done when".
- **Blob storage is a local volume** (`STORAGE_DIR`). Several API replicas need shared storage (WS2).
- **Rate limits behind a proxy** count each visitor's own IP only when the proxy is listed in `FORWARDED_ALLOW_IPS` (default `127.0.0.1`). Behind a published Docker port the API sees the compose network's gateway, so list that or use `*` (see `docs/DEPLOY.md`); the hosted demo uses `*`.
- **Evidence text goes to the model as written**, names, addresses and phone numbers included (issue #60). Photos never do: the model gets only their capture time, device and editing software (issue #61 is about changing that after the MVP).
- **No malware scan or illegal-content check on uploads** (issue #61). Uploads are limited to JPG, PNG, HEIC, PDF and TXT, checked by content, and photos are re-encoded.
- **Font size:** the bundled Noto Sans SC adds about 10 MB to the repo and image.
- **`docker compose up --scale api=2` does not work yet:** the api service publishes fixed host port 8000, so a second replica cannot start without a proxy service in front (not planned for now). Migrations already run once, in the `migrate` service (#76), not once per replica.
- **The filing package files photos without their GPS data.** Each JPG, PNG and HEIC-made JPG in the zip is a copy with the GPS IFD (and an XMP packet carrying GPS) taken out; the capture time and orientation stay, and the stored original is untouched (`services/evidence/gps.py`). The manifest PDF says "location data removed" for each such file. Not covered: location in a maker note or in a legacy "Raw profile type exif" PNG text chunk. Issue #9's remaining part (warning the tenant on the intake page, an optional per-file "file the stripped copy instead") still waits on the open decision below.
- **A live intake link or a signed-in caseworker can send about 500 MB in one upload request** (20 × 25 MB + 1 MB). Bad credentials are refused before the body is read (#10). The upload rate limit runs after the body is read, so a per-client size cap would need a proxy in front (the hosted demo's nginx caps a request at 100 MB).
- **OCR reads English, Chinese, French and Spanish, not Russian or Hindi** (the bundled model knows neither script; its guesses are dropped, so those images give no text). It does not keep which side of a chat a bubble is on, and handwriting reads poorly. About 2 s per image, at most `OCR_REQUEST_SECONDS` (40) of OCR per upload request; a file whose OCR was skipped for time is not read again when the same file is uploaded again (it matches the stored copy). Images thinner than 1:20 are not read.
- **Big5 and GBK TXT files with characters outside GB2312 answer 415** (issue #11, closed by #25: kept on purpose). The reason now says "re-save it as UTF-8", which fixes every one of them. Big5 stays refused on purpose: it decodes as GB18030 into private-use characters and no byte pattern tells the two code pages apart, so a guess would file traditional Chinese as different characters. Windows-1252 and Shift-JIS are refused for the same reason.
- **Verified by an agent on 2026-11-06 (Playwright + rendered pages, see the PR #1 comments):** Chinese glyphs render in the built PDF, second-org isolation in two separate browser contexts, `?mock=1`, the empty-caseload state. **Electron 45 window (2026-11-06, Windows, against `web/index.html?mock=1` over the DevTools protocol):** the mock UI renders, the renderer has no Node, `window.open` is denied, off-origin navigation is blocked. **Electron 45 window against the real API (2026-11-07, Windows 11, NineJZ356, PR #1):** `npm install`, `node check.js` and `npm start` work, login works in the window, the renderer has no Node, `window.open` is denied. **Mac: pending Lameda12's check by eye** (`make desktop` starts the window; install and `check.js` pass).
- **Throughput work is deferred:** `WEB_CONCURRENCY` and gzip belong to WS2; the load re-benchmark (`backend/bench/load.py`, p50/p95/p99 on the compose stack) to WS5. `GET /api/cases` already runs a fixed number of queries (query-count test).
- **UI routes don't include the case id** (`#timeline`), so a case can't be deep-linked or shared.
- **Seed data has a zh-language tenant but all-English content.** The demo case needs real Chinese tenant messages and evidence text (fictional) so the video shows multilingual intake. Owner: WS5.
- **CI runs on GitHub** with Postgres and Redis service containers; both checks are green on PR #1 (re-checked after each push).
- Rate-limit counters only count requests that pass body validation.
- Two seeded demo logins share one password (`SEED_PASSWORD`, compose default `tenantproof-demo`). Local only.

## WS3 TODO

- `schema:` PR for `config.py` and `.env.example`: `anthropic_api_key`, `record_fixtures`, `fixture_strict` (today `FIXTURE_STRICT` is read from the environment in `llm.py`; tests set it to 1). Until then `llm._live_api_key()` reads `ANTHROPIC_API_KEY` from the process environment only (not from `backend/.env`). The model is the `LLM_MODEL` setting (#26), defaulting to `llm.MODEL`.
- `RecordingProvider` (`RECORD_FIXTURES=1`) is not built yet. `Run.usage.est_cost_usd` uses the list price of the model actually used, cache reads and writes included (#34); a model not in the table gets no estimate.
- **Text and facts store:** `SqlTextStore` (`services/agent/evidence_text.py`) reads the `evidence_page` and `evidence_fact` tables (PR #6); the in-memory store stays for tests. The `extract` step (`services/agent/extract.py`, issue #14) writes the facts at the start of a run; WS1 ingest (PR #8) writes the pages. `runner.py` callers pass `SqlTextStore(session)`.
- Not built: the arq worker (a run executes as a background task in the API process), and the `ingest` step; `IMPLEMENTED_STEPS` is `extract`, `timeline`, `rules`, `draft` and `gaps`, in that order. The whole run may take `RUN_BUDGET_S` (180 s by default), one model call 60 s.
- **`draft` (#40, opoeci6, merged):** an agent loop, not deterministic as BUILD_PLAN first said (extracted facts carry no field key). Its one write tool, `fill_form_field`, fills only a `missing` field, never `remedy`; every citation must pass the verifier and the value must be words of a verified excerpt. Values land as `needs_confirmation`, and the form page has a Confirm button for them. With nothing to fill it makes no model call.
- `search_legal_sources` returns the corpus excerpt and its locator (WS4). An entry nobody has confirmed has no excerpt and returns `""`, never invented text.
- One random evidence nonce per run, saved on the steps so a resume reuses it. `request_hash` masks it, so recordings replay under any nonce.
- A photo gets one code-written page with its capture time (#28), so the agent can cite it. An `exif` date candidate must match a cited file's capture time.
- `flag_conflict` adds a `conflict` flag (contract change #30) and still appends to `strength_reasons`; the review screen shows a "Conflict" badge with the note. Case brief PDF for the caseworker (`GET /cases/{id}/case-brief`, contract change #34, merged as #37). Demo-honesty fixes from the AI usability review (merged as #31): "Not analysed yet" in the caseload and a banner, package 409 without a completed run or an accepted event (contract change #31), "Not in this prototype" for the unbuilt steps, duration and cost on the run page, Demo rows, a reason next to the disabled Send button, no Arabic link. The runner asks for the hearing date itself when the timeline step ends with none set and the model did not ask (the `ask_caseworker` pause, field `hearing_date`).

## Open decisions

- Who owns each workstream (table below).
- Whether the filed package carries stripped copies instead of originals. Originals keep GPS, but they also keep the sha256 chain and the EXIF that authenticate them. This decides issue #9.
- How tenants are told that originals are filed: #9's warning on the intake page.
- Whether `SECRET_KEY` is needed at all: sessions are server-side random tokens, so nothing signs with it today. Either WS2 adds a real use or drops it from the guard.
- Legal sources (WS4): all five are confirmed by a person (NineJZ356, 2026-11-16; Lameda12, 2026-11-17, issue #54). The checklist is `backend/app/services/legal/README.md`; Policy #28's URL is dead and the entry is left out, which that file explains.
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
| WS1 Evidence ingest + tenant uploads (M2) | opoeci6 | merged (`24a343d`) | done: upload routes, type check by signature, metadata, stripped copy, text as `evidence_page` rows, evidence in the zip (PRs #6, #7, #8). Follow-ups: #9 (#11 closed by #25) | none |
| WS2 Hosted deploy + infra hardening (M2) | opoeci6 (#56 to #59, #76), YCL0609 (#10) | merged | the hosted demo is set up by hand and described in `docs/DEPLOY.md` (#57). Merged: #62 to #64 (#56 QR code of the intake link, #58 real client IP for the rate limits, #59 expired logins deleted). Open: #76, the `migrate` service and the `/healthz` readiness check from YCL0609's `m2-deploy` branch. Open: `opoeci6/m2-intake-gate` (#10, YCL0609's gate). Not built and unowned: a managed host, S3 storage, the production guard, `--scale api=2`, a per-client upload size cap | none |
| WS3 Agent runtime (M3) | Lameda12 | merged | day 1 merged (PR #2); day 2 merged as PRs #5, #15, #18, #19, #20: the 12 tool bodies, `loop.py`, `runner.py` (one nonce per run, a whole-run deadline, `RUN_BUDGET_S`, 180 s by default since #51; pause/resume), `verifier.py`, `strength.py`, `dates.py`, refusal handling, `SqlTextStore` over the #6 tables, `POST /cases/{id}/runs` (issue #13), the `extract` step (issue #14), `POST /runs/{id}/answer` (contract in PR #19) with the run page's answer control, and the demo evals (PR #16). Prompt caching on the Anthropic provider (breakpoints on the last tool and the stable system text, the nonce in a run note after them); cache tokens are in the run's stored usage. `rules` and `gaps` came with WS4 (#35). The `draft` step (#40, opoeci6, approved by Lameda12) is merged as #42: the `fill_form_field` tool and a form agent. Next: the arq worker and the `ingest` step | nothing, for what is merged |
| WS3 Tenant answers + quoted-source translation (M3) | YCL0609 | YCL0609/m2-tenant-answers | in progress: #95 lets the tenant type a short answer per request on the intake page, stored as a TXT evidence item (`message 1` locators) and recorded as `Gap.answer`; #96 adds a cached **Show in English** reading aid for any non-ASCII quote (`POST /cases/{id}/translate-excerpt`, `excerpt_translation` table, activity log, never cited or filed). Schema commit 2748f15; feature commit on the same branch; tests added (`test_translation.py`, `test_intake_answers.py`), not run in this environment (no Docker/Python 3.12) | none |
| WS4 Legal sources, rules, gaps agent (M3) | opoeci6 | merged (#35, `569348d`) | merged on 2026-11-16 with Lameda12's #39 inside; NineJZ356 confirmed three sources; Lameda12 confirmed Form J and Reg 25A on 2026-11-17 (#54). PR 1: `services/legal/` with the corpus (four official sources, verbatim excerpts), the URL check and its command, excerpts in `search_legal_sources`, and the deterministic `rules` step (sources plus the evidence deadline). PR 2: the `gaps` step — deterministic checks first, then an agent that drafts requests to the tenant in their language. **A person must confirm the last two sources** (#54): `backend/app/services/legal/README.md`. A source check (fix/legal-sources) corrected the README, added the Regulations s. 25A entry, relabelled the deadline "must be received by", and keeps `.heic` out of the package. Since then: the gaps prompt skips fields the `draft` step filled (#42), and the caseworker can edit and translate each request to the tenant (#48, PRs #52 and #53) | a person for #54 |
| WS5 Evals, security, perf, demo (M3 to M4) | TODO | `<github-username>/m4-evals-security-perf` | not started | starts now; fills in as WS1 to WS4 land |
