# Deploying the hosted demo

How the hosted demo runs today, and how to update it. It was set up by hand on one Linux server;
this file is the record of that set-up (issue #57). It holds no secrets, IP addresses, domain
names or key names: ask the server's maintainer for the demo domain, SSH access and the values of
the settings named below.

The planned production deploy (WS2 in `BUILD_PLAN.md`: a managed host, object storage for blobs,
several API replicas) is not built. See "What this deploy does not cover" at the end.

## The pieces

```
phone or browser ──HTTPS──> Cloudflare ──HTTPS──> nginx (on the server) ──HTTP──> api container
                                                                                   ├─ postgres container
                                                                                   └─ redis container
```

- **Cloudflare** holds the domain's DNS. The subdomain is a proxied record of its own (there is
  no wildcard record), so visitors reach Cloudflare, never the server directly.
- **nginx** on the server ends HTTPS with a Cloudflare origin certificate and passes requests to
  the API on a localhost port. Its site file for TenantProof is separate from the server's other
  sites. It:
  - restores each visitor's own IP from Cloudflare's `CF-Connecting-IP` header (the realip
    module: `set_real_ip_from` lists Cloudflare's published IPv4 and IPv6 ranges, and
    `real_ip_header CF-Connecting-IP`);
  - **sets** `X-Forwarded-For` to that one address (`proxy_set_header X-Forwarded-For
    $remote_addr`, not `$proxy_add_x_forwarded_for`), so an `X-Forwarded-For` a visitor sends
    is thrown away instead of appended to, and sets `X-Forwarded-Proto https`;
  - waits up to 120 s for a reply (`proxy_read_timeout`). Analysis runs take longer than that,
    but they run in the background and the page polls, so no request waits that long;
  - allows request bodies up to 100 MB (`client_max_body_size`), above the app's own 25 MB per
    file, so the app, not nginx, refuses a file that is too big;
  - leaves the HTTP-to-HTTPS redirect to the server's shared nginx config.
- **docker compose** runs the repo's `docker-compose.yml` (Postgres, Redis, `migrate` and the API)
  from a clone of the repo, plus a server-only `docker-compose.override.yml` (below). The
  one-shot `migrate` container runs the migrations and seeds an empty database on every
  `docker compose up`, then exits; the API starts only after it succeeds. It never wipes a
  database that already has data. `docker compose ps -a` lists it as exited (0), which is normal.

## Server-only files (never committed)

**`docker-compose.override.yml`**, next to `docker-compose.yml`:

| What | Why |
|---|---|
| The API's port is published on `127.0.0.1` only | Only nginx can reach it. This is what makes `FORWARDED_ALLOW_IPS=*` safe. |
| Postgres and Redis publish no host ports | Nothing outside the compose network needs them. |
| `PUBLIC_BASE_URL=https://<the demo domain>` | Intake links and the Origin check use it. |
| `COOKIE_SECURE=true` | The site is HTTPS. Compose's own default (`false`) is for plain-http localhost only. |
| `FORWARDED_ALLOW_IPS=*` | The rate limits then count each visitor's IP from nginx's `X-Forwarded-For` (#58). Use `*` only while the API port is reachable by nginx alone. Otherwise list the address the API container sees nginx connect from: through a published port that is the compose network's gateway (or its subnet), never `127.0.0.1`. Leaving the default `127.0.0.1` silently puts every visitor back on one shared budget. |
| `restart: unless-stopped` on every service | The demo comes back after a server reboot. |

Set these as `environment:` entries. The port changes use compose's merge tags: `ports: !reset []`
removes Postgres's and Redis's published ports, and `ports: !override ["127.0.0.1:<port>:8000"]`
replaces the API's. Do not override the API's `command:` to add uvicorn flags: once, a
`command:` with the flags on a more-indented YAML line silently dropped them.

**`.env`** (mode 600), which compose reads for `${...}` substitution. Names only; ask the
maintainer for values:

| Setting | Notes |
|---|---|
| `SEED_PASSWORD` | Password of the two seeded caseworker accounts. Random, not the local demo one. |
| `COMPOSE_PROJECT_NAME` | Keeps this app's containers and volumes apart from the server's other compose apps. |
| `LLM_MODE` | `live` for real analysis runs, `fixture` for none. |
| `ANTHROPIC_API_KEY` | Only with `LLM_MODE=live`. The maintainer enters it on the server; never paste it into a chat, a commit or a ticket. |
| `LLM_MODEL` | Optional. Empty means the built-in default. The demo sets a smaller model because of the API account's rate limits; remove it once those are raised. |
| `RUN_BUDGET_S` | Optional, seconds one analysis run may take (default 180). |
| `OCR_ENABLED` | Optional, default `true`: text in photos and scanned PDF pages is read on this server and becomes evidence text. `false` turns it off. |
| `OCR_REQUEST_SECONDS` | Optional, default 40: the most one upload request spends on OCR (about 2 s per image). Later images are kept without their text, and the evidence says so. Keep it under the proxy's timeout (Cloudflare: 100 s). |

## Updating the demo

From the clone on the server (it tracks `dev`):

```bash
git pull
docker compose up -d --build
docker compose ps
curl -s https://<the demo domain>/healthz
```

`/healthz` answers `{"status":"ok","llm_mode":"live"}` (or `fixture`) when the API is up and
reaches both Postgres and Redis, and 503 `{"status":"degraded",...}` when it cannot reach one of
them; `docker compose logs api` says which (`healthz: degraded (db=ok redis=error)`). If the API
does not start at all, check `docker compose logs migrate`: a failed migration stops the API from
starting on the new code.

**Trying PRs that are not merged yet:** use a throwaway branch that exists only on the server.

```bash
git fetch origin
git checkout -B deploy/<name> origin/<first PR branch>
git -c user.name=deploy -c user.email=deploy@localhost merge origin/<second PR branch>
docker compose up -d --build
```

Never push that branch. Afterwards go back with `git checkout dev && git pull` and rebuild.

## Resetting the demo data

Live runs and test uploads add cases, runs and files to the demo database. Before recording a
demo, reload the seed data:

```bash
docker compose exec api uv run --no-dev python -m app.seed --force
```

This **wipes every case** (the seeded ones come back as new, any others are gone), every login
session and every intake link. Uploaded files stay in the `blobs` volume but nothing points to
them any more.

## Live mode and the model

- With `LLM_MODE=fixture` the app starts and works without an API key, but an analysis run has no
  recorded replies and ends failed.
- For real runs, the maintainer puts `LLM_MODE=live` and `ANTHROPIC_API_KEY` in `.env` and runs
  `docker compose up -d` again. Evidence text (never the photos or their GPS data) goes to the
  model; `ANTHROPIC_BASE_URL` would send it to another host, so leave it empty unless you mean that.
- If runs fail with 429, the API account's rate limit for that model is too low: set `LLM_MODEL`
  to a model the account allows, or raise the limit.

## Checking it from outside

Clients other than a browser may need a `User-Agent` header: Cloudflare answers 403 to some
default ones (Python's, for example).

## What this deploy does not cover

The rest of WS2 is not built: a managed host, object storage for uploaded files (they live in a
Docker volume on this one server), a guard that refuses unsafe production settings, more than one
API replica (`--scale api=2`), and a per-client cap on upload size. An upload with a bad intake
link or without a login is refused before the API reads its body (#10), but this server's nginx
buffers up to 100 MB of every request body before passing it on, and a live link may send that much
in one request. Backups are not set up either: the Postgres volume and the `blobs` volume exist
only on this server.
