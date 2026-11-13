# Project review — 2026-10-03

## Status 2026-11

What the NewPBot hardening slices closed. §7 deviations are the owner's call and
stay open; so do the items not listed here.

| Finding | Closed in |
|---|---|
| Double-booking: guarded slot release, refused submit rolled back, proposal for the held time, request lock, token consumed once (review 2026-11 findings 1, 2, 6, 17) | NP1 |
| Unverified email held a slot 48 h; per-IP booking limit; captcha on (review 2026-11 finding 3) | NP2 |
| Line break in an address broke the outbox batch; alert-about-alert loop; outbox dead after 62 minutes, stuck `sending`, SMTP timeout, Retry button; Telegram "unauthorized" transient (findings 4, 5, 7, 8; C1) | NP2 |
| Login links spent by a GET (finding 11); C4, C5 and the C15 bullets on empty translations, escaping, splitting, the Bot per pass, waitlist purge | NP2 |
| Backup freshness read mtimes; 36 h warning silent; no dump at start after an outage (finding 9) | NP3 |
| Argon2 on the event loop; no global login cap; `X-Forwarded-For` trusted from anyone (finding 10, C2) | NP3 |
| CSRF token unbound and unexpiring; empty webhook secret accepted; `/readyz` leaked the exception class (findings 15, 16, 18) | NP3 |
| Config upload buffered before the cap (C3); no security headers and `build-essential` in the image (C15) | NP3 |
| `sh -c` as PID 1; backup container held all of `.env`; cloudflared `:latest`; no limits, log rotation, Postgres timeouts; retention jobs every 20 s (findings 13, 14; review § Tuning) | NP3 |

Not done: the owner's calls in §7, a heartbeat for a dead worker (finding 18's
first bullet), encrypted off-box backups, a restore check deeper than the table
of contents, and the remaining C15 duplicated-rule bullets.

---

Reviewed at commit `0fabdd8` (master, clean tree). The deployment on this
machine was running during the review and was read, not changed.

## 1. Scope and method

What was read in full: the normative sections of both specification documents
(§7, §8, §9, §10, §14, §15, §17, §18, §19 of `IMPLEMENTATION.md`; §20 of
`DESIGN.md`), `CLAUDE.md`, the core services the booking lifecycle touches
(`booking.py`, `slots.py`, `clients.py`, `policies.py`, `translations.py`,
`notifications.py`), the whole worker (`main.py`, `jobs/outbox.py`,
`jobs/sweeps.py`), both transports, `app/main.py`, `app/config.py`,
`app/db.py`, the Compose files, the Dockerfile, `backup.sh`, the CI workflow,
and the test fixtures. The three channel adapters (Telegram router, web client
and admin routes, rendering) were each reviewed in full by a parallel
sub-review; every finding taken from those was re-read in the code before it
was included here, and two were checked against the installed library source.

What was run inside the `web` container with the dev overlay:

| Check | Result |
|---|---|
| `ruff check app tests` | clean |
| `mypy app` | clean, 56 files |
| `alembic heads` / `alembic check` | one head, no model drift |
| `pytest tests/core tests/channels` minus the three suites that commit | 674 passed, 14 skipped, 1 failed |

**The end-to-end suite and the three core suites that open their own
transactions were not run.** The container's `DATABASE_URL` is the
deployment's database with the therapist's clients in it, and those suites
commit (the README says so, and says not to point them at a live database).
The one failure that did occur is itself a product of the shared database; it
is finding B1.

Hard-rule greps (`utcnow`, `am`, `MarkdownV2`, direct sends, forbidden imports
under `app/core/`) came back clean apart from comments and test assertions.

## 2. Verdict

This is a well-built codebase that does what its specification says, and the
specification is unusually honest about what it has chosen not to do. The
architecture rule the project exists to enforce holds: every state transition
goes through `app/core/services/booking.py`, every outbound message is an
outbox row, and the architecture test guards the import boundary. The test
suite is large, specific, and indexed to the spec.

The problems found cluster in four places:

1. **Two things are wrong in the running deployment now.** The practice's SMTP
   credentials are being rejected, and a classification bug turns that into
   dead messages on the first attempt, so a real client has missed two
   reminders (A1). An Armenian prompt is blank because the translation editor
   saved an empty string (A2).
2. **Two robustness gaps in the worker and the webhook** that have not fired
   yet but will under ordinary conditions: one raising message strands the
   rest of its batch in `sending` (C1), and a Telegram edit or redelivery is
   fed to the wrong step (C5).
3. **Two §17 claims that do not hold on the deployed profile**: per-IP rate
   limits trust a header the caller writes (C2), and the config upload is
   buffered to disk before the size cap (C3).
4. **The test suite shares the live database** (B1), which the project knows
   and which has started to cost it.

Nothing found contradicts the design. The recommendations in §8 are short and
ordered.

## 3. What is done well

- **The core is actually the core.** `ALLOWED` in `booking.py` is the §7.1
  table as data. Both channels call the same functions with the same
  arguments. `client_decline_to_waitlist` refuses to let a route compose two
  core calls, and says why.
- **Row locking is where the spec says.** `slots._lock` runs before any status
  check; `_matching_slot` locks its `SELECT` too, with the race it closes
  written beside it.
- **The outbox is durability, not plumbing.** Claim commits before send, one
  transaction per message, `sending` left visible on a crash, and a health
  check (`outbox_stuck`) that watches for exactly that state. Dedupe is used
  where a replay is legitimate (`enqueue_raw`, health alerts) and allowed to
  fail where it is not (`enqueue`).
- **Authorization is centralised and correct** on both interactive channels:
  `_request_for` on every Telegram client callback, admin ids checked at the
  top of every admin path, 404 rather than 403 for a foreign UUID on the web.
  CSRF is on every mutating form in both surfaces and checked before any
  database work. `|safe` appears only on sanitiser output.
- **A client cannot inject markup into the therapist's chat.** Intent bodies
  are escaped after formatting and packed, never parsed as Markdown.
- **Time handling is careful.** Per-day conversion in `create_slots_bulk`,
  local-hour bucketing in `taken_hours_on`, DST explicitly tested in the week
  grid, octet-aware folding in the iCalendar emitter.
- **Data protection is designed in.** `erase_client` nulls every free-text
  field, cancels future sessions, deletes identities and flows; the audit log
  records counts, not content; `error_event` stores class and location, never
  the message; backups are `0700` and verified with `pg_restore --list` before
  they are given their final name.
- **The spec is kept honest by the suite.** `test_spec_coverage.py` fails when
  a section of `IMPLEMENTATION.md` is claimed by no test; `test_help.py` fails
  when the admin guide's stamp drifts; `test_schema.py` asserts
  `CLIENT_OWNED_TABLES` against the schema.
- **Operational thinking.** The health file is readable with the application
  down; the backup container shares the server's image so `pg_dump` cannot be
  newer than the server; the `tls` profile refuses to start without a domain
  rather than serve plain HTTP for every host; CI brings the `plain` stack up
  and asserts nothing is published on `0.0.0.0`.
- **The commit history is small-grained** (133 commits, 42 feat, 40 fix,
  21 docs) and the messages say why.

## 4. Findings — affecting the deployment now

### A1. SMTP authentication failures are classified as permanent — HIGH

`app/channels/email/transport.py:28` treats every SMTP `5xx` as permanent. The
live database holds two `dead` rows, both `reminder.client` emails to one real
client, both with `535 5.7.8 Username and Password not accepted`. That code
means the *practice's* mail credentials were rejected; it says nothing about
the recipient. The rows were given up on at attempts 1 and 2, the client got
neither reminder, and the status page has shown `outbox_dead` red since
2026-09-26.

Two things need fixing. The deployment's SMTP password (or app password) is no
longer accepted by the provider and must be corrected by hand. And while it
stays broken, every email the practice sends dies immediately, which is the
opposite of what retry-with-backoff exists for.

Fix: catch `aiosmtplib.SMTPAuthenticationError` and `SMTPConnectError` before
the generic `SMTPResponseException` branch and return `transient`; keep `5xx`
permanent only for recipient-level codes (`550`, `551`, `553`, and
`SMTPRecipientsRefused`). A test with a mocked `535` asserting `transient`
would have caught this. Consider one admin alert per outage rather than one
per message.

Related: the Delivery page has no retry control (searched `admin.py` for
retry, requeue, redeliver: none). Once A1 is fixed the two dead reminders stay
dead; the only recovery is SQL.

### A2. An empty translation value is saved and served as "" — MEDIUM

`admin.py:942` `translations_save` takes `value: str = Form("")` and hands it
unchanged to `set_text` (`translations.py:160`), which writes it. `_resolve`
(`translations.py:114`) returns a cached database value without the `.strip()`
guard the repository-file branch has, so a blank row stops the whole fallback
chain. The live database holds exactly one: `('hy', 'booking.ask_desired_time')`
saved empty on 2026-08-30. An Armenian client on the free-text booking path is
asked for their preferred time with a blank prompt. `test_seed.py::
test_untranslated_keys_are_not_seeded` encodes the rule and is the one test
failing today.

Fix: a blank value in `set_text` deletes the row (the therapist's intent is
"go back to the default"); `_resolve` skips blank database values the way it
skips blank repository values. Fix the live row.

## 5. Findings — process

### B1. The test suite shares the deployment database — HIGH

Recorded in `DESIGN.md` §20.3's last entry and in `CLAUDE.md`. The
consequences are now visible from three directions:

- Today's one failure (A2) is a live row, not a regression. The six
  `test_booking.py` schedule failures `CLAUDE.md` describes did **not**
  reproduce, because the live rows they depended on have moved. A suite whose
  red set changes with the calendar cannot be trusted either way.
- The end-to-end tests commit requests and outbox rows into a database whose
  worker is live. `conftest.py` forces `TELEGRAM_ADMIN_IDS=1,2`, so an admin
  notification a test creates is a row the real worker will try to deliver to
  chat id 1. The tests clean up, but a test that fails before its cleanup
  leaves the row (the README admits this), and a worker pass can run between
  creation and cleanup.
- Reviewers, this one included, cannot run the documented `pytest` command.

Fix: a `test_db` service in `docker-compose.dev.yml` (a second `postgres:16`
with its own volume) and a `DATABASE_URL` override for `exec web pytest`. CI
already does this with a fresh Postgres service, so the pattern exists.

## 6. Findings — code

Ordered by severity. Each names the file and line, what goes wrong, and what
to do.

### C1. One raising message strands the rest of its batch in `sending` — HIGH

`outbox.py:175-181`: the claim commits up to 50 rows as `sending`, then loops
one transaction per id with no `try/except`. `deliver_one` catches only
`KeyError` from `render`; a `ValueError` from `str.format` on an edited
translation, a database error inside `get_text`, or a transport that raises
instead of returning (the email transport catches only `SMTPException` and
`OSError`) propagates out of the loop. `run_once` records the job failure, but
every remaining claimed row is already committed as `sending`, which nothing
retries by design. Up to 49 unrelated confirmations become `outbox_stuck`
until someone edits the table. Not covered by `test_outbox.py` or
`test_outbox_dispatch.py`.

Fix: wrap each iteration; record an unexpected exception as a transient
outcome on that row and continue.

### C2. Per-IP rate limits trust a header the caller writes on the deployed profile — HIGH

`docker-compose.yml:42` runs uvicorn with `--forwarded-allow-ips='*'`.
Verified against the installed uvicorn 0.52.4:
`_TrustedHosts.get_trusted_client_address` returns the **leftmost**
`X-Forwarded-For` entry when every proxy is trusted. Cloudflare appends the
real address to a client-supplied header rather than replacing it, so under
the `cloudflared` profile (the one running here) `ratelimit.client_ip()`
returns whatever the attacker wrote. The admin login limit (5 per 15 minutes)
and the magic-link per-IP limit never trigger; password guessing is bounded
only by Argon2 cost. The Caddy `tls` profile is safe, since Caddy overwrites
untrusted forwarded headers. `DESIGN.md` §20.4 already says `client_ip()`
"needs auditing first"; this is that audit.

Fix: on the `cloudflared` profile read `CF-Connecting-IP` instead, or set
`--forwarded-allow-ips` to the tunnel container's address and take the
rightmost hop. Add a test that sends a forged `X-Forwarded-For` through the
limiter.

### C3. The config upload is buffered to disk before the size cap — MEDIUM

`admin.py:1522` reads `file.read(MAX_CONFIG_UPLOAD + 1)` from an
`UploadFile`, but Starlette has already consumed the whole body into a
`SpooledTemporaryFile` (spilling to disk above 1 MiB) before the route runs.
§17 says "5 MB cap enforced before parsing, never written to disk"; neither
holds. Requires an admin session, so the exposure is an authenticated
therapist or a stolen cookie posting something enormous. `test_hardening.py:393`
greps the source for the pattern rather than exercising it, and the 6 MB post
in `test_maintenance.py:225` passes while spooling to disk.

Fix: reject on `Content-Length` in a dependency before `request.form()` is
touched, and pass a body-size limit to the multipart parser.

### C4. A `view_request` token is spent before the ownership check — MEDIUM

`client.py:969` consumes the token, then `:992` finds the UUID in the URL is
not this client's and returns 404 without starting a session. The transaction
commits, so a mangled link spends the single-use token and the correct link
now lands on the sign-in form with a three-an-hour allowance. Reorder: load the
request and compare `client_id` before `consume_token`.

### C5. A Telegram edit or redelivery is applied to the next step — MEDIUM

`webhook.py:77` treats `edited_message` as a fresh message, and nothing
deduplicates `update_id`. A client sends their problem text, the flow moves to
`entering_name`, the client fixes a typo in the original, and the edit is
stored as their name. The same happens when Telegram redelivers a slow first
attempt. Callback steps are idempotent through `InvalidTransition`; typed
steps are not. Fix: ignore `edited_message`, and keep the last `update_id` per
chat in `flow_state.data`.

### C6. Admin email action links point at the client page — MEDIUM

`messages.py:293-301`: for email, an action with no URL in the payload falls
back to `_request_url`, which is `/r/{uuid}`, the client route. The admin
submission email's Approve, Propose and Reject links therefore open the
client's view with no token, and she lands on the sign-in form. Only the client
case is tested (`test_messages.py:241`). Admin actions should resolve to
`/admin/requests/{uuid}`.

### C7. Reminder emails carry no `view_token` — MEDIUM

`enqueue` mints a token for every client email about a request
(`notifications.py:524-534`); `enqueue_raw`, which `fire_reminders` uses for
its dedupe key, does not. A reminder email's link lands on the sign-in form,
which is the dead end the token exists to prevent.

### C8. Client free text reaches Telegram HTML unescaped in two places — MEDIUM

`router.py:499` formats `desired_time_text` into the "my appointments" line
and `router.py:969` formats a typed email address into `booking.contact.
email_sent`; neither goes through `escape_telegram`, while every admin-screen
line does. `_EMAIL_SHAPE` permits `<`. A client who types "<3 next week" as a
preferred time breaks "My appointments" for themselves until that request
leaves the active list; the bot logs a parse warning and shows nothing.

### C9. The admin request screen can exceed 4096 characters — MEDIUM

`router.py:2163-2223` concatenates `problem_text`, `contact_note` and three
thread bodies, each allowed 4096 by §17, after escaping (which expands
`&<>`). Nothing splits it. `_edit` fails, the fallback send fails, and the
therapist gets a toast and no screen. `_send_topic` already shows the answer:
parts via the markdown packer.

### C10. Unvalidated `tz` and `modality` from the browser produce 500s — MEDIUM

`client.py:535` lets the `tz` query parameter bypass `_timezone()` and reach
`ZoneInfo()`; `client.py:632,647` stores a form `tz` into the reservation and
`submit_slot_request` does not validate `client_timezone`, so a scripted POST
makes `/r/{uuid}` 500 forever for that request. `client.py:782` constructs
`Modality()` outside the `try`. Each is an `error_event` row an attacker can
produce at will, and one of them a client can inflict on their own request.

### C11. The purge and erasure never touch `outbox_message.payload` — MEDIUM

`sweeps.py:249-256` nulls three columns on the request; `erase_client` never
references `OutboxMessage`. Sent admin notifications keep `payload.problem`
(`request.submitted.admin`, `waitlist.joined.admin`) and `payload.note`
indefinitely, and `WaitlistEntry.problem_text` is not in `purge_content` at
all. §14 lists only the three columns, so this is a spec gap; it defeats
`DESIGN.md` §16's intent all the same. (Raw tokens in the same payloads are
already recorded in §20.1.)

### C12. Callbacks are not answered when `handle()` raises — LOW

`webhook.py:54-64` returns 200 before `_send`, so `answer_callback_query`
never runs and the button spins until Telegram times out. Reachable with a
crafted `slot:x` (`int()` raises) or `tz:<bad>` (`ValueError` from
`set_client_timezone`, not a `DomainError`).

### C13. The admin `/translations` and `/content/blocks` routes accept any `lang` — LOW

`admin.py:958` and `:785`; neither route nor core checks the language against
`{ru, hy, en}`. A posted `lang=am` writes a row: hard rule 5, reachable from a
form. The import path validates this (§16.7); the editor path does not.

### C14. `updated_at` columns never update — LOW

Every model has `server_default=func.now()` with no `onupdate` and no trigger;
only `Translation` has `onupdate`, because §15 needed it. Four call sites set
it by hand; the terminal transitions in `booking.py` do not. `purge_content`
filters on `BookingRequest.updated_at < cutoff`, so retention is measured from
submission rather than from the terminal transition. Days of difference over a
twelve-month window, but a column called `updated_at` that does not update
will mislead the next reader.

### C15. Smaller items

- `SLOT_WINDOW = timedelta(days=30)` is defined in both channels
  (`router.py:62`, `client.py:79`). A booking rule in two adapters; belongs in
  `policies.py`.
- The counter picker is gated on `fallback_to_negotiation` alone in both
  channels (`router.py:1333`, `client.py:1387`) while `resolve_booking_mode`
  requires `negotiation_enabled` too; with negotiation off and fallback on,
  Telegram offers a counter that `client_counter` accepts and `admin_propose`
  refuses. Same rule, three readings; belongs in `policies.py`.
- "Counter naming a slot that has since gone" is a raw query in three places
  (`client.py:1322`, `router.py:1478, 1642`); slot grouping by local day is
  written five times across the two channels.
- The Telegram slot picker has no ceiling (`keyboards.py:404-476`); known,
  `DESIGN.md` §20.4's open entry. Hourly slots five days a week exceed 100
  buttons inside three weeks.
- A new aiogram `Bot` is built on every `dispatch_outbox` pass and never closed
  (`transports.py:41`, `transport.py:62`); each pass that sends leaks an
  aiohttp session.
- A missing translation key is never cached, so each lookup re-runs a full
  language load (`translations.py:137-152`); with `hy`'s gaps one Armenian
  message can cost several table reads.
- An over-long paragraph containing inline tags is split mid-tag
  (`markdown.py:198-236` rebalances only `<pre>`); only therapist content
  blocks are exposed.
- `system.*.admin` intents have no email subject key; admin alert emails
  arrive with an empty Subject.
- A non-numeric Telegram chat id is treated as transient and retried six
  times (`transport.py:85`); a multi-part message that fails on part 2 re-sends
  part 1 on retry.
- Admin auth is a hand-repeated three-line check in roughly forty routes
  rather than a dependency; `test_every_admin_page_requires_a_session` is a
  hard-coded list missing `/admin/clients/{id}` and
  `/admin/content/revisions/*`.
- Web forms never call `looks_like_email`; any string becomes a client row.
- The client's request thread renders proposal times in UTC with no label
  (`client.py:1408`) while the heading above uses the client's zone.
- "Check your email" is shown even when `email_enabled` is false or the
  allowance is exhausted (`client.py:832-839`).
- Dead code: `_client_by_uuid` (`router.py:2344`), the unreachable
  `ADMIN_ACTIONS` branch in `_callback` (`router.py:1139`), `help_languages`,
  `to_email_html`/`to_email` (used only by tests).
- Flash text and `rejected: {exc}` are reflected from the query string
  (`admin.py:263, 993`); autoescaped, so cosmetic.
- No `Content-Security-Policy`, `X-Frame-Options` or `Strict-Transport-Security`
  header anywhere in the app or `Caddyfile`.
- The Dockerfile keeps `build-essential` in the runtime image; a two-stage
  build would drop it.

## 7. Spec versus implementation

Places where a document and the code disagree. None is a silent choice;
`CLAUDE.md` asks for them to be reported.

- **§17's upload clause** ("enforced before parsing, never written to disk")
  is not true of the implementation (C3).
- **§17's per-IP limits** are not enforceable on the `cloudflared` profile as
  deployed (C2).
- **§13.3** names `reminder.client` and `request.confirmed.client` as the
  both-channel intents; `BOTH_CHANNELS` (`notifications.py:71`) adds
  `request.rescheduled.client`. Sensible, undocumented.
- **§11.4's "minimal HTML alternative"** for email is implemented
  (`to_email_html`) and never sent; the transport is plain text only.
- **§12.1 `POST /book/hold`** says "hold a slot"; nothing is held, and the
  page says so. Already in `DESIGN.md` §20.1.
- **§14's purge** names three columns; the outbox payload and the waitlist
  entry are outside it (C11).
- **§19 M9** says "§17 verified item by item"; the cookie flags, the upload
  cap, and the per-IP magic-link limit have no behavioural test.
- **`slots.py`'s docstring table** omits `held → extend` and `booked → block`,
  both of which it implements. Prose only.
- **`README.md`** says not to point the e2e tests at a live database;
  `CLAUDE.md` describes doing so. The README is right.

## 8. Recommendations, in order

1. **Fix the SMTP credentials and the `535` classification** (A1). A real
   client is missing reminders today.
2. **Give the tests their own database** (B1). Everything else about the
   suite's trustworthiness follows from it.
3. **Harden the worker loop** (C1) and **stop trusting the forwarded header**
   on the tunnel profile (C2). Both are small and both fail under ordinary
   conditions.
4. **Fix the three email-link defects together** (A2's blank row, C6, C7):
   they share `messages.py` and `notifications.py` and one afternoon.
5. **Reorder the token check** (C4), **ignore edited messages and dedupe
   `update_id`** (C5), **escape the two Telegram lines** (C8), **split the
   admin screen** (C9), **validate `tz` and `modality`** (C10). Each is a
   handful of lines plus a test.
6. **Move the upload cap in front of the parser** (C3) and **add the purge
   of outbox payloads and waitlist text** (C11, after deciding whether §14
   should say so).
7. **Pull the duplicated rules into `policies.py`** (C15's first three
   bullets) and **cap the Telegram picker**.
8. **Have the Armenian copy reviewed.** `hy.yaml` carries 129 `# TODO: review`
   markers (drafted, not written by a native speaker) against 74 in `ru.yaml`.
   The spec says the final wording is the therapist's; it has not happened yet
   for the language many of her clients may use.

## 9. Numbers

| | |
|---|---|
| Application code | 17,376 lines across 56 modules |
| Test code | 16,793 lines, 771 test functions, 44 modules |
| Largest modules | `telegram/router.py` 2,348; `web/admin.py` 1,958; `booking.py` 1,429; `web/client.py` 1,427 |
| Migrations | 6, one head, `alembic check` clean |
| Locale keys | 550 rows seeded; en 300 lines, hy 214, ru 208 |
| Live data | 10 clients, 32 requests, 228 outbox rows (2 dead), health `fail` since 2026-09-26 |
| Commits | 133 between 2026-08-23 and 2026-09-02 |
