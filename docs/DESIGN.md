# Psychotherapy Booking Service — Design Document

**Version:** 2.0 (from-scratch rewrite)
**Audience:** humans — the maintainer, future contributors, and anyone deciding whether a change fits the architecture.
**Companion document:** `IMPLEMENTATION.md` — the build spec for an implementing agent. This document explains *what and why*; that one specifies *exactly how*.

---

## 1. Purpose and scope

A booking service for a single psychotherapist's private practice. Prospective and returning clients read practice information and request a consultation; the therapist stays in the loop for every scheduling decision.

The service is reachable through more than one channel. In this version: **a web application** and **a Telegram bot**, with **email** as an outbound-only channel. The architecture treats these as interchangeable front-ends over one shared core, so adding WhatsApp or any other channel later is a new adapter, not a new application.

### In scope

- Practice information as therapist-editable content, rendered natively per channel
- Consultation requests: slot-based booking, free-form time negotiation, and a waitlist
- Therapist approval, counter-proposal, rejection, and cancellation
- Automated session reminders
- Multilingual client-facing surfaces (Russian, Armenian) and an English admin surface
- Admin web UI for scheduling, content, translations, and settings

### Explicitly out of scope for this version

- Payments and invoicing
- Multiple therapists in one deployment (but see §18 — the schema is prepared for it)
- Client-initiated cancellation (therapist-initiated cancellation **is** in scope — see §14)
- Client-facing extras such as self-study material, homework, or session notes
- External calendar synchronisation (CalDAV, Google Calendar)
- WhatsApp or any channel beyond web, Telegram, and outbound email

### Non-goals

This is not a general-purpose CRM and not a clinical records system. It stores what is needed to schedule a session and nothing more. Any feature that would turn it into a store of clinical history is out of scope by design, not by omission.

---

## 2. What changed since v0.8 / v1.0, and why

The two earlier technical references were shaped by the practical limits of the tools writing and implementing them at the time: specifications had to be short enough to fit in one prompt, and implementations had to be simple enough to be generated in one pass. Several decisions were compromises to those limits rather than to the problem. Those limits no longer apply, so the following are corrected:

| Earlier decision | Problem | Now |
|---|---|---|
| Telegram-first; FastAPI bolted on in v1.0 | Domain rules lived inside message handlers; the web UI reimplemented them | Channel-agnostic core; every channel is a thin adapter (§3) |
| Landings stored as HTML files, sent with `parse_mode='HTML'` | Telegram accepts ~10 tags and caps messages at 4096 characters; real pages break or truncate | Content stored as ordered Markdown **blocks** in the database, rendered per channel (§10) |
| `am` used as the Armenian language code | `am` is Amharic; Armenian is `hy` | `hy` throughout |
| Timezones stored as fixed UTC offsets (`"UTC+3"`, `offset_minutes`) | Breaks for every European client twice a year at DST transitions | IANA zone names (`Europe/Moscow`, `Asia/Yerevan`), resolved with `zoneinfo` |
| `settings` as a singleton row with `id=1` | Only works if there is exactly one practice, permanently | `practice` row, referenced by `practice_id` on every table (§18) |
| `PendingNotification` queue polled by the bot | Solved the web→Telegram case only; email and future channels had no path | Generalised into a single **outbox** for all outbound messages on all channels (§12) |
| Reminders as `reminder_24h_sent` / `reminder_1h_sent` booleans | Cannot express arbitrary offsets, and a rescheduled session leaves stale flags | Explicit `reminder` rows with a due time and a state (§13) |
| `final_time` (string) alongside `scheduled_datetime` | Two sources of truth for the same fact | One `scheduled_start` (timestamptz); free-text survives only as the client's original *request* |
| Waitlist folded into `requests` with `type='waitlist'` | Forces most request columns to be nullable and pollutes the state machine | Separate `waitlist_entry` table with its own small lifecycle |
| `RequestType` and `SlotStatus` as code-level enums | Adding a session type ("supervision") means a migration and a deploy | `session_type` rows the therapist can edit; slot status stays an enum (it is a lifecycle, not a catalogue) |
| Naive `datetime.utcnow()` throughout | Deprecated in Python 3.12; naive values invite silent timezone bugs | Timezone-aware UTC everywhere; `timestamptz` columns |

Two structural problems in the v1.0 pre-alpha codebase also motivated a rewrite rather than a refactor: the repository contains a duplicated source tree (`app/app/...` alongside `app/...`, with diverging copies of `models.py`, `handlers/`, and `alembic/`), and the migration history has branched — two separate `002_` revisions both declare `001_v1_0_schema` as their parent. Untangling those would cost as much as starting clean.

---

## 3. Architecture

### 3.1 The shape

```
                  ┌──────────────────────────────────────────┐
   Telegram ─────▶│                                          │
                  │  inbound adapters                        │
   Web UI   ─────▶│  (translate a channel event into a       │
                  │   core use-case call)                    │
   (WhatsApp)────▶│                                          │
                  └────────────────────┬─────────────────────┘
                                       │
                  ┌────────────────────▼─────────────────────┐
                  │                  CORE                    │
                  │  domain model · state machines ·         │
                  │  use-cases · policies · domain events    │
                  │  (knows nothing about Telegram, HTTP,    │
                  │   SMTP, or HTML)                         │
                  └────────────────────┬─────────────────────┘
                                       │ domain events
                  ┌────────────────────▼─────────────────────┐
                  │  notification service → OUTBOX (table)   │
                  └────────────────────┬─────────────────────┘
                                       │
                  ┌────────────────────▼─────────────────────┐
                  │  outbound adapters (transports)          │
                  │  Telegram · Email · (WhatsApp) · Web-pull │
                  └──────────────────────────────────────────┘
```

The core exposes **use-cases** (`submit_booking_request`, `admin_propose_time`, `client_accept_proposal`, …) and emits **domain events**. It never formats a message, never builds a keyboard, and never touches a socket.

### 3.2 Why adapters must not be "the same UI, drawn differently"

Channels differ in capability, not just in styling. Telegram has inline keyboards and free-form messages of up to 4096 characters. Email has no interactivity beyond links and no guarantee of being read within a day. The web has full forms and immediate feedback. WhatsApp, when it arrives, will forbid free-form outbound messages outside a 24-hour window and require pre-approved templates.

So the core does not emit *messages*. It emits **intents** — a semantic key plus a payload plus a set of available **actions**:

```
intent:  request.proposal_received
payload: { request_uuid, proposed_start, session_type, therapist_note }
actions: [ accept, counter_propose, decline ]
```

Each outbound adapter decides how to express that. Telegram renders three inline buttons. Email renders three signed links. The web renders three form buttons. A future WhatsApp adapter maps it onto an approved template. Adding a channel means implementing one interface; it never means touching booking logic.

### 3.3 Process topology

Three containers, one image:

- **`web`** — the single ASGI ingress. Serves the client web UI, the admin web UI, the internal API, and the Telegram webhook as an ordinary route (`POST /channels/telegram/webhook`). Long-polling exists only as a development toggle.
- **`worker`** — outbox dispatch, slot-hold expiry, request expiry, reminder scheduling and firing, retention purge. Polls the database on a short interval.
- **`db`** — PostgreSQL.

There is deliberately **no separate bot process**. v1.0's split into a bot app and a web app is what forced the `PendingNotification` queue to exist as a cross-process message bus; with one ingress and one worker, the outbox serves its real purpose (durability and retries) rather than plumbing.

No Redis, no Celery, no external broker. At one therapist's volume, PostgreSQL with `SELECT … FOR UPDATE SKIP LOCKED` is a perfectly good queue, and it removes an entire service from the deployment.

---

## 4. Domain model in prose

- A **practice** is the deployment's single tenant: name, default language, therapist timezone, on-site clinic link, and all operational settings.
- A **client** is a person. They have a language, an optional timezone, and an optional display name. The name is whatever they typed; nothing is verified.
- An **identity** binds a client to one channel: `(telegram, 123456789)` or `(email, a@b.c)`. One client may hold several. This is what makes the client, rather than a Telegram ID, the single source of truth (§5).
- A **session type** is a bookable product: individual or couple, with a duration and a price. The therapist can add or retire types without a deploy.
- A **slot** is an offered start time of a given duration, optionally restricted to certain session types and to online or on-site. Its status moves through a small lifecycle (§8).
- **Join information** is how the client actually reaches the session: a clinic link for on-site, and for online either the practice's standing meeting room or a per-session link the therapist enters when approving. It is never sent by email (§12); email links to the request page instead.
- A **booking request** is a client's ask for a session. It carries the session type, modality, the client's timezone, an optional free-text problem description, and either a chosen slot or a free-text desired time. Its status is the heart of the system (§7).
- A **negotiation message** is one turn in the back-and-forth between therapist and client about when to meet, attached to a request.
- A **waitlist entry** is what a client leaves when the practice is closed to new bookings. It has no slot, no negotiation, and no reminders — hence its own table.
- A **content block** is one editable piece of practice information, in Markdown, in one language, belonging to a topic, at a position (§10).
- An **outbox message** is one thing to be delivered to one address on one channel, with retry state (§12).
- A **reminder** is a scheduled future notification tied to a confirmed request (§13).

---

## 5. Identity and authentication

### 5.1 Clients

The v1.0 model identified a client *by* their Telegram ID. That works until the web needs to serve someone without Telegram, at which point there is no place to put them.

The fix separates the person from the credential:

- `client` — the person, with a stable internal ID.
- `identity` — `(client_id, channel, external_id, verified_at)`.

A Telegram identity is created on first `/start` and is verified by construction: Telegram vouches for the user ID. An email identity is created on the web and is verified by a magic link — an unverified email must not be able to book, or the system becomes a way to send unsolicited mail in the therapist's name.

**Merging.** Every notification email includes a Telegram deep link of the form `https://t.me/<bot>?start=link_<token>`. Tapping it hands the token to the bot as the `/start` payload; the bot resolves the token and attaches a `telegram` identity to the client who already exists. This is the identity-merge path, and it costs the client one tap rather than a "please type your email into the bot" flow. The same mechanism works in reverse: a Telegram-first client who supplies an email later gets a verification link.

Tokens are single-use, short-lived, and stored hashed. Login tokens expire in 30 minutes; channel-link tokens in 24 hours.

### 5.2 Therapist

One admin account. Username plus password (Argon2id), server-side session with an httpOnly, Secure, SameSite=Lax cookie, and CSRF tokens on every mutating form. Telegram admin access remains gated by user ID from configuration, as before, because the bot has no other way to authenticate.

Rate-limit login attempts and magic-link issuance. Both are the only unauthenticated write paths in the system.

---

## 6. Booking modes

Three settings compose into the behaviour a client sees. Keeping them orthogonal is deliberate — the therapist should be able to turn off new bookings without losing the negotiation feature, and to run free negotiation without deleting her slots.

| `availability_on` | `booking_mode` | Slots exist? | What the client gets |
|---|---|---|---|
| `false` | — | — | Waitlist form only, plus the `references` content |
| `true` | `slots` | yes | Slot picker in the client's timezone |
| `true` | `slots` | no | Free-text time request if `fallback_to_negotiation`, otherwise waitlist form |
| `true` | `negotiation` | — | Free-text time request directly |

`auto_confirm_slots` (default off) allows a picked slot to confirm without therapist approval. It exists because it is one line of policy, but the default keeps the therapist in the loop, which is the point of the product.

---

## 7. Request lifecycle

```
                    ┌──────────┐
                    │  DRAFT   │  (in-progress form; never persisted for web)
                    └────┬─────┘
                         │ submit
                    ┌────▼─────┐
        ┌───────────│ PENDING  │───────────┐
        │           └────┬─────┘           │
        │ admin proposes │ admin approves  │ 48h elapsed / admin rejects
   ┌────▼────────┐       │            ┌────▼──────────────────┐
   │ NEGOTIATING │───────┼───────────▶│ REJECTED / EXPIRED    │
   └────┬────────┘       │            └───────────────────────┘
        │ client accepts │
        │           ┌────▼──────┐
        └──────────▶│ CONFIRMED │
                    └────┬──────┘
                         │ session time passes        │ admin cancels
                    ┌────▼──────┐              ┌──────▼──────┐
                    │ COMPLETED │              │  CANCELLED  │
                    └───────────┘              └─────────────┘
```

Rules worth stating explicitly, because they are where implementations drift:

- Only `PENDING` and `NEGOTIATING` may become `CONFIRMED`.
- `CONFIRMED` may only become `CANCELLED` or `COMPLETED`. There is no path back into negotiation; a change of time after confirmation is a cancellation plus a new request. This keeps reminders and slot bookkeeping honest.
- Entering `REJECTED`, `EXPIRED`, or `CANCELLED` releases any held or booked slot in the same transaction.
- `COMPLETED` is set by the worker once the scheduled end time has passed, not by anyone clicking anything.

---

## 8. Slots and time

All instants are stored as timezone-aware UTC in `timestamptz` columns. Conversion happens only at the edges: the admin enters slot times in the practice timezone, clients see them in their own.

Client timezone is determined by, in order: an explicit choice; the browser's `Intl.DateTimeFormat().resolvedOptions().timeZone` on the web; the stored value from a previous visit; the practice default. In Telegram there is no automatic source, so the client picks from a therapist-curated list of IANA zones with friendly labels ("Yerevan, Tbilisi, Dubai").

Slot lifecycle:

```
AVAILABLE ──hold──▶ HELD ──confirm──▶ BOOKED
    ▲                 │                  │
    └──── expire ──────┘                  │
    └──── release ───────────────────────┘

AVAILABLE ──admin blocks──▶ BLOCKED ──admin unblocks──▶ AVAILABLE
```

A hold lasts 15 minutes by default and exists to stop two clients picking the same slot while one of them is still typing their problem description. Holds are released by the worker when they expire. Booking and holding both take a row lock (`SELECT … FOR UPDATE`) on the slot; this is the one place in the system where concurrency genuinely matters.

`BLOCKED` covers "the therapist removed this slot but a request already references it historically" and lets slot deletion be a soft operation.

---

## 9. Negotiation

Retained from v0.8, and switchable — the therapist may not want it once slots settle into a rhythm.

A negotiation is an ordered list of messages on a request. Each has a sender (`admin` or `client`), a kind (`proposal`, `counter`, `accept`, `decline`, `note`), an optional structured `proposed_start`, and optional free text. Structured times are preferred: when the therapist proposes a real datetime, acceptance can confirm the request directly and schedule reminders. Free text remains legal because "some evening next week?" is a normal thing for a client to say, and forcing it into a datetime picker would be worse than storing the sentence.

Whose turn it is is derived from the last message's sender, not stored. There is no timeout on a negotiation beyond the 48-hour expiry that applies to unanswered `PENDING` requests; a `NEGOTIATING` request stays open until someone closes it. That is a deliberate choice — an automatic close would fire on exactly the clients who are slowest to reply, who are often the ones most worth waiting for.

---

## 10. Content

### 10.1 The model

Practice information — work terms, qualifications, about psychotherapy, references — lives in the database as **content blocks**, not as files.

A **topic** is a page (`work_terms`, `qualification`, …), configurable by the therapist. A **block** is one Markdown fragment within a topic, in one language, at a position. The web concatenates a topic's blocks into a page. Telegram sends them as separate messages, which is what makes conditional delivery possible — the bot can send the first two blocks now and the third only after the client picks "online".

This block granularity is the actual solution to Telegram's 4096-character limit. Splitting a long document automatically produces awkward breaks; splitting at boundaries the author chose produces a conversation.

### 10.2 Why database and not files on a shared volume

Markdown files in a mounted directory, rendered per channel, would work — the important half of the decision is the *format*, and Markdown-plus-renderer is correct either way. The database wins on the remaining points:

- Atomic writes for free. A file being rewritten can be read half-saved by the other container.
- Version history, so a paste that breaks a page at 23:00 can be rolled back.
- One backup story (`pg_dump`) instead of a database dump plus a volume snapshot.
- No shared writable volume between containers, which is what would otherwise pin `web` and `worker` to the same host forever.

The therapist edits blocks in the admin UI, so the "I want to edit files in a real editor" argument does not apply to her. It may apply to the maintainer, so import and export to `.md` files is available as an admin operation.

### 10.3 Rendering

Blocks are authored in a defined Markdown subset and rendered by a real converter — parse to an AST, then emit per channel. Passing raw Markdown to Telegram with `parse_mode='MarkdownV2'` fails on the first `.` or `-` in ordinary Russian text; MarkdownV2 requires escaping about eighteen characters and supports neither headings nor tables.

- **Telegram**: HTML parse mode with Telegram's supported tag subset. Headings become bold lines, bullets become `• `, horizontal rules become a divider line, tables are rejected at save time.
- **Web**: full HTML, sanitised through an allowlist.
- **Email**: plain text plus a minimal HTML alternative.

Unsupported constructs are caught when the therapist saves, with an explanatory error, rather than at send time in front of a client.

---

## 11. Localization

Two kinds of text, handled differently:

- **UI strings** (button labels, prompts, error messages) are developer-owned. They ship in the repository as the source of truth, seed the database on first startup, and are editable in the admin UI afterwards. Lookup order is: in-memory cache → database → repository defaults → the key itself. The repository fallback means a database problem degrades the wording, not the service.
- **Content** is therapist-owned and lives only in the database (§10).

The boundary matters in practice: v1.0 put "consultations run on Yerevan time, Fridays and Saturdays" into a UI string. That is practice policy, not chrome — it changes when her schedule changes, and it belongs in a content block she can edit, not in a translation key a developer owns.

Client languages: Russian (`ru`) and Armenian (`hy`). Admin UI and all operational errors: English. Adding a language is inserting rows and translating content, not changing code.

A missing translation in a non-default language falls back to the practice default language, then to the key. Missing keys are logged once per key per process, not per occurrence.

---

## 12. Notifications and the outbox

Every outbound message — Telegram, email, and any future channel — is written as a row in one `outbox_message` table inside the same transaction as the domain change that caused it. Nothing calls `bot.send_message()` from a request handler.

This buys:

- **Atomicity.** A confirmed booking and its notification either both happen or neither does.
- **Retries with backoff**, in one place rather than per call site.
- **Idempotency** via a dedupe key, so a worker restart mid-send cannot double-notify.
- **A delivery log** — what was sent, when, to which address, and what failed.
- **Multi-channel routing.** The same intent goes to a client's Telegram identity, or their email, or both, by policy rather than by branching code.

The worker claims batches with `FOR UPDATE SKIP LOCKED`, attempts delivery, and on failure schedules the next attempt with exponential backoff. After the final attempt a message is marked `dead` and the therapist is alerted through her own channel. Permanent failures (a client has blocked the bot) are distinguished from transient ones (a timeout) and are not retried.

**Email content is deliberately minimal.** Email is the least private channel in this system — shared inboxes, lock-screen previews, a partner reading over a shoulder. No problem description, no clinical detail, and a neutral subject line. Reminders carry the date and time in the body, since a reminder that requires a click is not a reminder; status changes may be as little as "There is an update on your request →". Telegram messages may be more detailed, being already on the client's own device.

---

## 13. Reminders

When a request is confirmed, the worker creates explicit `reminder` rows for each enabled offset (24 hours and 1 hour by default), each with a due time and a state. When a request is cancelled or its time changes, pending reminders are cancelled and recreated.

Rows rather than booleans, because booleans cannot express a third offset, cannot survive a reschedule cleanly, and cannot record why a reminder was skipped. A reminder whose due time is already in the past when the request is confirmed is marked `skipped`, not fired late.

The worker's sweep is the only mechanism; there is no in-memory scheduler holding jobs that a restart would lose. This is why APScheduler is absent from the stack — a database-backed due-time query does the same work and survives a deploy.

---

## 14. Cancellation

Therapist-initiated cancellation ships in this version and is not optional: without it, a confirmed booking has no exit that releases its slot, and the therapist has no recourse when she is ill. Cancelling sets a reason, releases the slot, cancels pending reminders, and notifies the client on every channel they have.

Client-initiated cancellation is deferred. The data model accommodates it — `cancelled_by` already distinguishes actor types — so enabling it later is a UI surface plus a policy check against `cancel_window_hours`, not a migration.

---

## 15. Admin surface

The web UI is the primary admin surface:

- **Requests** — filter by status, view the full negotiation thread, approve, propose, reject, cancel; and the same requests as a **week schedule** (below)
- **Slots** — create in bulk (a weekly pattern over a date range) and individually, block, delete
- **Waitlist** — mark contacted, convert to a request, close
- **Content** — edit blocks per topic and language, reorder, preview per channel, view and restore revisions
- **Translations** — edit UI strings, see which keys are missing per language
- **Settings** — availability, booking mode, prices, session types, reminder offsets, timezone list
- **Delivery** — recent outbox messages and failures, so "did she get my message?" is answerable

Telegram retains a reduced admin surface for the operations that matter when away from a desk: toggle availability, view pending requests, approve, propose, reject. Content editing and settings are web-only; a phone keyboard is the wrong tool for them.

The dividing line is **triage, not administration**. What belongs on the phone is time-sensitive and decidable in a tap or two: a request arrived and wants an answer, a session is today and has to be cancelled because she is ill, availability has to go off for a week. What belongs on the web is anything that means composing text or configuring the practice, because on a phone that is a worse version of a form she could fill in properly later.

Two consequences follow, and both are worth stating because they are easy to get wrong. First, the phone surface is a *panel that navigates*, not a series of one-shot commands: every screen offers the way back, so the therapist is never left holding a message with nothing to press — the earlier version answered "Confirmed 7f3c…" and left her there. Second, the panel edits its own message rather than sending a new one, because a triage tool used a dozen times a morning must not bury the chat it lives in. Neither is a feature so much as the price of the surface being usable at all.

The requests list answers *what needs answering*. It is ordered newest first, because a queue is worked from the top. It cannot answer *what does my week look like*, and no amount of filtering makes it: a list sorted by arrival says nothing about the shape of Thursday. So the same requests are also offered as a **week schedule** — seven day columns, each holding that day's sessions in time order. It is a second view of one query, not a second feature: no new table, no new state, and every entry links back to the request page that already exists.

It is deliberately a day-column agenda rather than an hour-ruled grid. An hour-ruled week lets two weeks be compared by eye, which is a real advantage, but it pays for that in empty rows — a practice this size books a handful of hours a day, so most of the ruling is blank, and the blankness is what the eye lands on first. The judgement was that the noise costs more than the comparison is worth, and it was made by the person who reads it every morning.

The schedule is **read-only**. Dragging a session to another hour looks obvious and is not: rescheduling means renegotiating with a client through the same proposal machinery as everywhere else (§9), and a view that quietly rewrote `scheduled_start` would go around it. A cell is a link to the request; the request page keeps the actions.

Three things follow, each of which would otherwise be a quiet defect:

- **Finished sessions stay on it.** A confirmed session becomes `completed` once its end time passes (§7), so a schedule showing only confirmed work would render last week as empty — a plainly false statement about a week that happened. `completed` is shown, muted.
- **Requests with no time yet are shown beside it, not on it.** A free-text request carries wording rather than an instant ("some evening next week?"), and there is no honest cell for it. Dropping it from the view would hide exactly the requests most in need of an answer, so they get their own list underneath.
- **It is not calendar synchronisation.** §20 defers a subscribable feed of the therapist's whole schedule, and §1 puts external calendar sync out of scope. This is neither: nothing leaves the server, nothing is fetched, no external calendar is involved. It is a way of drawing rows the admin UI already reads.

---

## 16. Data protection

Names are unverified and no identity documents are collected, so the sensitive field is one: the free-text problem description a client writes when requesting a consultation. Combined with a stable identifier, that is health-related information about an identifiable person, and it deserves proportionate handling — not a consent-management subsystem.

Concretely:

- Message bodies and problem text never appear in application logs. Log identifiers, not content.
- A retention job deletes problem text and negotiation bodies a configurable number of months (default 12) after a session or a terminal status, leaving the row for statistics.
- Disk and database backups are encrypted at rest, and backups are tested by restoring them.
- Problem text is visible only through the authenticated admin UI and the therapist's own Telegram account.
- Email never carries it (§12).
- An admin operation exports or erases everything associated with one client, so a request to be forgotten can be honoured without a database console.

This is a half-page of policy and one worker job. It is not legal advice, and if the practice takes EU clients at volume the question deserves a real answer from someone qualified.

---

## 17. Deployment

One VPS, Docker Compose, one image. The service is responsible for its own TLS by default — no external proxy is assumed and none needs configuring by hand.

The default profile runs Caddy in front of the app, which obtains and renews a Let's Encrypt certificate for the configured domain automatically. v1.0's Nginx Proxy Manager arrangement is dropped: it was manual setup work that a four-line Caddyfile does by itself.

Two other profiles exist for people who already have an ingress. A `plain` profile serves HTTP on the loopback interface only, for use behind an existing reverse proxy. Adding `cloudflared` runs a Cloudflare Tunnel alongside it, with no inbound ports opened at all. Cloudflare is therefore an optional module, not a prerequisite.

Because the Telegram webhook needs a publicly reachable HTTPS URL, a deployment running `plain` with nothing terminating TLS in front of it will refuse to register a webhook and will say so, rather than failing silently — long-polling is the supported fallback there.

The webhook is verified with a secret header token and its URL contains a random path segment.

Configuration is entirely environment variables, with `.env.example` committed and `.env` **git-ignored** — the one operational fix carried over from v1.0, where `.env` was tracked. Values there are placeholders today, but a tracked `.env` becomes a leak the first time someone fills it in and runs `git add .`.

Backups: a `backup` sidecar container takes a nightly `pg_dump` into a directory bind-mounted from the host, prunes by age, and nothing on the host has to be configured for that to happen (§21). With content in the database, that single dump is the complete state of the service. Copying it off-host and encrypting it remains the operator's job — the container cannot know where "off-host" is.

---

## 18. Readiness for multiple practices

This version serves one therapist. Every table nonetheless carries a `practice_id` foreign key, and one practice row is seeded at install. The column is unused today and costs nothing; retrofitting a tenant key across a live schema with real bookings in it is genuinely painful.

What would still be required to serve several therapists — and is therefore *not* claimed as done — is per-practice bot tokens and webhook routing, an onboarding flow, admin accounts scoped to a practice, and a public directory or per-practice subdomain. The schema will not fight any of that.

---

## 19. Decisions and rejected alternatives

| Decision | Alternative considered | Why |
|---|---|---|
| Core with channel adapters | Telegram-first with a web view | The web UI otherwise reimplements booking rules; a third channel would triple the drift |
| Single ASGI ingress, webhook as a route | Separate bot and web processes | v1.0's split is what forced a cross-process notification queue to exist |
| Postgres-backed outbox and job sweep | APScheduler; Celery + Redis | In-memory schedules die on deploy; a broker is a whole service for a handful of jobs a day |
| `client` + `identity` tables | Telegram ID as the client's primary key | Otherwise a client without Telegram cannot exist; also gives a natural WhatsApp path |
| Content blocks in the database | Markdown files on a shared volume | Atomicity, revisions, one backup; no shared writable volume between containers |
| Markdown subset with a real renderer | Raw HTML per channel; MarkdownV2 passthrough | Telegram's tag subset and escaping rules break on ordinary Russian punctuation |
| Separate `waitlist_entry` table | `type='waitlist'` on requests | Waitlist has no slot, negotiation, or reminders; folding it in makes half the columns nullable |
| `session_type` as data | Enum in code | Adding "supervision" should not need a migration |
| Explicit `reminder` rows | `reminder_24h_sent` booleans | Booleans cannot survive a reschedule or express a third offset |
| IANA timezones | UTC offset strings | DST |
| aiogram 3 for the Telegram adapter | python-telegram-bot | The state machine lives in the core; PTB's `ConversationHandler` wants to own flow control. Either library works if the adapter stays dumb |
| Prices as structured amount plus currency | Free-text `"50 USD / 60 min"` | Nearly free now, and required the moment payments appear |
| Caddy with automatic Let's Encrypt as the default ingress | Nginx Proxy Manager, as in v1.0 | NPM was manual setup work; a four-line Caddyfile replaces it, and a tunnel or an existing proxy stay available as profiles |
| Practice-wide meeting room with a per-request override | A single fixed room; or a link entered every time | Covers both a therapist with one standing room and one who generates a link per session |
| Configuration export as JSON with natural keys | A database dump used for both purposes | A dump carries clients' problem text; a config file must be safe to email to whoever is rebuilding the install |
| Import merges, never deletes | Replace-the-world import | A file from an older install would silently delete a topic the therapist added afterwards |
| A `backup` sidecar container | A host cron entry (the original §17 answer); a worker sweep job | The operator should not have to configure anything on the host; and a backup that stops when the app crashes is a backup that stops when you need it |
| Restore by CLI only | A restore button in the admin UI | One click that overwrites the database has no safe failure mode, and restore happens once a decade under supervision |
| Health checks read the database | Scanning container logs for errors | Logs are unstructured, are lost on `docker compose down`, and describe the same failures the tables already record exactly |
| Check results in a JSON file | A row in the table being monitored | A checker that reports into the database cannot report that the database is unreachable |
| A file, not SQLite | SQLite beside Postgres | A second engine with its own locking and corruption modes, for one 2 KB record |
| Exceptions recorded as class and location | Message and traceback | An exception message can carry an email address or a fragment of problem text (§16) |
| Staleness of the status file is the liveness check | A heartbeat column | Free, needs no migration, and catches a wedged worker as well as a dead one |
| External uptime check for total failure | Alerting directly from the worker when the database is down | A direct send breaks the outbox rule and only ever runs on the worst day of the year |

---

## 20. Deferred

Roughly in the order they would be worth doing:

1. Client-initiated cancellation and rescheduling
2. WhatsApp adapter (Cloud API; note the 24-hour window and template approval)
3. Payments and payment instructions per language
4. **The therapist's** calendar: export and synchronisation of her whole schedule (a subscribable `.ics` feed first, CalDAV later). Not to be confused with the per-session file a client gets attached to a confirmation email, which is built (IMPLEMENTATION.md §13.5)
5. The same per-session calendar file on Telegram, which needs `send_document` and a transport that can carry an attachment. Worth doing only after checking the hand-off: an `.ics` sent into a Telegram chat opens in a calendar app reliably on iOS and unreliably on Android, and a file the client cannot open is worse than no file
6. The week schedule (§15) on Telegram. **Needs design thought and testing with the therapist before any code is written**, because the obvious answer does not fit. A grid needs monospace, so it needs `<pre>` — the only tag in the supported subset that holds alignment (hard rule 6) — and a phone shows roughly 30 to 40 monospace characters before wrapping or side-scrolling. Seven day-columns do not fit in that, and the version that renders correctly on a desk will be unreadable on the device the surface exists for. The likely shape is therefore not a grid but a day-grouped agenda — a heading per day, its sessions beneath, empty days collapsed to one line — which is a different design and should be judged as one rather than as a degraded grid. Two things to settle by trying it rather than by reasoning: whether a day-grouped agenda actually beats the flat soonest-first list already at `asess:7` (§13.2), and how many days belong on one screen given the 4096-character limit and the current cap of ten sessions. Until both are answered from use, the honest Telegram answer is the existing list plus a link to the web schedule, which is what §13.2 already does for every web-only capability
7. Recurring clients: session history, prepaid packages
8. Client self-study materials as a content type
9. Multi-practice operation (§18)
10. Statistics for the therapist: conversion, no-shows, load by weekday

### 20.1 Known weaknesses, accepted for now

Not features waiting to be built — things we have looked at, understood, and
decided not to act on yet. Recorded so the next person finds the reasoning
rather than the surprise.

**Raw tokens live in `outbox_message.payload`.** `issue_token` promises that
only a hash is persisted, so a database leak hands out no live tokens
(IMPLEMENTATION.md §6.2). The notification service then puts the raw
`view_token` into the outbox payload, and the login link carries its raw token
the same way — so the outbox undoes for its own rows what the token table is
careful about, and every nightly dump (§21) contains live view and login tokens
until they expire or are consumed. Closing it properly means minting the token
in the worker at send time rather than at enqueue time, because a raw value
cannot be recovered from a hash. That is a real change to who mints tokens, and
the exposure is bounded: single-use, short-lived, and behind a `0700` backup
directory. Revisit when the outbox grows a pruning job, or if dumps ever leave
the host.

**Nothing reserves a slot while the client fills in the form.** `/book/hold`
records the choice against the flow and takes no database hold, because §6.4
makes `held_by_request` non-null whenever a slot is held — a real hold needs a
request row, and creating one there would notify the therapist about a form
nobody has filled in. So two clients can be on the same slot at once, and the
loser is told at submit (`booking.slot.taken`); the M2 concurrency test
guarantees there is exactly one winner. `slot_hold_minutes` therefore names the
window a client *should* finish in rather than one anything enforces, and the
wording on the page says so instead of promising a reservation. Closing it
properly wants a nullable hold owner or a two-phase submit, plus the same
treatment on Telegram, which has no pre-submit reservation at all. Worth doing
when contention stops being hypothetical — one therapist and a handful of
requests a day is not where two clients race for one slot.

**A `view_request` token burns on `GET`.** Opening `/r/{uuid}?token=…` consumes
the token on first paint, so anything that fetches the URL before the client
does spends it — and mail scanners on some corporate systems do exactly that,
leaving the real client at the sign-in form with a magic-link allowance of three
an hour (§17). Tested against a personal Gmail account and the behaviour did not
appear; the risk is specific to link-scanning gateways rather than to consumer
mail. Any fix moves when the token burns, which §6.2 pins as single-use, so it
is not worth changing on a hypothetical. Revisit if a client reports a dead
link, and treat "the therapist's clients are on corporate mail" as the trigger.

---

## 21. Portable configuration and backups

Two different problems get confused with each other, so this section separates them before answering either.

**The first problem is moving an installation's *character* to another installation.** Everything the therapist typed into the admin UI — the settings, the session types, the timezone list, the topics and their blocks in three languages, the edited translations — represents weeks of writing that exists nowhere else. A fresh `docker compose up` produces a working service with seed defaults and none of that voice. Rebuilding on a new VPS, standing up a staging copy, or handing the install to someone else should not mean retyping it.

**The second problem is losing the database.** That is not about voice; it is about clients, bookings, and the audit trail. It needs a periodic physical dump, and the dump contains health-related free text.

### 21.1 Why these are two mechanisms, not one

The obvious economy is to use one thing for both: take `pg_dump` and call it the export. It is wrong for the first problem in two ways.

A dump carries `problem_text`. The whole point of §16 is that this field travels as little as possible; a "config export" that a therapist might email to whoever is helping them migrate cannot contain it. The config export contains no client data at all, which is what makes it safe to keep in a password manager or attach to a message.

A dump also carries primary keys, sequences, and the schema of the version that produced it. Restoring one into a newer install means matching migration state. The config file matches on **natural keys** instead — a topic by its `code`, a translation by `(lang, key)`, a block by `(topic, lang, position)` — so a file exported before three migrations still imports afterwards, and rows that gained a column simply take its default.

Hence: a small, human-readable, client-data-free **config file** for the first problem, and physical **dumps** for the second.

### 21.2 What the config file is, and what it deliberately is not

It holds the practice settings, session types, timezone options, content topics and blocks, and translations. It does not hold clients, requests, waitlist entries, outbox rows, reminders, the audit log, admin credentials, or content revision history.

It also does not hold **slots**. Slots look like configuration from the admin page but they are dated availability: importing last winter's Tuesday afternoons into a fresh install in August produces garbage, and a booked slot cannot be transplanted away from the request that holds it. The therapist recreates a week of slots in one bulk action, which is cheaper than making the file mean two things.

### 21.3 Why import merges and never deletes

Import upserts what the file names and leaves everything else alone. The alternative — make the database match the file exactly — is a better clone and a worse tool. Files are kept and reused; the moment someone imports a six-month-old file to restore one topic's wording, replace-semantics would silently delete every topic added since. Merge fails in the recoverable direction: the worst outcome is stale text that can be edited, rather than absent text that has to be rewritten.

Two things make merge safe to run on a live install. It applies in a single transaction, so a file that fails validation halfway leaves nothing behind. And a changed content block writes a revision through the same path the editor uses, so the existing per-block rollback undoes a bad import without any new machinery.

Validation is strict on arrival and rejects the whole file rather than importing part of it: an unrecognised format or version, a language outside `ru`/`hy`/`en`, a settings field that `update_settings` would refuse, an unknown enum value. A translation key that does not exist in the code's catalogue is reported and skipped rather than being written — a key no renderer reads is dead weight, and its presence usually means the file came from a *newer* version than this one.

### 21.4 Why backups are a container and not a host cron entry

The original answer was a documented `crontab` line on the host. It works and costs nothing to specify, which is exactly why it was chosen — and it fails in practice for the operator this project actually has: one person, one VPS, who installed with `docker compose up` and has no reason to know that a second, invisible piece of setup exists on the host. A backup that depends on the operator remembering to configure it is a backup most installs do not have.

A sidecar service in the same Compose file is set up by the same command that starts everything else. It runs the same `postgres:16` image as the database, so `pg_dump` and the server can never drift apart in version — the most common way a dump turns out to be unrestorable. It writes to a host bind mount, so the files are reachable both through the filesystem and through the admin UI, and prunes beyond the retention window.

The rejected alternative was a job in the worker loop, which would have obeyed the "no scheduling outside a database sweep" rule literally and could have recorded each run in a table. It loses on independence: the worker is application code that can crash-loop on a bad deploy, and the backup is precisely what you want to still be running when it does. That rule exists so that *domain* work — reminders, expiry — survives a restart with its state in the database. A dump has no such state; the evidence that it ran is the file.

### 21.5 Why the admin UI downloads dumps but cannot make or restore them

Downloading matters: the therapist may not have shell access to the VPS, and "the backups exist but only root can see them" is not a recovery plan. The admin page therefore lists what is in the directory and streams a file back, read-only.

There is no "back up now" button. It would put the web container in the backup path for a case the config export already covers — the reason to want one is almost always "I am about to change a lot of text", and a config export is the right snapshot for that, being smaller, safer, and readable.

There is no restore button either, and this one is not a matter of cost. Restore overwrites every booking made since the dump. It is a supervised operation that happens once a decade, it needs the application stopped first, and a control that does it behind one click in a browser is a hazard with no compensating benefit. Restore stays a documented CLI procedure, and the README keeps requiring a rehearsal onto a scratch database — an untested backup is a hypothesis.

### 21.6 The consequence for data protection

The dumps contain problem text, so the backup directory is a new place where §16's sensitive field lives at rest. That is stated rather than avoided: the directory needs restrictive permissions, the download route is behind admin authentication over TLS, and encrypting and copying off-host remains the operator's responsibility. The config file, by contrast, is designed to carry nothing sensitive at all — which is the whole reason it exists separately.

---

## 22. Knowing when something is wrong

Every other section of this document is about the service working. This one is about the therapist finding out when it is not, without an operations team and without a terminal.

### 22.1 What failure looks like in a practice this size

The failures that matter here are silent. A client who never receives a confirmation does not file a ticket; they conclude the practice is disorganised and book with someone else. A reminder that never fires produces a no-show three days later that looks like an ordinary no-show. A backup container that died in February is indistinguishable from a healthy one until the day it is needed.

None of these announce themselves. All of them are visible in the database within seconds of happening. The gap is not detection — it is that nobody is looking, and that the person who would look does not know what a `dead` outbox row is.

So the goal is narrow and worth stating plainly: **the therapist should learn, in one glance and in plain language, whether to carry on or to call the person who runs the server.** Everything below serves that sentence, and anything that does not serve it is out of scope.

### 22.2 Why this is not built on logs

The obvious approach is to scan the container logs for errors. It is the wrong source here, for reasons that are specific rather than stylistic.

Logs in this deployment live inside containers and are lost on `docker compose down` unless a logging driver keeps them. They are unstructured, so any check becomes a regular expression matched against English sentences that change whenever someone edits a log line. They carry no severity contract — a `WARNING` from a library is not a reason to alarm a therapist.

And they are redundant with something better. This system was deliberately built so that failure is *durable state*: an undelivered message is an `outbox_message` row with a status, an attempt count, and the error that caused it (§12). A missed reminder is a `reminder` row still `scheduled` after its `due_at`. Reading those tables is more precise than parsing the log line emitted beside them, and it survives a restart.

Logs remain what they are: the thing the IT person reads *after* the light turns red, to find out why. A diagnostic medium, not a monitoring input.

There is one genuine gap the tables do not cover. An unhandled exception in a web request leaves no trace anywhere — the client sees a 500 and leaves — and a worker job that throws is caught on purpose so that one bad job cannot stop the others (§14), which means it can fail every twenty seconds for a month while nothing accumulates. The answer is still not to read logs: it is to **record the exception where it is already being caught**, as a row. Same durability as everything else, no parsing.

### 22.3 Why the health record is a file beside the database, not a row inside it

The first design put the check results in a table. That is circular: the most consequential thing the checker can discover is that the database is unreachable, and a checker that reports into the database cannot report that.

So the worker writes its findings to a small JSON file on a mounted volume, atomically — a temporary name, then a rename, the same trick `backup.sh` already uses so that a half-written file is never read. The file is readable when Postgres is not, and it is readable over SSH with `cat`, which is exactly the situation the IT person is in when the admin UI will not load.

SQLite was the other candidate and is rejected: a second database engine, with its own locking semantics and its own corruption modes, to hold a single record of about two kilobytes. A file is the smaller idea.

The file also solves worker liveness for free, which the table version would have needed a heartbeat column for. The worker rewrites the file every pass. **If the file is older than a few minutes, the worker is dead** — that one fact replaces a set of heuristics about work piling up, and it catches a wedged worker as readily as a crashed one.

Note what this does *not* change: exception records still go in the database. They originate in the `web` process and are consumed by the `worker` process, and Postgres is the one thing both already share. Routing them through a file or a socket instead would rebuild the cross-process queue that v1.0 had and that §3.3 exists to avoid.

### 22.4 What a colour is allowed to mean

A light that cries wolf is ignored within a week, and an ignored light is worse than none — it converts a real alarm into background furniture. Two rules keep it honest.

**Red means clients are affected right now. Amber means something will break, or is degraded but working.** A dump that has not been taken for two days is amber: nothing is broken today, and on the day it matters it will be far too late. A `dead` outbox row is red: a specific person did not receive something the practice promised them.

**The therapist's own queue is never coloured.** Ten unanswered requests is not a fault; it is Tuesday. Colouring it teaches the eye that the dot means "there is work", which is precisely the meaning that makes it useless for "there is a fault".

A colour on its own is also not enough to act on. Every check produces two sentences: one for the therapist, in the language of consequences ("Messages have stopped going out; three people have had no reply since 14:20"), and one for whoever runs the server, in the language of causes (`outbox: 3 dead, oldest 2026-08-26T14:20Z, last error: SMTP 535`). The first tells them whether to call. The second is what they read down the phone.

### 22.5 Recording exceptions without recording what people wrote

An exception message is untrusted text. `ValueError: invalid address for lena@example.com`, or a `KeyError` carrying a fragment of somebody's problem text, would put exactly the content §16 protects into a new table, and from there onto a status page.

So the record is the exception's **class, module, and line** — never its formatted message, never a traceback. That is enough to tell one recurring failure from another, and enough for the IT person to find it in the logs, which do have the detail, in the place where detail belongs.

### 22.6 What this cannot see, stated rather than implied

If Postgres is down, no admin page renders at all: every page needs it for the session and the navigation. The outbox cannot be written either, so no alert can be sent. Sending directly from the worker in that case would break the rule that every outbound message is a durable row (§12), and would add an untested code path that only ever runs on the worst day of the year.

The correct answer is outside the box and costs nothing: an external uptime check against `/readyz`, which already fails when the database does not answer. A green dot means "everything visible from inside this deployment is fine" — it can never mean "the site is reachable from the internet". That distinction belongs in the README, not in a footnote.

Similarly, the containers cannot inspect each other. Reading `docker compose ps` from inside `web` would mean mounting the Docker socket, which is root on the host handed to the process most exposed to the internet. Container death is inferred from its consequences instead — a dead worker stops writing the file, a dead backup container stops producing dumps — which is both safer and more meaningful, since a container can be running and useless.

### 22.7 On proving the database is healthy

A checker can confirm cheap invariants: the Alembic version matches the code, exactly one practice row exists, a handful of counts that should be zero are zero. Real corruption checking is `pg_amcheck` territory and does not belong anywhere near a page render.

But the corruption that actually ruins a practice is not in the live database — it is in the dumps, discovered on the one day they are needed. So `backup.sh` verifies each dump immediately after writing it by asking `pg_restore` to list its contents. It costs about a second, and it turns "we have backups" into "we have backups that could be read this morning", which is the only version of that sentence worth having.
