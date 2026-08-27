# Psychotherapy Booking Service — Implementation Specification

**Version:** 2.0
**Audience:** an implementing agent or developer.
**Read `DESIGN.md` first.** It explains the reasoning; this document is normative. Where they disagree, this document wins — and the disagreement is a bug worth reporting.

Requirements use RFC 2119 keywords: **MUST**, **MUST NOT**, **SHOULD**, **MAY**.

---

## 1. How to use this document

Work milestone by milestone (§19). Each milestone has acceptance criteria that **MUST** pass before the next begins. Do not implement later milestones early; the ordering exists so that the core is testable before any channel exists.

Do not invent features not specified here. Where this document says "configurable", it means an environment variable or a settings row, never a constant in a module.

---

## 2. Stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.12+ | Timezone-aware `datetime.now(UTC)`; `datetime.utcnow()` **MUST NOT** appear anywhere |
| Web framework | FastAPI | Single ASGI app: client UI, admin UI, internal API, Telegram webhook |
| Templates | Jinja2 + HTMX | No SPA, no build step |
| ORM | SQLAlchemy 2.0, async, `Mapped[]` declarative style | No legacy `Column(...)` class attributes |
| Driver | `asyncpg` | |
| Migrations | Alembic | Linear history; one head at all times |
| Database | PostgreSQL 16 | |
| Telegram | aiogram 3 | Adapter only; no aiogram FSM — state lives in the database |
| Markdown | `markdown-it-py` | Parse to tokens, emit per channel |
| Sanitising | `nh3` (or `bleach`) | Web HTML output only |
| Passwords | `argon2-cffi` | |
| Email | `aiosmtplib` | Generic SMTP from configuration |
| Validation | Pydantic v2 | Settings and API schemas |
| Tests | `pytest`, `pytest-asyncio`, `time-machine` | |
| Container | Docker Compose | Services: `web`, `worker`, `db` |

**Forbidden:** APScheduler, Celery, Redis, any in-memory job scheduler. Scheduled work is a database sweep (§14).

---

## 3. Repository layout

```
.
├── app/
│   ├── core/                     # NO imports from fastapi, aiogram, jinja2, aiosmtplib
│   │   ├── models.py             # SQLAlchemy models
│   │   ├── enums.py
│   │   ├── events.py             # domain event dataclasses
│   │   ├── errors.py             # domain exceptions
│   │   ├── policies.py           # booking mode resolution, cancellation window, expiry
│   │   └── services/
│   │       ├── clients.py        # identity resolution, linking, tokens
│   │       ├── content.py
│   │       ├── slots.py
│   │       ├── booking.py        # request lifecycle + negotiation
│   │       ├── waitlist.py
│   │       ├── settings.py
│   │       ├── translations.py
│   │       └── notifications.py  # domain event -> outbox rows
│   ├── render/
│   │   ├── markdown.py           # AST -> per-channel emitters
│   │   ├── intents.py            # intent catalogue + payload schemas
│   │   └── messages.py           # intent + locale + channel -> RenderedMessage
│   ├── channels/
│   │   ├── base.py               # Transport protocol
│   │   ├── telegram/             # webhook router, keyboards, transport
│   │   └── web/                  # client + admin routers, templates, static, guides/
│   │   └── web/                  # client + admin routers, templates, static
│   ├── worker/
│   │   ├── main.py               # loop
│   │   └── jobs/                 # outbox, holds, expiry, reminders, completion, retention, status
│   ├── db.py                     # engine, session factory, unit of work
│   ├── config.py                 # pydantic-settings
│   └── main.py                   # FastAPI app assembly
├── locales/                      # ru.yaml, hy.yaml, en.yaml — seed source of truth
├── alembic/
├── tests/
│   ├── core/                     # no channel imports
│   ├── channels/
│   └── e2e/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── .gitignore                    # MUST contain .env
└── README.md
```

**Architectural constraint (enforced by test, §17):** no module under `app/core/` may import `fastapi`, `aiogram`, `jinja2`, `aiosmtplib`, or `nh3`.

---

## 4. Configuration

All configuration is environment variables, loaded through Pydantic settings. `.env.example` **MUST** be committed with placeholder values; `.env` **MUST** be in `.gitignore`.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `DATABASE_URL` | yes | — | `postgresql+asyncpg://…` |
| `SECRET_KEY` | yes | — | Token and cookie signing; ≥32 bytes |
| `BASE_URL` | yes | — | Public https origin, no trailing slash |
| `TELEGRAM_BOT_TOKEN` | yes | — | |
| `TELEGRAM_BOT_USERNAME` | yes | — | For `t.me` deep links |
| `TELEGRAM_WEBHOOK_SECRET` | yes | — | Sent as `X-Telegram-Bot-Api-Secret-Token` |
| `TELEGRAM_WEBHOOK_PATH` | no | random at install | Include an unguessable segment |
| `TELEGRAM_MODE` | no | `webhook` | `webhook` \| `polling` (development only) |
| `TELEGRAM_ADMIN_IDS` | yes | — | Comma-separated user IDs |
| `SMTP_HOST` | no | — | Email channel disabled if unset |
| `SMTP_PORT` | no | `587` | |
| `SMTP_USER` | no | — | |
| `SMTP_PASSWORD` | no | — | |
| `SMTP_FROM` | no | — | **SHOULD** match the authenticated account's domain (SPF/DMARC) |
| `SMTP_STARTTLS` | no | `true` | |
| `PRACTICE_NAME` | no | `Practice` | Seed only |
| `PRACTICE_TIMEZONE` | no | `Asia/Yerevan` | IANA; seed only |
| `DEFAULT_LANGUAGE` | no | `ru` | Seed only |
| `ADMIN_USERNAME` | yes | — | Seed only |
| `ADMIN_PASSWORD` | yes | — | Seed only; hashed at first startup, never stored plain |
| `WORKER_POLL_SECONDS` | no | `20` | |
| `LOG_LEVEL` | no | `INFO` | |
| `COMPOSE_PROFILES` | no | `tls` | `tls` \| `plain` \| `plain,cloudflared` (§16) |
| `DOMAIN` | conditional | — | Public hostname; required by the `tls` profile |
| `ACME_EMAIL` | conditional | — | Let's Encrypt contact address; required by the `tls` profile |
| `TUNNEL_TOKEN` | conditional | — | Required by the `cloudflared` profile only |
| `TRUST_PROXY_HEADERS` | no | `true` | Honour `X-Forwarded-Proto` / `X-Forwarded-For` |
| `BACKUP_DIR` | no | `./backups` | Compose only — host path bind-mounted into `backup` (read-write) and `web` (read-only) |
| `BACKUP_PATH` | no | `/backups` | Container path `web` lists dumps from; the other end of the same mount |
| `BACKUP_HOUR_UTC` | no | `3` | Compose only — hour of day the sidecar dumps; `0`–`23` |
| `BACKUP_RETENTION_DAYS` | no | `30` | Compose only — dumps older than this are pruned after each successful run |
| `STATE_DIR` | no | `./state` | Compose only — host path for the status file; `worker` read-write, `web` read-only |
| `STATE_PATH` | no | `/state` | Container path both processes use; the other end of the same mount (§16.8) |

If `SMTP_HOST` is unset the email channel **MUST** be disabled cleanly: no email identities can be created, the web UI **MUST** require Telegram login, and outbox rows for the `email` channel **MUST NOT** be created.

Everything else — availability, booking mode, prices, reminder offsets, retention — is a database setting edited in the admin UI, **not** an environment variable.

---

## 5. Enums

```python
Channel          = telegram | email | web | whatsapp        # whatsapp declared, unimplemented
Modality         = online | onsite
RequestStatus    = pending | negotiating | confirmed | rejected | expired | cancelled | completed
SlotStatus       = available | held | booked | blocked
NegotiationKind  = proposal | counter | accept | decline | note
SenderType       = admin | client | system
WaitlistStatus   = new | contacted | converted | closed
BookingMode      = slots | negotiation
TokenPurpose     = login | link_channel | view_request
OutboxStatus     = pending | sending | sent | failed | dead
ReminderState    = scheduled | sent | skipped | cancelled
ActorType        = admin | client | system
ContentBlockKind = text | link_button
CheckState       = ok | warn | fail                          # rendered as green/amber/red (§16.8)
ErrorSource      = web | worker
```

Persist enums as **PostgreSQL native enum types**, values lowercase exactly as above. `RequestType` from v1.0 does not exist; session types are rows (§6.4). `CheckState` is the exception: it is never stored in the database, only written to the status file (§16.8), so it needs no type and no migration.

The first migration **MUST** create them explicitly, before any table that references them:

```sql
CREATE TYPE channel            AS ENUM ('telegram','email','web','whatsapp');
CREATE TYPE modality           AS ENUM ('online','onsite');
CREATE TYPE request_status     AS ENUM ('pending','negotiating','confirmed','rejected',
                                        'expired','cancelled','completed');
CREATE TYPE slot_status        AS ENUM ('available','held','booked','blocked');
CREATE TYPE negotiation_kind   AS ENUM ('proposal','counter','accept','decline','note');
CREATE TYPE sender_type        AS ENUM ('admin','client','system');
CREATE TYPE waitlist_status    AS ENUM ('new','contacted','converted','closed');
CREATE TYPE booking_mode       AS ENUM ('slots','negotiation');
CREATE TYPE token_purpose      AS ENUM ('login','link_channel','view_request');
CREATE TYPE outbox_status      AS ENUM ('pending','sending','sent','failed','dead');
CREATE TYPE reminder_state     AS ENUM ('scheduled','sent','skipped','cancelled');
CREATE TYPE actor_type         AS ENUM ('admin','client','system');
CREATE TYPE content_block_kind AS ENUM ('text','link_button');
CREATE TYPE error_source       AS ENUM ('web','worker');
```

In SQLAlchemy, declare these with `sqlalchemy.Enum(PyEnum, name='<type_name>', native_enum=True, create_type=True)`; `values_callable` **MUST** be set so the *values* (lowercase) are persisted, not the Python member names.

---

## 6. Schema

Given as PostgreSQL DDL for precision. Implement as SQLAlchemy 2.0 declarative models and generate Alembic migrations from them; the resulting schema **MUST** match this. All timestamps are `timestamptz` and stored in UTC. Every table carries `practice_id`.

### 6.1 Practice and settings

```sql
CREATE TABLE practice (
    id                      SERIAL PRIMARY KEY,
    name                    TEXT NOT NULL,
    default_language        TEXT NOT NULL DEFAULT 'ru',
    timezone                TEXT NOT NULL DEFAULT 'Asia/Yerevan',   -- IANA
    clinic_onsite_url       TEXT,
    online_meeting_url      TEXT,                                    -- default room for online sessions
    availability_on         BOOLEAN NOT NULL DEFAULT TRUE,
    booking_mode            booking_mode NOT NULL DEFAULT 'slots',
    fallback_to_negotiation BOOLEAN NOT NULL DEFAULT TRUE,
    negotiation_enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    auto_confirm_slots      BOOLEAN NOT NULL DEFAULT FALSE,
    slot_hold_minutes       INTEGER NOT NULL DEFAULT 15,
    pending_expiry_hours    INTEGER NOT NULL DEFAULT 48,
    cancel_window_hours     INTEGER NOT NULL DEFAULT 24,
    reminder_offsets_min    INTEGER[] NOT NULL DEFAULT '{1440,60}',
    retention_months        INTEGER NOT NULL DEFAULT 12,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Exactly one row is seeded at install. `reminder_offsets_min` replaces v1.0's two booleans; an empty array disables reminders.

### 6.2 Clients and identity

```sql
CREATE TABLE client (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    practice_id  INTEGER NOT NULL REFERENCES practice(id),
    display_name TEXT,
    language     TEXT NOT NULL,
    timezone     TEXT,                       -- IANA, nullable
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    erased_at    TIMESTAMPTZ                 -- set by the erasure operation
);

CREATE TABLE identity (
    id          BIGSERIAL PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practice(id),
    client_id   UUID NOT NULL REFERENCES client(id) ON DELETE CASCADE,
    channel     channel NOT NULL,
    external_id TEXT NOT NULL,               -- Telegram user ID as text, or lowercased email
    verified_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (practice_id, channel, external_id)
);
CREATE INDEX ON identity (client_id);

CREATE TABLE auth_token (
    id          BIGSERIAL PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practice(id),
    client_id   UUID REFERENCES client(id) ON DELETE CASCADE,
    purpose     token_purpose NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,        -- sha256 of the raw token
    payload     JSONB,                       -- e.g. {"email": "...", "request_uuid": "..."}
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Raw tokens **MUST NOT** be stored. A token is valid only if `used_at IS NULL AND expires_at > now()`, and consuming it sets `used_at` in the same transaction as the action it authorises.

### 6.3 Admin

```sql
CREATE TABLE admin_user (
    id            SERIAL PRIMARY KEY,
    practice_id   INTEGER NOT NULL REFERENCES practice(id),
    username      TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    email         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    UNIQUE (practice_id, username)
);

CREATE TABLE admin_session (
    id            BIGSERIAL PRIMARY KEY,
    admin_user_id INTEGER NOT NULL REFERENCES admin_user(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ
);
```

### 6.4 Session types and slots

```sql
CREATE TABLE session_type (
    id                   SERIAL PRIMARY KEY,
    practice_id          INTEGER NOT NULL REFERENCES practice(id),
    code                 TEXT NOT NULL,          -- 'individual', 'couple'
    duration_min         INTEGER NOT NULL DEFAULT 60,
    price_amount_minor   INTEGER,                -- 5000 = 50.00
    price_currency       TEXT,                   -- ISO 4217
    price_display_override TEXT,                 -- wins over amount/currency when set
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order           INTEGER NOT NULL DEFAULT 0,
    UNIQUE (practice_id, code)
);

CREATE TABLE slot (
    id               BIGSERIAL PRIMARY KEY,
    practice_id      INTEGER NOT NULL REFERENCES practice(id),
    starts_at        TIMESTAMPTZ NOT NULL,
    duration_min     INTEGER NOT NULL DEFAULT 60,
    modality         modality,                   -- NULL = either
    status           slot_status NOT NULL DEFAULT 'available',
    hold_expires_at  TIMESTAMPTZ,
    held_by_request  BIGINT,                     -- FK added after booking_request exists
    booked_request   BIGINT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON slot (practice_id, status, starts_at);
-- NULLS NOT DISTINCT is required: modality IS NULL means "either", and without it
-- PostgreSQL treats every NULL as distinct, allowing unlimited duplicate slots.
CREATE UNIQUE INDEX slot_unique_offer ON slot (practice_id, starts_at, modality)
    NULLS NOT DISTINCT;

CREATE TABLE slot_session_type (
    slot_id         BIGINT NOT NULL REFERENCES slot(id) ON DELETE CASCADE,
    session_type_id INTEGER NOT NULL REFERENCES session_type(id) ON DELETE CASCADE,
    PRIMARY KEY (slot_id, session_type_id)
);
```

An empty `slot_session_type` set means the slot accepts **all** active session types.

Invariants:
- `status='held'` ⟺ `hold_expires_at IS NOT NULL AND held_by_request IS NOT NULL`
- `status='booked'` ⟺ `booked_request IS NOT NULL`
- `status IN ('available','blocked')` ⟹ `hold_expires_at IS NULL AND held_by_request IS NULL AND booked_request IS NULL`

Enforce with `CHECK` constraints.

### 6.5 Booking requests

```sql
CREATE TABLE booking_request (
    id                  BIGSERIAL PRIMARY KEY,
    uuid                UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    practice_id         INTEGER NOT NULL REFERENCES practice(id),
    client_id           UUID NOT NULL REFERENCES client(id),
    session_type_id     INTEGER NOT NULL REFERENCES session_type(id),
    modality            modality NOT NULL,
    status              request_status NOT NULL DEFAULT 'pending',
    source_channel      channel NOT NULL,
    slot_id             BIGINT REFERENCES slot(id),
    scheduled_start     TIMESTAMPTZ,
    scheduled_duration_min INTEGER,
    client_timezone     TEXT,                    -- IANA at time of request
    meeting_url         TEXT,                    -- overrides practice.online_meeting_url
    desired_time_text   TEXT,                    -- free-text path only
    problem_text        TEXT,
    contact_note        TEXT,                    -- preferred means of contact
    display_name        TEXT,                    -- as typed, unverified
    expires_at          TIMESTAMPTZ,             -- pending expiry
    confirmed_at        TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    cancelled_by        actor_type,
    cancellation_reason TEXT,
    rejected_reason     TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON booking_request (practice_id, status, created_at DESC);
CREATE INDEX ON booking_request (client_id);
CREATE INDEX ON booking_request (scheduled_start) WHERE status = 'confirmed';
```

`uuid` is the client-visible identifier and **MUST** appear in every admin notification.

Constraint: `status='confirmed'` requires `scheduled_start IS NOT NULL AND confirmed_at IS NOT NULL`.

Add the deferred slot foreign keys once this table exists:

```sql
ALTER TABLE slot ADD CONSTRAINT slot_held_by_fk
    FOREIGN KEY (held_by_request) REFERENCES booking_request(id) ON DELETE SET NULL;
ALTER TABLE slot ADD CONSTRAINT slot_booked_fk
    FOREIGN KEY (booked_request) REFERENCES booking_request(id) ON DELETE SET NULL;
```

### 6.6 Negotiation and waitlist

```sql
CREATE TABLE negotiation_message (
    id             BIGSERIAL PRIMARY KEY,
    request_id     BIGINT NOT NULL REFERENCES booking_request(id) ON DELETE CASCADE,
    sender         sender_type NOT NULL,
    kind           negotiation_kind NOT NULL,
    proposed_start TIMESTAMPTZ,
    body_text      TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON negotiation_message (request_id, created_at);

CREATE TABLE waitlist_entry (
    id           BIGSERIAL PRIMARY KEY,
    uuid         UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    practice_id  INTEGER NOT NULL REFERENCES practice(id),
    client_id    UUID NOT NULL REFERENCES client(id),
    problem_text TEXT,
    contact_note TEXT,
    status       waitlist_status NOT NULL DEFAULT 'new',
    admin_note   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    contacted_at TIMESTAMPTZ
);
```

Whose turn it is in a negotiation is **derived** from the last message's sender and **MUST NOT** be stored.

### 6.7 Content, translations, timezones

```sql
CREATE TABLE content_topic (
    id           SERIAL PRIMARY KEY,
    practice_id  INTEGER NOT NULL REFERENCES practice(id),
    code         TEXT NOT NULL,               -- 'work_terms', 'qualification', ...
    sort_order   INTEGER NOT NULL DEFAULT 0,
    show_in_menu BOOLEAN NOT NULL DEFAULT TRUE,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (practice_id, code)
);

CREATE TABLE content_block (
    id           BIGSERIAL PRIMARY KEY,
    practice_id  INTEGER NOT NULL REFERENCES practice(id),
    topic_id     INTEGER NOT NULL REFERENCES content_topic(id) ON DELETE CASCADE,
    lang         TEXT NOT NULL,
    position     INTEGER NOT NULL,
    kind         content_block_kind NOT NULL DEFAULT 'text',
    body_md      TEXT NOT NULL,
    link_url     TEXT,                        -- kind='link_button'
    is_published BOOLEAN NOT NULL DEFAULT TRUE,
    version      INTEGER NOT NULL DEFAULT 1,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (topic_id, lang, position)
);

CREATE TABLE content_block_revision (
    id         BIGSERIAL PRIMARY KEY,
    block_id   BIGINT NOT NULL REFERENCES content_block(id) ON DELETE CASCADE,
    version    INTEGER NOT NULL,
    body_md    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (block_id, version)
);

CREATE TABLE translation (
    id          BIGSERIAL PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practice(id),
    lang        TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (practice_id, lang, key)
);

CREATE TABLE timezone_option (
    id           SERIAL PRIMARY KEY,
    practice_id  INTEGER NOT NULL REFERENCES practice(id),
    iana_name    TEXT NOT NULL,               -- 'Asia/Yerevan'
    display_name TEXT NOT NULL,               -- 'Yerevan, Tbilisi, Dubai'
    sort_order   INTEGER NOT NULL DEFAULT 0,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (practice_id, iana_name)
);
```

Every write to `content_block` **MUST** insert the previous body into `content_block_revision` and increment `version`, in one transaction. Keep the most recent 20 revisions per block.

### 6.8 Outbox, reminders, audit

```sql
CREATE TABLE outbox_message (
    id              BIGSERIAL PRIMARY KEY,
    practice_id     INTEGER NOT NULL REFERENCES practice(id),
    channel         channel NOT NULL,
    address         TEXT NOT NULL,            -- Telegram chat ID or email address
    client_id       UUID REFERENCES client(id) ON DELETE SET NULL,
    admin_user_id   INTEGER REFERENCES admin_user(id) ON DELETE SET NULL,
    request_id      BIGINT REFERENCES booking_request(id) ON DELETE SET NULL,
    intent_key      TEXT NOT NULL,
    payload         JSONB NOT NULL,
    locale          TEXT NOT NULL,
    dedupe_key      TEXT UNIQUE,
    status          outbox_status NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ
);
CREATE INDEX ON outbox_message (status, next_attempt_at);

CREATE TABLE outbox_attempt (
    id           BIGSERIAL PRIMARY KEY,
    message_id   BIGINT NOT NULL REFERENCES outbox_message(id) ON DELETE CASCADE,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok           BOOLEAN NOT NULL,
    error        TEXT
);

CREATE TABLE reminder (
    id         BIGSERIAL PRIMARY KEY,
    request_id BIGINT NOT NULL REFERENCES booking_request(id) ON DELETE CASCADE,
    offset_min INTEGER NOT NULL,
    due_at     TIMESTAMPTZ NOT NULL,
    state      reminder_state NOT NULL DEFAULT 'scheduled',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    fired_at   TIMESTAMPTZ,
    UNIQUE (request_id, offset_min)
);
CREATE INDEX ON reminder (state, due_at);

CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practice(id),
    actor_type  actor_type NOT NULL,
    actor_id    TEXT,
    action      TEXT NOT NULL,                -- 'request.confirm', 'content.update', ...
    entity_type TEXT,
    entity_id   TEXT,
    meta        JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`payload` **MUST NOT** contain `problem_text` or negotiation bodies for the `email` channel (§13.4).

### 6.9 Error events

The one thing the domain tables cannot record: an exception that left no other trace (DESIGN.md §22.2).

```
error_event
  id            bigserial pk
  practice_id   int fk -> practice
  source        error_source not null        -- web | worker
  kind          text not null                -- exception class, e.g. 'OperationalError'
  location      text not null                -- 'app.worker.jobs.outbox:184' or a job name
  at            timestamptz not null default now()
  index (at)
```

- Written where the exception is **already** being caught: the ASGI exception handler in `app/main.py`, and §14's per-job `except` in the worker loop.
- Recording **MUST** be best-effort and **MUST NOT** mask the original exception. If the insert fails — most obviously because the database is what broke — the original error propagates unchanged and the failure is logged.
- `kind` and `location` only. The exception's **message and traceback MUST NOT be stored**: either can carry an email address or a fragment of `problem_text` (hard rule 8, DESIGN.md §22.5). The logs keep the detail.
- Pruned after 30 days by `prune_error_events` (§14).

---

## 7. State machines

### 7.1 Booking request

| From | Event | To | Side effects (same transaction) |
|---|---|---|---|
| — | `submit` | `pending` | Slot (if any) `held`→`booked` reservation intent; `expires_at = now + pending_expiry_hours`; emit `request.submitted` |
| `pending` | `admin_approve` | `confirmed` | `scheduled_start` set; slot→`booked`; `confirmed_at`; create reminders; emit `request.confirmed` |
| `pending` | `admin_propose` | `negotiating` | Insert `negotiation_message(admin, proposal)`; release slot if the proposal names a different time; emit `request.proposal` |
| `pending` | `admin_reject` | `rejected` | Release slot; emit `request.rejected` |
| `pending` | `expire` (worker) | `expired` | Release slot; emit `request.expired` |
| `negotiating` | `client_accept` | `confirmed` | `scheduled_start` = last admin proposal; book matching slot if one exists; create reminders; emit `request.confirmed` |
| `negotiating` | `client_counter` | `negotiating` | Insert `negotiation_message(client, counter)`; emit `request.counter` |
| `negotiating` | `admin_propose` | `negotiating` | Insert message; emit `request.proposal` |
| `negotiating` | `client_decline` / `admin_reject` | `rejected` | Release slot; emit `request.rejected` |
| `confirmed` | `admin_cancel` | `cancelled` | Release slot; cancel scheduled reminders; emit `request.cancelled` |
| `confirmed` | `complete` (worker) | `completed` | Release slot booking; no notification |

Any transition not in this table **MUST** raise `InvalidTransition` and change nothing. There is no path from `confirmed` back to `negotiating`.

A **client note** is deliberately not in the table, because it is not a transition: it inserts `negotiation_message(client, note)`, emits `request.note`, and leaves the status exactly as it was. It is accepted while a request is `pending`, `negotiating` or `confirmed` — the states where the therapist can still act on what it says — and **MUST** be refused once the request is terminal. The note body stays in the admin UI: the notification says a note arrived and names the request, nothing more.

### 7.2 Slot

| From | Event | To | Guard |
|---|---|---|---|
| `available` | `hold(request)` | `held` | Row lock; `starts_at > now()` |
| `held` | `release` | `available` | — |
| `held` | `expire` (worker) | `available` | `hold_expires_at < now()` |
| `held` | `extend(until)` | `held` | Re-stamps `hold_expires_at`; anything but `held` is left alone |
| `held` | `book(request)` | `booked` | `held_by_request = request.id` |
| `available` | `book(request)` | `booked` | Row lock (admin approving a free-text request onto a slot) |
| `booked` | `release` | `available` | Request left `confirmed` |
| `available` | `block` | `blocked` | Admin |
| `blocked` | `unblock` | `available` | Admin |

`hold` and `book` **MUST** execute `SELECT … FROM slot WHERE id = :id FOR UPDATE` before checking status. This is the only place where a lost update would double-book.

**A hold lasts as long as whatever it is protecting.** `slot_hold_minutes` is the window a *client* has to finish a form, and it applies only until the request exists. `submit_slot_request` holds until the request's own `expires_at` instead: the request stays `pending` for `pending_expiry_hours`, and a slot released ahead of that goes back on the picker while the request still points at it. A proposal that keeps its slot re-stamps the hold (`extend`), because §7.1 expires only `pending` — a negotiation has no expiry of its own, so a hold inherited from submission would lapse mid-conversation. `hold_expires_at` is never NULL while `held`; §6.4 makes that a biconditional.

---

## 8. Core use-cases

Signatures are indicative; all are `async`, take a session/unit-of-work, and return domain objects or raise domain errors. None takes an aiogram or FastAPI object.

**Clients and identity**
```
resolve_client(channel, external_id, *, language=None) -> Client        # get-or-create
issue_login_token(email) -> raw_token                                    # email identity
consume_token(raw_token, purpose) -> TokenResult
link_identity(client_id, channel, external_id, verified) -> Identity
set_client_language(client_id, lang)
set_client_timezone(client_id, iana)
```

**Content and translations**
```
list_menu_topics(lang) -> list[TopicSummary]
get_topic_blocks(topic_code, lang) -> list[ContentBlock]                 # published, ordered
upsert_block(topic_id, lang, position, body_md, kind, link_url) -> Block # writes a revision
get_text(lang, key, **fmt) -> str                                        # cache → DB → repo → key
```

**Slots**
```
list_available_slots(session_type_id, modality, window_from, window_to, tz) -> list[SlotView]
hold_slot(slot_id, request_id) -> Slot
release_slot(slot_id)
create_slots_bulk(pattern) -> list[Slot]                                 # weekday × time × date range
block_slot(slot_id) / unblock_slot(slot_id) / delete_slot(slot_id)
```

**Booking**
```
resolve_booking_mode() -> BookingModeResult                              # implements DESIGN §6 matrix
submit_slot_request(client_id, slot_id, session_type_id, modality, problem, contact_note,
                    display_name, client_tz, source_channel) -> BookingRequest
submit_free_time_request(client_id, session_type_id, modality, desired_time_text, ...) -> BookingRequest
join_waitlist(client_id, problem, contact_note) -> WaitlistEntry

admin_approve(request_id, scheduled_start=None) -> BookingRequest
admin_propose(request_id, proposed_start=None, body_text=None) -> BookingRequest
admin_reject(request_id, reason=None) -> BookingRequest
admin_cancel(request_id, reason) -> BookingRequest
client_accept(request_id) -> BookingRequest
client_counter(request_id, proposed_start=None, body_text=None) -> BookingRequest
client_decline(request_id) -> BookingRequest
```

Every use-case that changes state **MUST** append an `audit_log` row and return with domain events collected; the notification service turns events into outbox rows before the transaction commits.

---

## 9. Connector contracts

```python
class Transport(Protocol):
    channel: Channel
    async def send(self, address: str, message: RenderedMessage) -> DeliveryResult: ...

@dataclass
class RenderedMessage:
    subject: str | None            # email only
    parts: list[str]               # one part per outbound message (Telegram splits)
    actions: list[Action]          # buttons / links
    parse_mode: str | None

@dataclass
class Action:
    key: str                       # 'accept' | 'counter' | 'decline' | 'open'
    label: str                     # localised
    url: str | None                # email/web
    callback_data: str | None      # telegram, ≤64 bytes

@dataclass
class DeliveryResult:
    ok: bool
    permanent_failure: bool        # blocked bot, invalid address — do not retry
    error: str | None
```

Adding a channel means: one `Transport` implementation, one emitter in `app/render/markdown.py`, one inbound router if the channel is interactive, and a `Channel` enum value. It **MUST NOT** require touching anything under `app/core/services/`.

Telegram `callback_data` **MUST** stay within 64 bytes: use `<action>:<request_id>` and look up the rest.

---

## 10. Intent catalogue

Each intent has a key, a recipient, a payload schema, and available actions. Translation keys follow `intent.<key>.<part>`.

| Intent key | To | Payload | Actions |
|---|---|---|---|
| `request.submitted.admin` | admin | uuid, client name, session type, modality, requested time, problem | approve, propose, reject |
| `request.submitted.client` | client | uuid, session type, requested time | open |
| `request.proposal.client` | client | uuid, proposed_start, note | accept, counter, decline |
| `request.counter.admin` | admin | uuid, proposed_start, note, thread | approve, propose, reject |
| `request.confirmed.client` | client | uuid, scheduled_start, duration_min, session type, modality, join info | open |
| `request.confirmed.admin` | admin | uuid, scheduled_start, client | — |
| `request.rejected.client` | client | uuid, reason | — |
| `request.expired.client` | client | uuid | — |
| `request.cancelled.client` | client | uuid, scheduled_start, reason | — |
| `reminder.client` | client | uuid, scheduled_start, offset_min, modality, join info | open |
| `waitlist.joined.client` | client | — | — |
| `request.note.admin` | admin | uuid | — |
| `waitlist.joined.admin` | admin | uuid, problem, contact note | — |
| `auth.login_link.client` | client (email) | login url, telegram deep link | open |
| `auth.link_channel.client` | client | telegram deep link | open |
| `system.delivery_failed.admin` | admin | intent, address, error | — |
| `system.health.degraded.admin` | admin | overall state, failing check ids | open |
| `system.health.recovered.admin` | admin | overall state | — |

Payloads **MUST** be JSON-serialisable and **MUST NOT** embed rendered text.

**Join info** resolves as: `booking_request.meeting_url` if set, else `practice.online_meeting_url`, else omitted. It is included only when `modality='online'`, only in `request.confirmed.client` and `reminder.client`, and only for non-email channels — for `channel='email'` the message links to `/r/{uuid}` instead, where the client sees it after authenticating. For `modality='onsite'`, `practice.clinic_onsite_url` takes its place and **MAY** be sent by email. The admin sets a per-request `meeting_url` at approval time; leaving it blank uses the practice default.

---

## 11. Markdown rendering

### 11.1 Accepted subset

Paragraphs, `**bold**`, `*italic*`, `` `code` ``, fenced code blocks, `[text](url)`, unordered lists, ordered lists, headings `#`–`###`, blockquotes, horizontal rules.

**Rejected at save time** with a localised admin error: tables, images, raw HTML, nested lists deeper than one level, footnotes, headings below `###`.

### 11.2 Telegram emitter

- Output uses `parse_mode='HTML'` with only `<b> <i> <u> <s> <a> <code> <pre> <blockquote>`.
- Headings → `<b>text</b>` on its own line.
- Unordered list items → `• item`; ordered → `1. item`.
- Horizontal rule → `──────────`.
- Text nodes **MUST** escape `&`, `<`, `>` — and nothing else.
- Split output into parts at **block boundaries only**, at most 3500 characters per part. Never split inside a code block or a link. If a single block exceeds 3500 characters, split it at paragraph boundaries and, failing that, at the last whitespace before the limit.
- **MUST NOT** use `parse_mode='MarkdownV2'` anywhere.

### 11.3 Web emitter

Full HTML, headings shifted one level down (`#` → `<h2>`), output passed through the sanitiser allowlist before rendering.

### 11.4 Email emitter

Plain text primary, minimal HTML alternative. Links rendered as `text (url)` in the plain part.

### 11.5 Tests

The renderer **MUST** have golden tests including: Russian text containing `.`, `-`, `!`, `(`, `)`; Armenian text; a link whose label contains `<`; a 6000-character block; a code block containing `</b>`.

---

## 12. Web surface

### 12.1 Client routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Home; topic menu; language switcher |
| GET | `/t/{topic_code}` | Rendered topic page |
| GET | `/book` | Step 1 — session type and modality |
| GET | `/book/slots` | Step 2 — slots in the client's timezone (HTMX partial) |
| POST | `/book/hold` | Hold a slot; returns hold expiry |
| GET | `/book/details` | Step 3 — problem, name, contact note |
| POST | `/book` | Submit; returns confirmation with the request UUID |
| POST | `/waitlist` | Join the waitlist |
| GET | `/r/{uuid}` | Request status and negotiation thread (auth required) |
| POST | `/r/{uuid}/accept` \| `/counter` \| `/decline` | Negotiation actions |
| POST | `/r/{uuid}/note` | Add information to a request (§7.1); status unchanged |
| GET | `/auth/email` | Request a magic link |
| GET | `/auth/callback?token=` | Consume a login token |

Timezone is detected client-side and posted with the booking; a visible selector allows override. On-site selection **MUST** show `clinic_onsite_url`.

A message *about a request* **MUST** link to `/r/{uuid}` carrying a `view_request` token (§6.2), so following it opens the request rather than a sign-in form. Consuming that token starts a client session, which is what makes the link still work when it is opened a second time — the token itself is single-use, as §6.2 requires of every token.

### 12.2 Admin routes

All under `/admin`, session-authenticated, CSRF-protected.

`/admin/login`, `/admin/requests` (+ `?view=`, `?start=`), `/admin/requests/{uuid}` (+ `approve`, `propose`, `reject`, `cancel`), `/admin/waitlist`, `/admin/slots` (+ `bulk`, `block`, `delete`), `/admin/content` (+ `blocks`, `preview`, `revisions`), `/admin/translations` (+ `missing`), `/admin/settings`, `/admin/session-types`, `/admin/timezones`, `/admin/delivery`, `/admin/clients/{id}/export`, `/admin/clients/{id}/erase`, `/admin/maintenance` (+ `config/export`, `config/import`, `backups/{filename}`), `/admin/help`, `/admin/status`.

`/admin/requests` serves two views of one query, selected by `?view=` (DESIGN.md §15). `view=list` is the default and is the status-filtered table that already exists; `view=grid` is the **week schedule**. An unrecognised value falls back to `list` rather than erroring — the parameter is navigation, not input.

| Parameter | Values | Default |
|---|---|---|
| `view` | `list`, `grid` | `list` |
| `start` | `YYYY-MM-DD`, interpreted as the Monday of the week to show | the current week in the practice timezone |
| `status` | a `request_status` value; applies to `view=list` only | unset (all) |

`start` is a date rather than a week offset so a week is linkable and means the same thing tomorrow. A value that does not parse, or that is not a Monday, is snapped to the Monday of the week it falls in; a missing value means the current week. The grid **MUST NOT** carry the `status` filter: the view is defined by one rule instead, and the filter chips are replaced by the week navigation.

The grid shows a request whose effective start falls in the week — `scheduled_start` when set, otherwise the `starts_at` of the slot in `slot_id` (§7.1 sets `scheduled_start` only at approval, so a held slot is what a pending request has). Statuses shown are `confirmed`, `completed`, `pending`, and `negotiating`. `completed` is included and rendered muted: the worker sweeps a confirmed session to `completed` once its end passes (§14), so a grid without it renders every past week as empty. `rejected`, `expired`, and `cancelled` do not appear.

Requirements:

- Seven day columns, Monday to Sunday, in the **practice** timezone — not the admin's browser and not the client's. Each column holds that day's entries in time order. There are no hour rows (DESIGN.md §15 gives the reasoning).
- An entry **MUST** be placed by the local wall-clock date of its effective start: convert to the practice zone, then bucket by `.date()`. Placement **MUST NOT** be computed as an offset from the week's start instant. The difference shows up twice a year — a week containing a DST transition is not 168 hours long, and offset arithmetic silently shifts a day's worth of entries across the boundary.
- The window queried **MUST** be widened by a day at each end and then filtered by local date, so a zone whose midnight moves cannot clip the first or last column.
- Each entry renders its local start time, the client's display name, and its status, and links to `/admin/requests/{uuid}`. It carries **no** `problem_text` (hard rule 8), and no duration or modality — both are one click away on the request page, and the view was chosen for its quietness.
- Requests that are `pending` or `negotiating` with **no** effective start — the free-text path, which has `desired_time_text` and neither a schedule nor a slot — **MUST** be listed below the grid rather than omitted. The list is capped and shows status, name, and the client's own wording.
- The grid is **read-only**. There is no route that writes from it: no drag to reschedule, no click to create a slot, no inline approve.
- Both views are English like the rest of the admin surface (§15, DESIGN.md §11) and add **no** translation keys.
- No schema change. The read is served by `booking.scheduled_in_window` and `booking.unscheduled_for_admin` in `app/core/services/` — the channel does no scheduling logic of its own, and does not query per row.

The maintenance page carries both halves of §16.7 and §16.6 and nothing else:

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/maintenance` | Config export/import panel; list of available dumps |
| GET | `/admin/maintenance/config/export` | The config file as a JSON download (§16.7) |
| POST | `/admin/maintenance/config/import` | Multipart upload; `apply=0` previews, `apply=1` writes |
| GET | `/admin/maintenance/backups/{filename}` | Stream one dump from `BACKUP_PATH` |

The import route is one route with two modes on purpose: a preview that runs different code from the apply is a preview of nothing. Both parse and validate identically; `apply=0` rolls the transaction back and renders the counts.

`{filename}` **MUST** be validated against `^psychobooking-\d{4}-\d{2}-\d{2}(-\d{6})?\.dump$` and resolved inside `BACKUP_PATH`; anything else returns 404 without touching the filesystem. The response **MUST** stream rather than read the file into memory.

Uploads **MUST** be capped (5 MB) and rejected above it before parsing.

`/admin/content/preview` **MUST** render a block exactly as each channel would, side by side.

`/admin/status` shows the checks from §16.9 in full: for each, its state, the therapist's sentence, and the technical detail. `admin/base.html` carries a **dot** on every admin page, coloured from the overall state and linking here — the point of the feature is that nobody has to remember to look.

- The page and the dot read `STATE_PATH/status.json` and **MUST NOT** run the checks themselves. Checking is the worker's job (§14); a page render does no filesystem walking, no `pg_dump` inspection, and no outbound HTTP.
- A file that is missing, unparseable, or older than 3 minutes renders red with `worker_alive` failing (§16.9). This is the one check computed by the reader.
- `ok` | `warn` | `fail` render as green, amber and red. The colour names appear only in the template, never in the data or the domain (§16.8).
- The page **MUST** name what to do: carry on, or call whoever runs the server — and it links to the guide's troubleshooting section (§12.2's `/admin/help`) rather than restating it.

`/admin/help` serves the admin guide from `app/channels/web/guides/admin-guide.<lang>.html`, session-authenticated like every other admin route. The guides are complete standalone documents — their own navigation, search, theme toggle, and print stylesheet — so they are returned as they are rather than rendered through `admin/base.html`.

- They ship **inside the image**, not in `docs/`, so the guide always describes the version this installation is running.
- They **MUST** be self-contained: no CDN, no external stylesheet or font, nothing fetched at view time. There is no CSP to catch a regression, and a practice server may have no outbound network at all.
- `?lang=` selects the language. `en` and `ru` exist; anything else, `hy` included, falls back to `en` — a missing page is worse than the wrong language, and the fallback is also what stops the parameter being a path.
- This is the one admin surface that is **not** English-only (§15, DESIGN.md §11). The console stays in English; the manual explaining it does not have to be.

### 12.3 Telegram webhook

`POST {TELEGRAM_WEBHOOK_PATH}` — verify `X-Telegram-Bot-Api-Secret-Token` against `TELEGRAM_WEBHOOK_SECRET`; reject with 403 on mismatch, **before** parsing the body. Return 200 quickly; hand the update to the router.

---

## 13. Telegram flows

### 13.1 Client

1. `/start` — resolve or create the client and Telegram identity. If a `link_<token>` payload is present, consume it and attach this Telegram identity to the existing client instead.
2. Language selection (`Русский` / `Հայերեն`) on first contact only; stored on the client.
3. Persistent main keyboard: one button per menu topic, plus Consultation and My appointments.
4. Topic button → send that topic's published blocks in order, as separate messages.
5. Consultation → `resolve_booking_mode()` and follow the resolved path (slots picker / free-text / waitlist).
6. Slot picker: inline keyboard grouped by day, times in the client's timezone; timezone chosen from `timezone_option` if unknown.
7. After the slot is held: session type, modality, problem text, optional name, then the contact step (each skippable where optional). The contact step is a choice, not free text, because "email" is a natural answer to an open question and the service cannot act on it:
   - **Telegram** — the identity already exists, so nothing is stored and delivery keeps following §13.3.
   - **Email** — ask for an address and reject anything not shaped like one. The address is **not** trusted on arrival: `auth.login_link.client` is sent to it, and following that link is what sets `verified_at` (§6.2). Once verified, §13.3 delivers confirmations and reminders to both channels. Verification is never a precondition for booking — the request is submitted either way.
   - **Other** — free text, stored in `contact_note` as before.
8. Submit → confirmation carrying the request UUID.
9. My appointments → every request of the client that is `pending`, `negotiating` or `confirmed`, newest first, at most three. Each shows its status, its time in the client's timezone, the session type and the modality; a confirmed online session also shows its join link (§10). `problem_text` is **MUST NOT** be echoed back (hard rule 8) — the client wrote it, but this service does not repeat it into a chat someone else may be reading over their shoulder. When exactly one of the listed requests is waiting on the client's answer, the message carries that request's accept / counter / decline buttons, so a proposal buried in the chat history is still answerable. This view changes nothing: no transition, no outbox row.

A message whose text exactly matches a main-keyboard label is **navigation, not an answer**: any half-finished flow is abandoned and the button's action runs. Without this rule a client who taps a topic while being asked to describe their problem has the button's label stored as the problem, which is what the flow-step check would otherwise do with it.

Multi-step input state is stored in the database against the client, **not** in aiogram FSM memory, so a restart does not lose a half-finished booking.

### 13.2 Admin

`/admin` gated by `TELEGRAM_ADMIN_IDS`, and by nothing else: the bot has no other way to authenticate the therapist (DESIGN.md §5.2). A chat that is not in that set gets **silence**, never an error — an unknown chat learns nothing.

The surface is a **panel**: one message with an inline keyboard, which the therapist navigates. It is inline-only because her chat already carries the client reply keyboard and two persistent keyboards cannot coexist. It is English, like the rest of the admin surface (DESIGN.md §11), so none of it takes translation keys.

| Screen | Callback | Shows | Leads to |
|---|---|---|---|
| Panel | `apanel` | availability, pending and negotiating counts, next confirmed session, waitlist size, admin URL | requests, sessions, waitlist, availability toggle |
| Requests | `areq:<page>` | pending and negotiating, newest first, 5 a page | one request; panel |
| Request | `aopen:<id>` | uuid, status, client name, contact note, session type, modality, time, `problem_text`, last 3 thread messages | its permitted actions; requests; panel |
| Sessions | `asess:<days>` | confirmed sessions in the next `days` (2 or 7), with join links, at most 10 | one request; the other window; panel |
| Waitlist | `awl:<page>` | entries, 5 a page, **read-only** | panel |

Requirements:

- Every screen **MUST** offer a way back. No reply may end in text with nothing to press — including the outcome of an action, which **MUST** be the re-rendered request screen rather than a bare "Confirmed …".
- A request's action buttons **MUST** be derived from §7.1's transition table, so the panel never offers what the core would refuse. `negotiating` therefore offers propose and reject but not approve.
- `propose` and `cancel` need typing, so they park the request id in `flow_state` (§13.1's store, not aiogram FSM) and answer with a prompt carrying `✕` to abandon. Cancel's prompt carries `Skip`; the reason reaches the client in `request.cancelled.client`, so it is asked for rather than invented. Approve uses the practice's default meeting link; a per-request `meeting_url` stays web-only.
- A reply caused by a **button** edits that message in place; a reply caused by **typed text** is a new message. An edit rejected as unmodified **MUST** be treated as success, and an edit refused because the message is too old (Telegram's 48-hour limit, reached by pressing a button on an old notification) **MUST** fall back to sending a new message.
- The webhook **MUST** answer the callback query, or the therapist's client spins on every tap. A refused action answers with its reason.
- `/admin` while a typed answer is pending abandons it, exactly as a main-keyboard label does in §13.1.
- `problem_text` **MAY** be shown here: this is an admin surface, and DESIGN.md §16 names the therapist's own Telegram account as one of the two places it is visible. It **MUST NOT** be logged (hard rule 8).

Content, translations, settings, session types, timezones, slot creation, the delivery log, and client export or erasure are **web-only** and **MUST** answer with a link to the admin UI.

### 13.3 Notification delivery policy

For each intent and client, deliver to: the Telegram identity if one exists; otherwise the verified email identity; and to both when `intent_key` is `reminder.client` or `request.confirmed.client` and both identities exist.

`auth.login_link.client` is the one exception, as §10 already implies by naming its recipient "client (email)": it is addressed to the email the link is *about*, verified or not, because that delivery is what proves the address. It is never routed to Telegram, and it is the only intent allowed to reach an unverified address.

### 13.4 Email content restriction

Outbox rows with `channel='email'` **MUST NOT** include `problem_text`, negotiation bodies, or session-type-derived clinical wording in `payload`. Subject lines **MUST** be neutral and configurable via translation keys. Reminder emails **MUST** include the date and time in the body.

### 13.5 Calendar attachment

A confirmation delivered by email carries one iCalendar file, so a client who keeps a calendar can add the session in a single action rather than retyping it. This is the only attachment the service produces, on any channel.

- Only `request.confirmed.client` on `channel='email'` attaches anything. Every other intent and every other channel attaches nothing. The Telegram equivalent is deferred (DESIGN.md §20).
- The file is `text/calendar; charset=utf-8`, named `session.ics`: one `VCALENDAR`, `VERSION:2.0`, `METHOD:PUBLISH`, containing exactly one `VEVENT`.
- `METHOD:REQUEST` **MUST NOT** be used and the event **MUST NOT** carry an `ATTENDEE`. Either one turns the entry into an invitation the client can accept or decline inside their calendar — an answer this service would never hear, on a channel that does not exist. Client-initiated cancellation is out of scope (§21). What is sent is a copy to keep, not a negotiation.
- `UID` is `{booking_request.uuid}@{host of BASE_URL}`, stable across redeliveries, so an outbox retry updates the entry the client already added instead of producing a second one. `SEQUENCE` is `0`.
- `DTSTART` and `DTEND` are UTC (`…Z` form). `TZID` **MUST NOT** be used and no `VTIMEZONE` is emitted: every client converts to the viewer's own zone, and storage is UTC already (hard rule 4).
- `DTEND` is `DTSTART` plus `booking_request.scheduled_duration_min`, falling back to the request's session type `duration_min`.
- `SUMMARY` comes from a translation key in the client's language and **MUST** be neutral in §13.4's sense. Once the client adds it, that string sits on a lock screen and on any calendar they share.
- `LOCATION` is `practice.clinic_onsite_url` for `modality='onsite'` (§10 permits that link by email) and a translated word for "online" for `modality='online'`. The online join link **MUST NOT** appear: §10's restriction follows the file, which is more exposed than the message carrying it, not less.
- `DESCRIPTION` is omitted. There is nothing to put in it the body does not already say better.
- The file **MUST** be built from the payload *after* §13.4's scrub, never from `booking_request` directly, so the attachment cannot become a second way around the scrub.
- Encoding follows RFC 5545: CRLF line endings, lines folded at 75 octets, and `\`, `;`, `,` and newlines escaped in text values.
- A cancelled session is **not** withdrawn from the client's calendar: no `METHOD:CANCEL` counterpart is sent, since that would require the invitation form ruled out above. `request.cancelled.client` **MUST** therefore say in words that a calendar entry added earlier needs removing by hand.

---

## 14. Worker jobs

One loop, every `WORKER_POLL_SECONDS`. Each job claims rows with `FOR UPDATE SKIP LOCKED` and commits per batch.

| Job | Query | Action |
|---|---|---|
| `dispatch_outbox` | `status='pending' AND next_attempt_at <= now()` limit 50 | Render, send, record attempt. On success `sent`. On transient failure: `attempts += 1`, backoff `min(2^attempts, 60) minutes`; after 6 attempts `dead` + `system.delivery_failed.admin`. On permanent failure: `dead` immediately, no alert unless `attempts = 0`. |
| `expire_slot_holds` | `slot.status='held' AND hold_expires_at < now()` | Release to `available` |
| `expire_requests` | `status='pending' AND expires_at < now()` | Transition to `expired`; release slot; notify |
| `fire_reminders` | `reminder.state='scheduled' AND due_at <= now()` | Create outbox rows with `dedupe_key = 'reminder:{reminder_id}'`; set `sent` |
| `complete_requests` | `status='confirmed' AND scheduled_start + duration < now()` | Transition to `completed`; release slot |
| `purge_content` | Terminal requests older than `retention_months` | Null out `problem_text`, negotiation `body_text`, `contact_note`; keep the row |
| `prune_tokens` | `auth_token.expires_at < now() - 7 days` | Delete |
| `prune_revisions` | Blocks with more than 20 revisions | Delete the oldest |
| `write_status` | The checks in §16.9 | Rewrite `STATE_PATH/status.json` atomically, **every pass**; on a transition into or out of `fail`, queue `system.health.degraded` / `system.health.recovered` (§16.10). **MUST NOT** raise: a failing check is a `warn` in the file, never a job that stops writing it |
| `prune_error_events` | `error_event.at < now() - 30 days` | Delete |
| `refresh_translations` | `max(translation.updated_at)` | Clear this process's UI-string cache if the mark has moved since the last pass (§15). Runs **before** `dispatch_outbox`, so an edit is in force for the messages that pass renders |

Every job **MUST** be idempotent and safe to run concurrently with a second worker, even though only one is deployed.

---

## 15. Localization

`locales/{ru,hy,en}.yaml` are the seed source of truth and are loaded into `translation` on first startup for keys that do not yet exist. Existing rows are **never** overwritten by a deploy — the therapist's edits win.

**The complete key catalogue ships with this specification** as `locales/en.yaml`, `locales/ru.yaml`, and `locales/hy.yaml`. Those files are normative for key *names*: an implementation **MUST NOT** invent key names for anything they already cover, and **MUST** add any new key to all three files in the same change. The Russian and Armenian *copy* in them is carried over from the v1.0 deployment where equivalent text existed and marked `# TODO` where it does not; final wording comes from the therapist and is not the implementer's to write.

Two rules on values:

- **No markup in translation values.** Emphasis, bullets, and layout come from the renderer, not from `<b>` inside a string. The v1.0 strings embedded Telegram HTML, which would leak literal tags into email and web.
- **Placeholders use `str.format` names** (`{time}`, `{uuid}`, `{price}`). A `KeyError` during formatting **MUST** fall back to the unformatted value and log at `ERROR`, never raise into a handler.

Anything that reads as practice *policy* rather than UI chrome — working hours, session length statements, cancellation terms — belongs in a content block (§6.7), not in a translation key. The v1.0 `ask_time` string hardcoded Yerevan time and Friday/Saturday availability into a UI label; that content is now a block.

Lookup order in `get_text`: process cache → `translation` row → repository YAML → practice default language → the key itself. Cache invalidates on admin edit.

The cache is **per process**, and an admin edit only clears the process serving that request. `web` and `worker` are separate processes (§3) and the worker renders every outbox message through the same cache, so the edit must reach it too: the `refresh_translations` job (§14) compares `max(translation.updated_at)` against the highest value that process has already applied and clears the cache when it moves. Staleness is therefore bounded by one worker pass. `translation.updated_at` **MUST** carry `onupdate` and not `server_default` alone — the seed inserts every key at boot, so an edit is always an UPDATE and a default-only column would never move.

Language codes: `ru`, `hy`, `en`. `am` **MUST NOT** appear anywhere in the codebase.

Missing-key logging: once per key per process, at `WARNING`.

---

## 16. Deployment

One VPS, Docker Compose, one image. TLS is the application's own concern by default — there is no assumed external proxy.

### 16.1 Profiles

Selected with `COMPOSE_PROFILES`:

| Profile | Services | When |
|---|---|---|
| `tls` (default) | `caddy`, `web`, `worker`, `db` | Standalone deployment. Caddy obtains and renews a Let's Encrypt certificate for `DOMAIN` automatically. |
| `plain` | `web`, `worker`, `db` | Something else terminates TLS — a Cloudflare Tunnel, or an existing reverse proxy on the host. `web` binds to `127.0.0.1:8000` only. |
| `plain,cloudflared` | above plus `cloudflared` | Cloudflare Tunnel, no inbound ports opened at all. |

`plain` **MUST NOT** publish port 8000 on `0.0.0.0`. If it did, the app would be reachable over plain HTTP from the internet, and session cookies marked `Secure` would silently stop working.

The Telegram webhook requires a publicly reachable HTTPS URL. With `plain` and nothing terminating TLS in front, startup **MUST** refuse to register a webhook and **MUST** log an explicit instruction to set `TELEGRAM_MODE=polling` instead.

### 16.2 `Dockerfile`

```dockerfile
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq5 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY locales ./locales
COPY alembic ./alembic
COPY alembic.ini .
RUN adduser --disabled-password --gecos "" appuser && chown -R appuser /app
USER appuser
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

One image serves both `web` and `worker`; the worker overrides the command. No volume is mounted into the application — with content in the database there is nothing on disk to persist except PostgreSQL's own data.

### 16.3 `docker-compose.yml`

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-psychobooking}
      POSTGRES_USER: ${POSTGRES_USER:-psycho}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?required}
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-psycho}"]
      interval: 5s
      timeout: 5s
      retries: 10
    restart: unless-stopped

  web:
    build: .
    env_file: .env
    depends_on:
      db:
        condition: service_healthy
    command: >
      sh -c "alembic upgrade head &&
             uvicorn app.main:app --host 0.0.0.0 --port 8000
             --proxy-headers --forwarded-allow-ips='*'"
    expose:
      - "8000"
    ports: !override []          # see per-profile note below
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"]
      interval: 10s
      timeout: 5s
      retries: 6
    restart: unless-stopped

  worker:
    build: .
    env_file: .env
    depends_on:
      web:
        condition: service_healthy
    command: ["python", "-m", "app.worker.main"]
    restart: unless-stopped

  caddy:
    image: caddy:2-alpine
    profiles: ["tls"]
    environment:
      DOMAIN: ${DOMAIN:?required for tls profile}
      ACME_EMAIL: ${ACME_EMAIL:?required for tls profile}
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    ports:
      - "80:80"
      - "443:443"
    depends_on:
      - web
    restart: unless-stopped

  cloudflared:
    image: cloudflare/cloudflared:latest
    profiles: ["cloudflared"]
    command: tunnel --no-autoupdate run --token ${TUNNEL_TOKEN:?required}
    depends_on:
      - web
    restart: unless-stopped

volumes:
  pgdata:
  caddy_data:
  caddy_config:
```

Compose has no per-profile `ports` override, so publish the `plain`-profile port from `docker-compose.plain.yml` (an overlay applied with `-f`) rather than from the base file:

```yaml
services:
  web:
    ports:
      - "127.0.0.1:8000:8000"
```

Under `tls`, `web` is reachable only on the internal network; Caddy is the sole ingress.

The base file also defines `backup` (§16.6), which is not behind a profile and runs in every deployment.

`worker` and `web` both mount `${STATE_DIR:-./state}` — read-write for `worker`, read-only for `web` — so the status file survives a restart and can be read with `cat` when the application cannot answer (§16.8).

`docker-compose.dev.yml` is a third overlay and belongs to development alone. The Dockerfile copies the source into the image, so a container runs the code as it stood at build time; the overlay bind-mounts `app/`, `tests/`, `locales/` and `alembic/` read-only over those copies, so `docker compose exec web pytest` exercises the working tree rather than the last build. It **MUST NOT** be applied to a deployment, which runs the image it was built from and nothing else.

### 16.4 `Caddyfile`

```
{
    email {$ACME_EMAIL}
}

{$DOMAIN} {
    encode zstd gzip
    reverse_proxy web:8000
}
```

Certificate issuance, renewal, and the HTTP→HTTPS redirect are automatic. No certbot cron, no manual renewal, no Nginx Proxy Manager.

### 16.5 Migrations on startup

`web` runs `alembic upgrade head` before serving. `worker` **MUST NOT** run migrations — it waits on `web`'s healthcheck, so exactly one process migrates. This is safe for a single-instance deployment; if `web` is ever scaled beyond one replica, migrations **MUST** move to a separate one-shot service.

### 16.6 Backup and restore

Backup is a `backup` service in the same Compose file, on the same `postgres:16` image as `db` so that `pg_dump` can never be older than the server it dumps. It **MUST NOT** be behind a profile: profiles select the ingress (§16.1), and a backup that has to be switched on is a backup most installs do not have.

The service sleeps until `BACKUP_HOUR_UTC`, dumps, prunes, and sleeps again:

```yaml
  backup:
    image: postgres:16
    env_file: .env
    depends_on:
      db:
        condition: service_healthy
    volumes:
      - ${BACKUP_DIR:-./backups}:/backups
    environment:
      PGPASSWORD: ${POSTGRES_PASSWORD:?required}
    entrypoint: ["/bin/sh", "/backup.sh"]
    restart: unless-stopped
```

Requirements on the script:

- `pg_dump -h db -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"` — custom format, so `pg_restore` can be selective.
- Write to a temporary name in the same directory and `mv` into place, so a half-written dump is never named like a complete one and never listed by the admin UI.
- Name `psychobooking-YYYY-MM-DD.dump`, matching the pattern §12.2 validates. A second run on the same day appends `-HHMMSS` rather than overwriting.
- Verify the dump before moving it into place: `pg_restore --list` must read it and report a non-empty table of contents. A dump that fails verification is deleted and the run fails loudly — dumps that exist and cannot be read are the corruption that actually ruins a practice (DESIGN.md §22.7). Record the outcome for `backup_verified` (§16.9) by writing `.last-verify` beside the dumps: one line, `ok` or `failed`, plus the filename.
- On success, delete dumps older than `BACKUP_RETENTION_DAYS`. **Never** prune when the dump failed — that is the run whose predecessors matter most.
- Exit non-zero and log on failure; `restart: unless-stopped` retries. Failure **MUST NOT** be silent.
- Compute the sleep from the wall clock each iteration, not by sleeping 86400 seconds, so a restart does not permanently shift the hour.
- On startup, if the directory holds no dump at all, take one immediately. A fresh install would otherwise show an empty backups page for up to a day, which reads as "backups are not working" rather than "not yet".

The directory is bind-mounted from the host, so dumps are reachable by `scp` without Docker. `web` mounts the same directory **read-only** at `BACKUP_PATH` and serves it per §12.2 for a therapist with no shell access.

The dump contains clients' problem text (§17, DESIGN.md §16). The directory **MUST** be created with restrictive permissions, and encrypting it and copying it off-host remains the operator's responsibility — the README **MUST** say so, and **MUST NOT** imply the container has done it.

Restore is a CLI procedure and **MUST NOT** be exposed in the admin UI:

```bash
docker compose stop web worker
docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  --clean --if-exists < backup.dump
docker compose start web worker
```

The README **MUST** document a restore rehearsal onto a scratch database. A backup that has never been restored is a hypothesis.

### 16.7 Configuration export and import

A JSON file holding everything the therapist typed into the admin UI, and no client data (DESIGN.md §21). Logic lives in `app/core/services/config_io.py` — `export_config()` and `import_config()` — and the routes in §12.2 are thin.

**Scope.** Included: the `practice` row, `session_type`, `timezone_option`, `content_topic`, `content_block`, `translation`. Excluded: clients, identities, requests, negotiation, waitlist, outbox, reminders, audit log, flow state, admin users, auth tokens, content revisions, and **slots** (dated availability, not configuration).

**Format.** Version 1:

```json
{
  "format": "psychobooking.config",
  "version": 1,
  "exported_at": "2026-08-26T09:00:00+00:00",
  "practice": { "name": "…", "timezone": "Europe/Moscow", "…": "MUTABLE_FIELDS only" },
  "session_types": [
    {"code": "individual", "duration_min": 60, "price_amount_minor": 5000,
     "price_currency": "AMD", "price_display_override": null,
     "is_active": true, "sort_order": 0}
  ],
  "timezone_options": [
    {"iana_name": "Asia/Yerevan", "display_name": "Yerevan, Tbilisi, Dubai",
     "sort_order": 0, "is_active": true}
  ],
  "content": [
    {"code": "work_terms", "sort_order": 0, "show_in_menu": true, "is_active": true,
     "blocks": [
       {"lang": "ru", "position": 0, "kind": "text", "body_md": "…",
        "link_url": null, "is_published": true}
     ]}
  ],
  "translations": { "ru": {"key": "value"}, "hy": {}, "en": {} }
}
```

- Database ids, `practice_id`, and timestamps are **never** written. Rows match on natural keys: `session_type.code`, `timezone_option.iana_name`, `content_topic.code`, `(topic.code, lang, position)` for blocks, `(lang, key)` for translations.
- `practice` carries exactly `MUTABLE_FIELDS` from `app/core/services/settings.py` — no more, so that adding a settings field does not silently widen what an import can rewrite.
- Keys are sorted and the file is written with `indent=2`, so two exports diff usefully in a text editor.

**Import semantics.**

- **Merge, never delete.** Rows present in the database and absent from the file are left untouched. There is no replace mode (DESIGN.md §21.3).
- One transaction. Any validation failure aborts the whole import; nothing partial is written.
- A changed `content_block.body_md` **MUST** go through the same path as the editor — previous body into `content_block_revision`, `version` incremented — so an unwanted import is undone per block from the existing revisions page.
- A block whose body is unchanged **MUST NOT** write a revision or bump `version`.
- `practice` changes go through `update_settings()`, not a direct `UPDATE`, so its validation applies.
- Import **MUST NOT** touch `admin_user`, and a file containing such a key is rejected rather than ignored.

**Validation — the whole file is rejected on any of these:**

- `format` is not `psychobooking.config`, or `version` is not one this build knows.
- A language outside `ru`, `hy`, `en`. `am` therefore cannot enter through an import (hard rule 5).
- A `practice` field outside `MUTABLE_FIELDS`, or a value `update_settings()` refuses.
- An unknown enum value (`booking_mode`, `content_block.kind`).
- `body_md` that fails the §11.1 Markdown subset check — the same validation the editor applies.
- Two blocks claiming the same `(topic, lang, position)`, or two translations the same `(lang, key)`.

A translation key absent from the code's catalogue is **reported and skipped**, not fatal: it usually means the file came from a newer build, and writing a key no renderer reads is dead weight.

**Preview.** `apply=0` runs the identical code path inside a transaction that is rolled back, and returns counts per section — created, updated, unchanged, skipped. The apply step re-parses the uploaded file; nothing is carried between the two requests in server state.

**Audit.** `config.export` and `config.import` rows in `audit_log`, with per-section counts in `meta`. `meta` **MUST NOT** contain content bodies or translation values — counts and keys only.

### 16.8 The status file

The worker writes its health findings to `STATE_PATH/status.json` on a volume bind-mounted from the host: `worker` read-write, `web` read-only. It is deliberately **not** a database row (DESIGN.md §22.3) — the most consequential thing a check can discover is that the database is unreachable.

Written atomically: a temporary name in the same directory, then `rename`. A reader **MUST** never see a partial file.

```json
{
  "written_at": "2026-08-26T09:00:00+00:00",
  "state": "warn",
  "version": 1,
  "checks": [
    {
      "id": "backup_fresh",
      "state": "warn",
      "summary": "The last backup is 2 days old.",
      "detail": "newest dump psychobooking-2026-08-24.dump, age 51h, threshold 36h"
    }
  ]
}
```

- `state` is `ok` | `warn` | `fail`, in code and in the file. Green, amber and red are the *rendering* of those three and appear nowhere in the data (§12.2).
- The overall `state` is the worst of the individual checks.
- `summary` is for the therapist: consequences, plain language, no jargon. `detail` is for whoever runs the server: counts, identifiers, timestamps, thresholds.
- `detail` **MUST NOT** contain `problem_text`, message bodies, exception messages, tracebacks, client names, or email addresses (hard rule 8, DESIGN.md §22.5). Counts and identifiers only.
- The file **MUST** be rewritten every pass even when nothing has changed: its `written_at` is the worker's liveness signal (§12.2).

### 16.9 Checks and thresholds

Normative. A check not in this table does not exist; thresholds are not tuning knobs and **MUST NOT** be exposed as settings — a threshold the therapist can raise is a threshold that gets raised the first time it is inconvenient.

| id | `fail` | `warn` | Detects |
|---|---|---|---|
| `worker_alive` | `status.json` older than 3 min, or absent | — | Worker crashed, wedged, or never started. Computed by the **reader**, not the writer |
| `outbox_dead` | ≥1 `dead` in the last 7 days | — | Somebody never got a confirmation or reminder |
| `outbox_failing` | — | ≥3 `failed` in the last hour | Wrong SMTP credentials, a blocked bot, Telegram throttling |
| `outbox_stalled` | `pending` with `next_attempt_at` overdue by >15 min | — | Dispatch is not running even though the worker is |
| `outbox_stuck` | `sending` for >15 min | — | A process died between the claim and the transport (§14). Deliberately not retried: whether the message arrived is precisely what is unknown, and a sweep that guessed would reintroduce the duplicate `sending` exists to prevent |
| `reminders_overdue` | `scheduled` with `due_at` overdue by >15 min | — | As above, on the reminder path |
| `web_errors` | ≥10 in the last hour | ≥1 in the last hour | Unhandled exception in a request — otherwise invisible (§6.9) |
| `worker_errors` | same job in ≥3 consecutive passes | ≥1 in the last hour | A job failing silently behind §14's per-job `except` |
| `backup_fresh` | newest dump older than 7 days | older than 36 h | The backup container is dead or failing |
| `backup_verified` | last dump failed verification | — | Dumps exist and cannot be read (§16.6) |
| `disk_space` | <5% or <500 MB free on `BACKUP_PATH` | <15% or <2 GB | The slow failure that takes everything with it |
| `schema_version` | Alembic version in the database ≠ code head | — | A half-applied or skipped migration |
| `practice_row` | not exactly one `practice` row | — | A seed that did not run, or a bad restore |

Rules that apply to all of them:

- The therapist's own workload is **never** a check. Pending requests, an empty slot grid, and a long waitlist are work, not faults (DESIGN.md §22.4).
- Every check **MUST** be a bounded query. `outbox_stalled` and `reminders_overdue` use the existing `ix_outbox_message_status_next_attempt_at` and `ix_reminder_state_due_at`; nothing here may table-scan.
- A check that cannot run (the filesystem is unreadable, a query raises) reports `warn` with a `detail` saying so. It **MUST NOT** report `ok`, and **MUST NOT** raise: one broken check may not take the status file down with it.

### 16.10 Health notifications

The dot is only useful to somebody looking at it. A transition **into** `fail` writes an outbox row for the admin (`system.health.degraded`, §10); a transition from `fail` back to `ok` writes `system.health.recovered`.

- Transitions only. While the state stays `fail`, at most one further notification per 6 hours.
- `dedupe_key = 'health:{state}:{iso8601 hour}'`, so a flapping check cannot spam the therapist even if the worker restarts.
- The payload carries the failing check **ids** and the overall state — never a `detail` string, which is written for a different reader and under looser rules than §13.4 allows for email.
- If the database is unreachable, no notification can be written; that case is covered by the external uptime check, not by code (DESIGN.md §22.6). The README **MUST** say so, next to the `/readyz` endpoint.

---

## 17. Security

- Passwords: Argon2id.
- Admin session cookie: `HttpOnly`, `Secure`, `SameSite=Lax`, rotated on login.
- CSRF token on every mutating admin and client form.
- Rate limits: admin login 5 per 15 min per IP; magic-link issuance 3 per hour per email and 10 per hour per IP; booking submission 5 per hour per client.
- Telegram webhook secret header checked before body parsing.
- All web-rendered content passes the sanitiser.
- SQL exclusively through SQLAlchemy; no string-built queries.
- `.env` git-ignored; `.env.example` committed with placeholders.
- Logging: **MUST NOT** log `problem_text`, negotiation bodies, tokens, or message payloads. Log identifiers.
- Uploads: exactly one — the config file at `/admin/maintenance/config/import` (§16.7). `application/json`, 5 MB cap enforced before parsing, admin session and CSRF required, never written to disk. Clients upload nothing.
- Backup dumps contain `problem_text`. The backup directory **MUST** be created `0700`; `web` mounts it read-only; the download route requires an admin session and validates the filename against a fixed pattern (§12.2). Hard rule 8 is unaffected — a dump reaching the therapist through the admin UI has not left it.
- The status file and `/admin/status` carry counts, identifiers, timestamps and thresholds only — never `problem_text`, message bodies, client names, addresses, exception messages, or tracebacks (§16.8, DESIGN.md §22.5). `error_event` stores an exception's class and location, never its message.
- The Docker socket **MUST NOT** be mounted into `web`, `worker`, or `backup`. It is root on the host, offered to the processes most exposed to the internet.

---

## 18. Testing

Required before a milestone is complete:

- **Architecture test:** import-graph assertion that no module under `app/core/` imports `fastapi`, `aiogram`, `jinja2`, `aiosmtplib`, or `nh3`. This test failing means the design has been violated.
- **Core unit tests** with no channel imports: every transition in §7.1 and §7.2, including every rejected transition raising `InvalidTransition`.
- **Concurrency test:** two coroutines holding the same slot simultaneously — exactly one succeeds.
- **Renderer golden tests:** §11.5.
- **Outbox tests:** transient failure retries with backoff; permanent failure does not retry; restart mid-send does not duplicate (dedupe key).
- **Reminder tests** using `time-machine`: creation on confirmation, cancellation on cancel, `skipped` when already past.
- **Retention test:** purge nulls content and preserves rows.
- **E2E:** book via web → approve via admin → reminder fires → cancel; and the same via a simulated Telegram update.
- **Config round-trip test:** export → import into a freshly seeded database → export again produces an identical file apart from `exported_at`; the second import writes no revisions and reports every section as unchanged.
- **Config rejection tests:** unknown language (including `am`), unknown settings field, unknown enum value, invalid Markdown, duplicate natural key — each aborts the whole import with the database untouched.
- **Backup download tests:** traversal (`../`), an absolute path, and a name outside the dump pattern all return 404; a valid name streams the file.
- **Health check tests:** each threshold in §16.9 asserted either side of its boundary; a check whose query raises reports `warn` without preventing the others from being written; `status.json` is written atomically and parses; a stale `written_at` is read as `worker_alive` failing.
- **Health notification tests:** one outbox row on the transition into `fail`, none while it stays there inside the 6-hour floor, one on recovery; the payload carries check ids and no `detail` string.
- **Error recording tests:** an unhandled exception in a request and a raising worker job each write one `error_event` carrying class and location; neither writes the exception message or a traceback; a failure to record **MUST NOT** mask the original exception.

Tests run against a real PostgreSQL (Compose service or testcontainers). SQLite **MUST NOT** be used — the schema depends on native enums, arrays, and `FOR UPDATE SKIP LOCKED`.

---

## 19. Milestones

Each milestone ends with its acceptance criteria passing in CI.

**M0 — Skeleton.** Repo layout, config loading, Dockerfile, Compose with all three profiles (§16), Caddyfile, health endpoints, `.gitignore` containing `.env`, CI running lint and tests.
*Accept:* `docker compose up` yields a healthy stack under both the `tls` and `plain` profiles; `/healthz` returns 200; the `plain` profile publishes nothing on `0.0.0.0`; the architecture test exists and passes trivially.

**M1 — Schema.** All models from §6, one linear Alembic migration, seed script (practice, admin user, session types, timezone options, content topics, translations from `locales/`).
*Accept:* `alembic upgrade head` on an empty database produces the schema in §6 including every enum type in §5; `alembic heads` shows exactly one; seeding is idempotent and loads every key from `locales/*.yaml`.

**M2 — Core domain.** Services from §8, both state machines, policies, domain events. No channels.
*Accept:* every transition and rejection in §7 is covered by a test; the concurrency test passes; the architecture test passes.

**M3 — Content and renderer.** Content services, Markdown subset validation, all three emitters.
*Accept:* golden tests in §11.5 pass; saving a table returns a validation error; a 6000-character block emits multiple Telegram parts split at block boundaries.

**M4 — Outbox, worker, email.** Notification service, outbox dispatch, SMTP transport, retry and dedupe, all worker jobs from §14.
*Accept:* outbox and reminder tests pass; with `SMTP_HOST` unset the system starts cleanly and creates no email rows.

**M5 — Telegram client.** Webhook route, `/start` with deep-link consumption, language, topics, the full booking flow, database-backed step state.
*Accept:* a simulated update sequence produces a `pending` request with a held slot; restarting `web` mid-flow preserves progress.

**M6 — Web client.** All routes in §12.1, timezone detection, magic-link auth, HTMX slot picker.
*Accept:* the E2E web booking test passes; a client with no Telegram can book end to end when SMTP is configured.

**M7 — Admin web.** All routes in §12.2 including content editing with per-channel preview and revisions.
*Accept:* a therapist can, without touching the database, change availability, create a week of slots, edit a block, roll it back, and approve a request.

**M8 — Negotiation and reminders across channels.** Proposal, counter, accept, decline surfaced in Telegram, web, and email; reminders delivered; therapist cancellation.
*Accept:* the full E2E scenario in §18 passes on both channels.

**M9 — Hardening.** Rate limits, audit log coverage, retention purge, client export and erasure, backup documentation, README with setup and operations.
*Accept:* security checklist in §17 verified item by item; retention test passes; a backup taken per §16.6 is restored onto a scratch database and the application starts against it; a fresh clone reaches a working deployment following only the README.

**M10 — Portability and backups.** Config export and import (§16.7) with the maintenance page from §12.2; the `backup` sidecar and dump download (§16.6); README operations section covering both.
*Accept:* exporting from a configured install and importing into a freshly seeded one reproduces its settings, session types, timezones, topics, blocks, and translations, and creates no clients or requests; re-importing the same file is a no-op that writes no content revisions; a file naming an unknown language, an unknown settings field, or invalid Markdown is rejected whole, leaving the database unchanged; the preview reports the same counts the apply performs; the `backup` container produces a restorable dump on schedule, prunes past the retention window, and never leaves a partial file visible; the download route rejects `../` and any name outside the dump pattern with 404.

**M11 — Health signal.** `error_event` (§6.9) and its recorders in `web` and `worker`; the checks in §16.9; the `write_status` and `prune_error_events` jobs (§14); the status file (§16.8); the dot and `/admin/status` (§12.2); health notifications (§16.10); dump verification in `backup.sh` (§16.6); README and admin-guide sections including the external uptime check.
*Accept:* with everything healthy the dot is green on every admin page and `status.json` lists every check in §16.9 as `ok`; stopping the `worker` container turns the dot red within 4 minutes with `worker_alive` failing, without any admin page erroring; a `dead` outbox row turns it red and writes exactly one `system.health.degraded` outbox row, and a second `dead` row within the hour writes none; an unhandled exception in a request creates an `error_event` carrying the exception class and location and **no** message or traceback; removing the newest dump and ageing the rest turns `backup_fresh` amber; a corrupt dump fails `backup_verified`; a check whose query raises reports `warn` and the other checks still write; `status.json` is readable and parseable with the application stopped.

**M12 — Calendar attachment.** The iCalendar file in §13.5: `duration_min` on the `request.confirmed.client` payload (§10), an attachment field on the connector contract (§9), the emitter, the SMTP attachment, and the cancellation wording in all three locale files.
*Accept:* a confirmed booking delivered by email arrives with exactly one `session.ics` that a strict RFC 5545 reader parses; its start and end match the request in UTC with no `VTIMEZONE` and no `TZID`; two delivery attempts of the same row carry the same `UID`, so the client's calendar holds one entry; the file carries no online join link and no problem text for either modality; an onsite session's `LOCATION` is the clinic link and an online session's is the translated word; a summary containing a comma, a backslash and Armenian text survives folding and escaping intact; no other intent and no other channel produces an attachment; `request.cancelled.client` tells the client to remove the entry by hand.

**M13 — Week schedule.** The second view of `/admin/requests` from §12.2: `scheduled_in_window` and `unscheduled_for_admin` in `app/core/services/booking.py`, the `view=grid` branch, the template, and the styles. No migration and no new locale keys.
*Accept:* `/admin/requests?view=grid` renders seven day columns in the practice timezone, each holding its day's entries in local time order; a `confirmed`, a `completed`, a `pending` with a held slot, and a `negotiating` request all appear in the column matching their local date, and a `rejected` one does not; a `pending` request with only `desired_time_text` appears in the unscheduled list and in no column; `start=` moves whole weeks and snaps a non-Monday date to its Monday; a week containing a DST transition renders seven columns with every entry on its correct local day; last week's grid shows its `completed` sessions rather than rendering empty; the grid HTML contains no `problem_text`; `view=list` and its status filters are unchanged; the architecture test passes and `alembic heads` still shows one head.

---

## 20. Seed data

- One `practice` row from `PRACTICE_*` environment variables.
- One `admin_user` from `ADMIN_USERNAME` / `ADMIN_PASSWORD`, hashed; the plaintext **MUST NOT** be persisted or logged.
- `session_type`: `individual` (60 min), `couple` (60 min), both active, no price until the therapist sets one.
- `content_topic`: `work_terms`, `qualification`, `about_psychotherapy`, `references` — the last with `show_in_menu = false` (it is sent with waitlist confirmations). Topic titles are **not** a column; they come from translation keys `content.topic.<code>.title`.
- `timezone_option`: `Asia/Yerevan`, `Europe/Moscow`, `Europe/Kyiv`, `Europe/Berlin`, `Europe/London`, `America/New_York`, `America/Los_Angeles` — IANA names with friendly display labels, **not** UTC offsets.
- `translation`: every key from `locales/*.yaml`.
- No slots, no clients, no requests.

---

## 21. Explicitly out of scope

Do not implement, even if it seems natural: payments, client-initiated cancellation, calendar synchronisation, WhatsApp, multi-practice onboarding, session notes, file uploads from clients, analytics beyond the audit log. `practice_id` exists on every table but only one practice is served; do not build practice switching.

Also not to be built, now that §16.6 and §16.7 exist: a restore button or any other write path for dumps in the admin UI; a "back up now" trigger; a replace-mode config import that deletes rows absent from the file; slots in the config file; scheduled or automatic import from a watched file.

Also not to be built as part of §16.8's health checking: log shipping or a log-scanning pipeline; an external APM or error-reporting service; mounting the Docker socket into any application container; alert thresholds as admin settings; paging, escalation, or on-call rotation; and any check that colours the therapist's own workload.
