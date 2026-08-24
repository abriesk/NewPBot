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

**Status: M7 (admin web) complete.** Milestones run M0 → M9
(`IMPLEMENTATION.md` §19) and are worked in order.

## Requirements

Docker and Docker Compose. Nothing else — Python, PostgreSQL, and every
dependency live in the image.

## Setup

```bash
cp .env.example .env
```

Fill in `.env`. At minimum you need `POSTGRES_PASSWORD`, `SECRET_KEY`,
`BASE_URL`, the four `TELEGRAM_*` values, `ADMIN_USERNAME`, and
`ADMIN_PASSWORD`. Generate secrets with `openssl rand -hex 32`.

`.env` is git-ignored and must stay that way. Only `.env.example` is committed.

## Running

The default `tls` profile runs Caddy in front of the app; it obtains and renews
a Let's Encrypt certificate for `DOMAIN` automatically. Set `DOMAIN` and
`ACME_EMAIL` first.

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

`DOMAIN` and `ACME_EMAIL` are needed only by the `tls` profile, which refuses to
start without them. Leave them blank under `plain` or `cloudflared` -- a tunnel
hands you a hostname without you owning a domain.

The Telegram webhook needs a publicly reachable HTTPS URL. Running `plain` with
nothing terminating TLS in front, the app refuses to register a webhook and says
so; set `TELEGRAM_MODE=polling` for local development.

```bash
docker compose logs -f web worker
```

## Health

| Path | Meaning |
|---|---|
| `/healthz` | Liveness. Does not touch the database — the Compose healthcheck gates `worker` on it. |
| `/readyz` | Readiness. 200 when the database answers, 503 otherwise. |

## Development

```bash
docker compose exec web pytest
docker compose exec web pytest tests/core -q
docker compose exec web pytest -k architecture
docker compose exec web ruff check app tests
docker compose exec web mypy app
```

Tests run against real PostgreSQL. **Never** substitute SQLite — the schema
depends on native enums, arrays, and `FOR UPDATE SKIP LOCKED`.

### Migrations

```bash
docker compose exec web alembic revision --autogenerate -m "short description"
docker compose exec web alembic upgrade head
docker compose exec web alembic heads
```

`alembic heads` **must** print exactly one head. Check it after every revision —
the previous codebase branched here and it cost a rewrite.

After changing a model, confirm the migration still matches it:

```bash
docker compose exec web alembic check
```

"No new upgrade operations detected" means the schema and the models agree.

`web` runs `alembic upgrade head` and then `python -m app.seed` before serving;
`worker` waits on `web`'s healthcheck, so exactly one process does either. The
seed is idempotent and re-runs harmlessly on every boot; it never overwrites a
translation the therapist has edited. If `web` is ever scaled beyond one
replica, migrations must move to a separate one-shot service.

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

## Backup and restore

Backup is a host cron entry, not a container. The dump contains clients'
problem text: encrypt it and keep it off-host.

```bash
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > "/backups/psychobooking-$(date +%F).dump"
```

Nightly, retained 30 days. With content in the database and no application
volumes, this dump plus `.env` is the complete state of the service.

Restore:

```bash
docker compose down web worker
docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists < backup.dump
docker compose up -d web worker
```

Rehearse a restore onto a scratch database. A backup that has never been
restored is a hypothesis.
