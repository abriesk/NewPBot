# CLAUDE.md

Project instructions for Claude Code. Read this first, every session.

## What this is

A booking service for a single psychotherapist's practice. Clients read practice
information and request consultations through **two interactive channels** (a web
app and a Telegram bot) plus **one outbound channel** (email). The therapist
approves every booking.

The whole point of the architecture is that booking logic lives in **one place**
and channels are thin adapters over it. If you find yourself writing a scheduling
rule inside a Telegram handler or a FastAPI route, stop — it belongs in
`app/core/services/`.

## Specification

Two documents. **Read both before writing code.**

- `docs/DESIGN.md` — reasoning, trade-offs, rejected alternatives. Read it to
  understand *why* something is the way it is.
- `docs/IMPLEMENTATION.md` — **normative**. Schema, state machines, routes,
  worker jobs, milestones, acceptance criteria. When in doubt, this wins.

If the two disagree, `IMPLEMENTATION.md` wins and the disagreement is a bug —
tell me about it, don't silently pick one.

If the spec is ambiguous or silent on something you need: **ask me**. Do not
invent a feature, a table, or an endpoint that isn't specified. Do not "improve"
the design mid-implementation.

## Current state

> Update this line as work progresses. It is the first thing to check each session.

**Milestone: M10 complete. M11 (health signal) specified in `IMPLEMENTATION.md` §16.8–§16.10, not yet implemented.**

Milestones run M0 → M11 (`IMPLEMENTATION.md` §19). Work them **in order**. Each
one's acceptance criteria must pass before starting the next. Do not implement
later milestones early — the ordering exists so the core is fully testable before
any channel exists.

## Hard rules

These are the ones that are easy to break and expensive to unwind. Violating any
of them is a bug even if the tests pass.

1. **Nothing under `app/core/` may import** `fastapi`, `aiogram`, `jinja2`,
   `aiosmtplib`, or `nh3`. There is an architecture test enforcing this. If it
   fails, fix the code, never the test.
2. **No direct sends.** Never call `bot.send_message()` or SMTP from a handler or
   route. Every outbound message is an `outbox_message` row written in the same
   transaction as the domain change that caused it.
3. **No in-memory scheduling.** APScheduler, Celery, and Redis are forbidden.
   Scheduled work is a database sweep in `app/worker/jobs/` using
   `FOR UPDATE SKIP LOCKED`.
4. **Timezone-aware UTC everywhere.** `datetime.utcnow()` must never appear.
   Use `datetime.now(UTC)` and `timestamptz` columns. Timezones are IANA names
   (`Europe/Moscow`), never UTC offset strings.
5. **`hy`, not `am`.** Armenian is `hy`. `am` is Amharic and must not appear
   anywhere in the codebase.
6. **Never `parse_mode='MarkdownV2'`.** Telegram output is HTML with the
   supported tag subset, produced by the emitter in `app/render/markdown.py`.
7. **Slot booking takes a row lock.** `SELECT … FROM slot WHERE id = :id FOR
   UPDATE` before any status check in `hold` or `book`. This is the only place a
   lost update would double-book a client.
8. **`problem_text` never leaves the admin UI.** Not in logs, not in email
   payloads, not in audit `meta`. Log identifiers, not content.
9. **`.env` is git-ignored.** Only `.env.example` is committed, with
   placeholders.
10. **Locale key parity.** `locales/en.yaml` is the normative key catalogue. Any
    key added must be added to all three files in the same change, and values
    carry no markup — no `<b>`, no Markdown emphasis.

## Commands

```bash
# stack
docker compose up -d                  # default profile: tls (needs DOMAIN, ACME_EMAIL)
COMPOSE_PROFILES=plain docker compose -f docker-compose.yml -f docker-compose.plain.yml up -d
docker compose logs -f web worker

# migrations — always review generated SQL before committing
docker compose exec web alembic revision --autogenerate -m "short description"
docker compose exec web alembic upgrade head
docker compose exec web alembic heads          # MUST print exactly one head

# tests
docker compose exec web pytest
docker compose exec web pytest tests/core -q   # core only, fastest signal
docker compose exec web pytest -k architecture # the import-boundary test

# lint / types
docker compose exec web ruff check app
docker compose exec web mypy app
```

Tests run against real PostgreSQL. **Never** substitute SQLite — the schema
depends on native enums, arrays, and `FOR UPDATE SKIP LOCKED`.

## Layout

```
app/core/        domain models, enums, events, policies, services   ← business logic lives here
app/render/      markdown emitters, intent catalogue, message rendering
app/channels/    telegram/, email/, web/ — thin adapters only
app/worker/      the loop and its jobs
locales/         ru.yaml, hy.yaml, en.yaml — seed source of truth
alembic/         one linear history, one head
tests/core/      no channel imports allowed
```

Full detail in `IMPLEMENTATION.md` §3.

## Working style

- **One milestone per session** where possible. Say which milestone you're on
  before starting, and stop at its acceptance criteria rather than rolling into
  the next.
- **Write the test with the code**, not after the milestone. Every state
  transition in §7 needs a test, including the ones that must be rejected.
- **Small commits**, one concern each. Conventional style: `feat(core): …`,
  `fix(telegram): …`, `chore(deps): …`.
- **Don't add dependencies** without asking. The stack in §2 is deliberate and
  short.
- **Don't create files outside the layout** in §3. No scratch scripts committed,
  no `NOTES.md`, no summary documents I didn't ask for.
- **Prefer editing over rewriting.** If a file needs restructuring, say so first.
- When something is genuinely underspecified, propose two options with the
  trade-off and let me pick. Don't pick silently and mention it later.

## Definition of done

A change is complete when all of these hold:

- Tests pass, including the architecture test
- `alembic heads` shows exactly one head
- New behaviour has a test; new keys exist in all three locale files
- No `datetime.utcnow`, no `am`, no direct sends, no forbidden imports
- The milestone's acceptance criteria in §19 are demonstrably met

## Out of scope

Do not build these, even where they'd fit naturally: payments,
client-initiated cancellation, calendar sync, WhatsApp, multi-practice
onboarding or switching, session notes, client file uploads, analytics beyond
the audit log.

`practice_id` exists on every table on purpose — one practice is served, and
practice switching must not be implemented.

## Known traps from the previous attempt

The v1.0 codebase this replaces failed in specific ways. Watch for the same
drift:

- A duplicated source tree (`app/app/…` alongside `app/…`) with diverging copies
  of the same modules. There is **one** tree.
- Branched Alembic history — two migrations claiming the same parent. Check
  `alembic heads` after every migration.
- Booking rules reimplemented separately in the bot and the web UI.
- Landing HTML dumped straight into `reply_html()`, breaking on Telegram's tag
  subset and 4096-character limit.
- A cross-process notification queue used as plumbing rather than durability.
  There is one ASGI ingress and one worker; the outbox exists for retries and
  atomicity, not for talking between processes.
