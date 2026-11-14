# Psychotherapy Booking Service

A booking service for a single psychotherapist's practice. Clients read practice
information and request consultations through a **web app** and a **Telegram
bot**, with **email** as an outbound-only channel. The therapist approves every
booking.

Booking logic lives in `app/core/`. Channels are thin adapters over it — a
scheduling rule inside a Telegram handler or a FastAPI route is a bug.

- `docs/DESIGN.md` — reasoning, trade-offs, rejected alternatives.
- `docs/IMPLEMENTATION.md` — **normative**. Schema, state machines, routes,
  worker jobs, milestones, acceptance criteria.

**Status: M9 (hardening) complete.** All milestones M0 → M9 are done.

---

## Setup from a fresh clone

Docker and Docker Compose are the only requirements. Python, PostgreSQL, and
every dependency live in the image.

### 1. Configure

```bash
cp .env.example .env
```

Generate the two secrets:

```bash
openssl rand -hex 32
```

Fill in `.env`. These have no working default and the stack will not start
without them:

| Variable | What it is |
|---|---|
| `POSTGRES_PASSWORD` | Any strong value; also goes inside `DATABASE_URL`. |
| `DATABASE_URL` | `postgresql+asyncpg://psycho:<password>@db:5432/psychobooking` |
| `SECRET_KEY` | ≥32 bytes. Signs cookies and derives the webhook path. |
| `BASE_URL` | Your public origin, no trailing slash. |
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather). |
| `TELEGRAM_BOT_USERNAME` | The bot's `@name`, without the `@`. |
| `TELEGRAM_WEBHOOK_SECRET` | Another `openssl rand -hex 32`. **At least 16 characters, and required in webhook mode** (the default): the app refuses to start without it. |
| `TELEGRAM_ADMIN_IDS` | Your own Telegram user id. Without it the bot has no admin. |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | The web admin login. Hashed at first start; the plaintext is never stored. |

`DOMAIN` and `ACME_EMAIL` are needed only by the `tls` profile, which refuses to
start without them. Leave them blank under `plain` or `cloudflared` — a tunnel
hands you a hostname without you owning a domain.

Leave `SMTP_HOST` empty to run without email: no email identities are created,
no email is queued, and clients sign in through Telegram. Set it (with
`SMTP_FROM`) to turn the channel on.

`CLIENT_IP_HEADER` (optional) names the header the per-IP rate limits read the
caller's address from. Compose sets it: `CF-Connecting-IP` on the `cloudflared`
profile (Cloudflare writes it, the caller cannot), `X-Forwarded-For` otherwise
(the *last* entry, which the one proxy in front wrote). uvicorn trusts no
forwarded header itself. Set it to empty only if nothing sits in front.

`.env` is git-ignored and must stay that way. Keep it `chmod 600`. Only `.env.example` is committed.

### 2. Start

The default `tls` profile runs Caddy in front, which obtains and renews a Let's
Encrypt certificate for `DOMAIN` by itself:

```bash
docker compose up -d
```

Behind an existing reverse proxy, use `plain` — it binds to `127.0.0.1:8000`
only and publishes nothing on `0.0.0.0`:

```bash
COMPOSE_PROFILES=plain docker compose -f docker-compose.yml -f docker-compose.plain.yml up -d
```

With a Cloudflare Tunnel, so no inbound ports are opened at all:

```bash
COMPOSE_PROFILES=plain,cloudflared docker compose -f docker-compose.yml -f docker-compose.plain.yml -f docker-compose.cloudflared.yml up -d
```

`web` migrates and seeds before serving; `worker` waits on its healthcheck, so
exactly one process does either.

### 3. Check

```bash
curl -fsS https://your-domain/healthz
```

Then sign in at `https://your-domain/admin` with `ADMIN_USERNAME` and
`ADMIN_PASSWORD`, and put some content in: **Content** → pick a topic → write a
block → **Preview per channel** → **Save**. The client site is at `/`.

The Telegram webhook needs a publicly reachable HTTPS URL. Running `plain` with
nothing terminating TLS in front, the app refuses to register one and says so;
set `TELEGRAM_MODE=polling` for local development.

```bash
docker compose logs -f web worker
```

---

## What changed for an operator (NP1-NP3)

- **A web booking from an unverified address holds its slot briefly.** The hold
  is `unverified_hold_minutes` in `/admin/settings` (default **15**, 1-120). The
  therapist is told, and the slot takes the normal hold, only when the client
  follows the login link. With SMTP off the booking files normally; a client
  with no login link left is refused rather than held.
- **The captcha is on by default** for a new practice (`captcha_on`, same page);
  a practice that already existed keeps the value it had. Turn it on if it is off.
- **Per-IP limit on booking** (`book_per_ip_per_hour`, default 10), and a
  **global cap on admin sign-ins**: 30 attempts in 15 minutes across all
  addresses, besides the 5 per address. While an attack from many addresses lasts
  it can keep the therapist from signing in; it clears 15 minutes after the
  attempts stop, and sessions already signed in are unaffected. Every Argon2 run
  (password checks, and the one-off dummy hash for unknown users) is in a thread,
  two at a time (each uses 64 MB), and the rest wait.
- **Delivery: a Retry button** on `/admin/delivery` for a dead message. Messages
  are retried over about 24 hours before they die, and a message stuck in
  `sending` is reclaimed after 15 minutes.
- **Login and request links confirm on GET and spend on POST**, so a mail
  scanner cannot use them up. The access log drops query strings and the
  `/healthz` line.
- **Backups:** freshness by file name; the 36 hour warning messages the
  therapist; a restart dumps if the newest dump is over 24 hours old (see
  [Backup](#backup)).
- **`TELEGRAM_WEBHOOK_SECRET` must be at least 16 characters** and is required in
  webhook mode. **Check its length before upgrading**: a shorter one stops the
  app from starting.
- **CSRF tokens are bound to the session and expire after 12 hours**; the cookie
  is `__Host-pb_csrf`. A form left open overnight is refused once; reload it.
- **Every response carries** `X-Content-Type-Options`, `X-Frame-Options`,
  `frame-ancestors 'none'` and `Referrer-Policy: no-referrer`. `/readyz` answers
  `{"status":"ok"}` or `{"status":"fail"}` and nothing more. The configuration
  upload is cut off at 5 MB while it streams (413).
- **Memory limits:** web 512 MB, worker 384 MB, database 512 MB. A container over
  its limit is killed and restarted, so the numbers are generous against the
  measured 181 / 179 / 27 MB.
- **Postgres timeouts:** a transaction idle for 60 s is ended, a lock wait over
  10 s fails, a statement over 30 s is cancelled, and a statement over 500 ms is
  logged (without its parameters, which are client data). `pg_dump` sets its own
  statement timeout and the migrations run without these three timeouts. The pool recycles
  connections after 30 minutes.
- **Containers** drop all capabilities (the database and backup add back only
  what they need to fix ownership), cannot gain privileges, log to three 10 MB
  files each, and `web` and `worker` get 60 s to stop (`web` now receives
  SIGTERM). `cloudflared` is pinned to `2026.11.0`. The backup container no longer
  receives `.env`, only the five variables it reads. `caddy` and `cloudflared`
  are not capability-dropped.
- **The retention jobs** (content purge, token, revision and error-event
  pruning) run hourly, not every 20 s.
- **The image is the runtime only** (see [Development](#development) for tests).

## Operations

### Day to day

Everything the therapist changes is in the admin UI, not in `.env`:
availability, booking mode, prices, session types, reminder offsets, the
timezone list, retention, and all practice content.

On a phone the top of every admin page carries the five daily pages — Today,
Requests, Schedule, Slots, Waitlist — and **More** (`/admin/more`), which lists
the rest; a computer shows every page in one row. Tables become cards below
58rem, the week grid shows one day at a time below 40rem, and the Slots list is
folded by day.

| Page | For |
|---|---|
| `/admin` (Today) | Who is waiting, with Approve on the row; today's and tomorrow's sessions; delivery and waitlist counts; the two daily switches |
| `/admin/requests` | The list (opens on "Needs you"): approve, propose another time, reject, cancel; `?view=grid` is the week Schedule with free and blocked times, Block day, **Copy this week to next week** and named week templates |
| `/admin/waitlist` | Mark contacted, converted, closed |
| `/admin/slots` | Create slots for a range of dates, block a date range, the upcoming slots by day |
| `/admin/more` | The other pages, one line each |
| `/admin/content` | Edit blocks, preview per channel, roll back |
| `/admin/translations` | Reword UI strings; see what is missing per language |
| `/admin/delivery` | Whether a message actually went out; **Retry** on a dead one |
| `/admin/clients` | The practice's people; `/admin/privacy` exports or erases one |
| `/admin/maintenance` | Export and import the configuration; download a backup |
| `/admin/help` | The admin guide, in English or Russian |
| `/admin/status` | The traffic light: what is working, and what to tell your IT person |
| `/admin/settings` | Booking rules, her photo and languages, the unverified-email hold, per-address booking limit, captcha, theme |

Telegram keeps a reduced admin surface for when you are away from a desk:
`/admin` in the bot lists open requests and toggles availability, and the
buttons on a notification approve, propose, reject and cancel. Content and
settings are web-only and reply with a link.

### Configuration: export and import

`/admin/maintenance` downloads everything typed into the admin UI as one JSON
file — settings, session types, timezones, topics, blocks, translations,
her photo and named week templates. It
carries **no client data**, so unlike a database dump it is safe to keep in a
password manager or send to whoever is helping you rebuild.

Importing it into a fresh installation reproduces the practice's voice without
retyping it. Import **merges**: it adds and updates what the file names and
never deletes anything the file leaves out. A changed page keeps its previous
text in the usual revision history, so a bad import is undone from
`/admin/content` per block.

Preview first — the button reports exactly what would change and writes
nothing. Then choose the file again and press Import.

Availability slots are deliberately not in the file: they are dated, and last
winter's Tuesdays mean nothing in a fresh install. Recreate a week from
`/admin/slots`.

### Backup

The `backup` container dumps the database once a day into `BACKUP_DIR` on the
host (default `./backups`), keeps `BACKUP_RETENTION_DAYS` of them, and needs no
setup beyond `docker compose up -d`. It runs the same `postgres:16` image as the
database, so `pg_dump` can never be older than the server it dumps.

**The dump contains clients' problem text.** The directory is created `0700`,
but encrypting the files and copying them off-host is still your job — the
container cannot know where "off-host" is.

```bash
docker compose logs backup          # when the last one ran, and whether it worked
ls -la backups/                     # what is on disk
```

**Freshness is read from the file's name, not its time stamp.** A dump is
`psychobooking-YYYY-MM-DD.dump` (a date-only name counts from `BACKUP_HOUR_UTC`)
or `...-HHMMSS.dump`. Copying the folder or restoring files resets every
modification time, which used to make a months-old dump look fresh. The health
check warns at 36 hours and fails at 7 days by that date, and **the 36 hour
warning now messages the therapist, as the failure does**. When the backup
container starts and the newest dump's name is over 24 hours old (or there is
none), it takes a dump at once instead of waiting for the next night.

They are also listed at `/admin/maintenance` with a download link, for a
therapist with no shell access on the server.

There is deliberately no "back up now" button and no restore button: restoring
overwrites every booking made since the dump, which is not a thing to do behind
one click in a browser.

### Restore

Stop the application first, so nothing writes while the database is being
replaced:

```bash
docker compose stop web worker
docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists < backups/psychobooking-2026-01-31.dump
docker compose start web worker
```

If your shell mangles binary on a redirect (Git Bash on Windows does), copy the
file in and restore from a path instead:

```bash
docker cp backups/psychobooking-2026-01-31.dump "$(docker compose ps -q db)":/tmp/restore.dump
docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists /tmp/restore.dump
```

### Rehearse the restore

**A backup that has never been restored is a hypothesis.** Do this on a scratch
database, without touching the live one:

```bash
# 1. Use the newest dump the backup container took (or take one now).
docker compose exec -T db pg_dump -U psycho -Fc psychobooking > /tmp/rehearsal.dump

# 2. Restore it into a throwaway database.
docker compose exec -T db createdb -U psycho scratch_restore
docker compose exec -T db pg_restore -U psycho -d scratch_restore --clean --if-exists < /tmp/rehearsal.dump

# 3. Start the application against it and confirm it answers.
docker compose run --rm -e DATABASE_URL=postgresql+asyncpg://psycho:$POSTGRES_PASSWORD@db:5432/scratch_restore \
  web python -c "
import asyncio
from sqlalchemy import text
from app.db import unit_of_work
async def main():
    async with unit_of_work() as s:
        print('practice:', (await s.execute(text('SELECT name FROM practice'))).scalar_one())
        print('requests:', (await s.execute(text('SELECT count(*) FROM booking_request'))).scalar_one())
asyncio.run(main())
"

# 4. Throw it away.
docker compose exec -T db dropdb -U psycho scratch_restore
```

### Upgrading

```bash
git pull
docker compose up -d --build
```

`web` runs `alembic upgrade head` before serving. Take a dump first.

---

## Health

| Path | Meaning |
|---|---|
| `/healthz` | Liveness. Does not touch the database — the Compose healthcheck gates `worker` on it. |
| `/readyz` | Readiness. 200 when the database answers, 503 otherwise. |

### The traffic light

Every admin page carries a coloured dot linking to `/admin/status`. **Red means
clients are affected right now; amber means something will break.** The
therapist's own queue is never coloured — a long list of pending requests is
work, not a fault.

Each check shows two lines: one for the therapist, about consequences, and one
technical line to read down the phone. When it goes red the therapist gets a
Telegram message, so nobody has to be watching the page.

The checks and their thresholds are in `IMPLEMENTATION.md` §16.9. The worker
runs them every pass and writes the result to `STATE_DIR/status.json`:

```bash
cat state/status.json | python -m json.tool
```

That file is deliberately not a database row. When the database is the thing
that broke, it is still readable — and so is this:

```bash
docker compose logs worker --tail 50
```

### One thing to set up outside the box

A green dot means *everything visible from inside this server is fine*. It can
never tell you the site is reachable, because if the whole stack is down there
is nothing left to draw a dot — or to send a Telegram message.

**Point a free external uptime check at `https://your-domain/readyz`** (
healthchecks.io, UptimeRobot, or a one-line cron on another machine). It returns
200 when the database answers and 503 otherwise. This is the only monitoring
that survives the server going away, and it takes about two minutes to set up.

---

## Development

The deployed image holds the application and its runtime requirements only: no
compiler, no pytest, mypy or ruff, no `tests/`. The tools live in
`requirements-dev.txt` and in the Dockerfile's `test` stage, which CI builds too.

```bash
docker build --target test -t newpbot-test .      # runtime image + dev tools + tests/
```

Tests need a PostgreSQL **whose name ends in `_test`** — the suite refuses any
other database, because some end-to-end tests commit like production:

```bash
docker network create np-test
docker run -d --name np-pg --network np-test -e POSTGRES_DB=psychobooking_test \
    -e POSTGRES_USER=psycho -e POSTGRES_PASSWORD=psycho postgres:16
docker run --rm --network np-test -v "$PWD/docs:/app/docs:ro" \
    -e DATABASE_URL=postgresql+asyncpg://psycho:psycho@np-pg:5432/psychobooking_test \
    -e SECRET_KEY=ci-secret-key-that-is-at-least-32-bytes-long -e BASE_URL=https://ci.example.test \
    -e TELEGRAM_BOT_TOKEN=0000000000:ci-token -e TELEGRAM_BOT_USERNAME=ci_bot \
    -e TELEGRAM_WEBHOOK_SECRET=ci-webhook-secret -e TELEGRAM_ADMIN_IDS=1 \
    -e ADMIN_USERNAME=admin -e ADMIN_PASSWORD=ci-admin-password \
    newpbot-test sh -c 'alembic upgrade head && python -m app.seed && pytest -q'
```

For an edit-and-run loop use the dev overlay, which builds the `test` stage and
mounts the working tree over the copies in the image:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml build web
docker compose -f docker-compose.yml -f docker-compose.dev.yml exec web pytest tests/core -q
docker compose -f docker-compose.yml -f docker-compose.dev.yml exec web ruff check app tests
docker compose -f docker-compose.yml -f docker-compose.dev.yml exec web mypy app
```

Tests run against real PostgreSQL. **Never** substitute SQLite — the schema
depends on native enums, arrays, partial indexes, `NULLS NOT DISTINCT`, and
`FOR UPDATE SKIP LOCKED`. Do not point the end-to-end tests at a live database.

### Migrations

```bash
docker compose exec web alembic revision --autogenerate -m "short description"
docker compose exec web alembic upgrade head
docker compose exec web alembic heads
docker compose exec web alembic check
```

`alembic heads` **must** print exactly one head — check after every revision;
the previous codebase branched here and it cost a rewrite. `alembic check`
printing "No new upgrade operations detected" means the models and the migration
still agree.

---

## Layout

```
app/core/        domain models, enums, events, policies, services   <- business logic
app/render/      markdown emitters, intent catalogue, message rendering
app/channels/    telegram/, email/, web/ -- thin adapters only
app/worker/      the loop and its jobs
locales/         ru.yaml, hy.yaml, en.yaml -- seed source of truth
alembic/         one linear history, one head
tests/core/      no channel imports allowed
```

Nothing under `app/core/` may import `fastapi`, `aiogram`, `jinja2`,
`aiosmtplib`, or `nh3`. `tests/core/test_architecture.py` enforces it. If that
test fails, fix the code — never the test.

---

## Privacy

The sensitive field is the free-text problem description a client writes.
Combined with a stable identifier that is health-related information about an
identifiable person, so:

- It never appears in logs, email payloads, or audit `meta` — only in the
  authenticated admin UI and your own Telegram.
- A retention job nulls it, along with negotiation bodies and contact notes, a
  configurable number of months after a session or a terminal status (default
  12). The rows stay, so statistics survive.
- `/admin/clients` exports everything held about one person, or erases them:
  identities, tokens and half-finished flows are deleted, every free-text field
  is nulled, confirmed sessions are cancelled so no reminder fires at somebody
  who is gone, and the bookings remain for statistics with `erased_at` set.
- Backups contain it. Encrypt them and keep them off-host.

This is proportionate handling, not legal advice. If the practice takes EU
clients at volume, the question deserves an answer from someone qualified.
