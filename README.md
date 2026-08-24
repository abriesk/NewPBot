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
| `TELEGRAM_WEBHOOK_SECRET` | Another `openssl rand -hex 32`. |
| `TELEGRAM_ADMIN_IDS` | Your own Telegram user id. Without it the bot has no admin. |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | The web admin login. Hashed at first start; the plaintext is never stored. |

`DOMAIN` and `ACME_EMAIL` are needed only by the `tls` profile, which refuses to
start without them. Leave them blank under `plain` or `cloudflared` — a tunnel
hands you a hostname without you owning a domain.

Leave `SMTP_HOST` empty to run without email: no email identities are created,
no email is queued, and clients sign in through Telegram. Set it (with
`SMTP_FROM`) to turn the channel on.

`.env` is git-ignored and must stay that way. Only `.env.example` is committed.

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

## Operations

### Day to day

Everything the therapist changes is in the admin UI, not in `.env`:
availability, booking mode, prices, session types, reminder offsets, the
timezone list, retention, and all practice content.

| Page | For |
|---|---|
| `/admin/requests` | Approve, propose another time, reject, cancel |
| `/admin/waitlist` | Mark contacted, converted, closed |
| `/admin/slots` | Create a week at a time, block, delete |
| `/admin/content` | Edit blocks, preview per channel, roll back |
| `/admin/translations` | Reword UI strings; see what is missing per language |
| `/admin/delivery` | Whether a message actually went out |
| `/admin/clients` | Export everything about one person, or erase them |
| `/admin/settings` | The knobs above |

Telegram keeps a reduced admin surface for when you are away from a desk:
`/admin` in the bot lists open requests and toggles availability, and the
buttons on a notification approve, propose, reject and cancel. Content and
settings are web-only and reply with a link.

### Backup

A host cron entry, not a container. **The dump contains clients' problem text:
encrypt it and keep it off-host.**

```bash
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > "/backups/psychobooking-$(date +%F).dump"
```

Nightly, retained 30 days. With content in the database and no application
volumes, this dump plus `.env` is the complete state of the service.

### Restore

```bash
docker compose down web worker
docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists < backup.dump
docker compose up -d web worker
```

### Rehearse the restore

**A backup that has never been restored is a hypothesis.** Do this on a scratch
database, without touching the live one:

```bash
# 1. Take a dump.
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

---

## Development

```bash
docker compose exec web pytest
docker compose exec web pytest tests/core -q
docker compose exec web pytest -k architecture
docker compose exec web ruff check app tests
docker compose exec web ruff format --check app tests
docker compose exec web mypy app
```

Tests run against real PostgreSQL. **Never** substitute SQLite — the schema
depends on native enums, arrays, partial indexes, `NULLS NOT DISTINCT`, and
`FOR UPDATE SKIP LOCKED`.

Some end-to-end tests commit like production rather than rolling back, and clean
up after themselves. They are safe to run repeatedly against a development
database, but do not point them at a live one.

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
