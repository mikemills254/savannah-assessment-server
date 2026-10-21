# Clinic Booking API

A REST API for booking 30-minute appointments with a clinic's doctors, built
with FastAPI, SQLAlchemy and PostgreSQL for the Savannah Informatics backend
assessment.

- **Live API:** `TODO: https://<your-domain>` (interactive docs at `/docs`,
  health at `/health`)
- **Web client:** [savannah-client](https://github.com/mikemills254/savannah-client) (optional; the API stands on its own)
- **Original design document:** [docs/Architecture_Readme.md](docs/Architecture_Readme.md)
- **Deployment runbook:** [docs/Deployment.md](docs/Deployment.md)

Every API route is under `/api/v1`, so the brief's `POST /appointments` is
`POST /api/v1/appointments`. `/` and `/health` are outside the prefix.

---

## 1. System design

I wrote [docs/Architecture_Readme.md](docs/Architecture_Readme.md) before
writing code. This section summarises it and notes where the build went
further than that first design, and why.

### The problem in one sentence

Show each doctor's free 30-minute slots, let a patient take one, and make it
impossible for two people to end up holding the same slot, even when they ask
at the same moment.

### Components

A **modular monolith**: one deployable FastAPI app with clear layers inside.

```text
HTTP ─> routes/        thin: parse input, check auth, call a service, shape the response
          │
          ▼
        services/      the rules: availability, booking, cancel, reschedule, events
          │
          ▼
        models/ ─> PostgreSQL   the source of truth; constraints enforce the invariants
                                 
        Redis           rate limits, and pub/sub for live updates (never booking state)
```

| Folder | Holds |
| --- | --- |
| `app/routes/` | One module per resource (`appointments`, `doctors`, `facilities`, `patients`, `auth`, `events`) |
| `app/services/` | Business rules, testable without HTTP. Raise `AppError`s that `main.py` maps to JSON errors |
| `app/models/` | SQLAlchemy models |
| `app/schemas/` | Pydantic request/response models (no database access) |
| `app/core/` | Config, security (JWT, password hashing), permissions, logging, rate limiting, Redis |
| `alembic/` | Versioned migrations; the only way the schema changes |
| `tests/` | Unit and integration tests against a real Postgres |

### Data model

| Table | What it is | Key constraints |
| --- | --- | --- |
| `facilities` | A clinic, with its IANA timezone | |
| `users` | Everyone, with exactly one role: `PATIENT`, `DOCTOR`, `FACILITY_ADMIN` or `SUPER_ADMIN` | Unique email (stored lowercased). Facility admins must have a `facility_id`; others must not (check constraint) |
| `doctor_facilities` | A doctor working at a clinic; a doctor can work at several | Primary key (doctor, clinic). Deactivated rather than deleted, so history stays |
| `doctor_working_hours` | Recurring weekly periods at one clinic, e.g. Monday 09:00–12:00, in that clinic's local time | `end_time > start_time`; (doctor, clinic) must be a `doctor_facilities` row |
| `appointments` | A booking and its lifecycle (`BOOKED` → `CANCELLED`), with who booked it, who cancelled it and why | See below |
| `idempotency_keys` | Makes a booking safe to retry | Unique per user and key |

Appointments are never deleted: cancelling is a state change, so history and
the reason survive.

### How double booking is prevented

This is the requirement that matters most, so the database enforces it rather
than trusting a "check, then insert" in Python (two requests can both pass the
check before either inserts):

```sql
-- one active booking per doctor per slot
CREATE UNIQUE INDEX uq_active_doctor_slot  ON appointments (doctor_id, starts_at)               WHERE status = 'BOOKED';
-- a patient can't hold two bookings at the same time at one clinic
CREATE UNIQUE INDEX uq_active_patient_slot ON appointments (facility_id, patient_id, starts_at) WHERE status = 'BOOKED';
```

The service still validates first (working hours, half-hour grid, not in the
past, at least an hour ahead) so the common errors are clear. The index is the
final word: when it rejects a concurrent duplicate, the service rolls back and
returns **409** naming the conflict. Because the indexes only cover `BOOKED`
rows, cancelling frees the slot immediately. `tests/test_concurrency.py` fires
simultaneous bookings at the same slot and asserts exactly one succeeds.

### Availability is computed, not stored

Slots aren't rows. For a doctor and a date, the service takes their working
periods for that weekday, steps through them in 30 minutes of clinic-local
wall-clock time, and removes slots that are booked or less than an hour away.
It runs one query for working hours and one for that day's bookings.
Generating slots in local time, and storing instants as `TIMESTAMPTZ`, keeps
it correct across daylight-saving changes (tested with London's clock change
and UTC+14 Kiritimati).

### Key decisions and trade-offs

| Decision | Why | Trade-off |
| --- | --- | --- |
| Modular monolith | Simple to run, test and deploy at five doctors | Scales as one unit until a part needs splitting out |
| PostgreSQL partial unique indexes | Correct under concurrency without locks or queues | Assumes fixed, aligned 30-minute slots. Variable lengths would need an exclusion constraint on a time range |
| Slots computed on request | No table of future slots to generate and keep in sync | A little computation per request (measured: query count doesn't grow with doctors or bookings) |
| Facilities (multi-clinic) from the start | "We want to grow". A clinic id on doctors and appointments is cheap now and painful to retrofit | More concepts than a single clinic strictly needs |
| One role per account, doctors linked to clinics | Simple permission checks, while a doctor keeps one account and one diary across clinics | A facility admin runs one clinic; someone who is both doctor and admin needs two accounts |
| Idempotency keys on booking | A retried request after a dropped response must not book twice | One extra row per booking |
| Redis for rate limits and live events | Rate limits must be shared across instances; live updates must reach users on any instance | One more service to run. If Redis is down, booking still works: limits fail open and the live update is skipped |
| JWT bearer tokens, no refresh tokens | Stateless, enough for the scope | A stolen token is valid until it expires (60 minutes) |

### Beyond the brief

Built on the same core rules, mostly because a real clinic needs them:

- **Roles.**
  - Clinic admins manage their doctors, working hours and all of the clinic's appointments, and can book on a patient's behalf (phone or walk-in). Booking for someone new creates an account they claim via an invite.
  - Doctors manage their own hours and appointments.
  - Platform admins create clinics.
- **Live updates.** `GET /api/v1/events` is a Server-Sent Events stream, published through Redis pub/sub after each change commits. When a patient books, the doctor and the clinic's admins see it within about 100 ms (measured end to end); patients see changes the clinic makes.
- **Doctors at several clinics.** One account, separate hours at each clinic. Appointments and hours reference `doctor_facilities`, so a doctor can only be booked or scheduled where they work; the one-booking-per-slot index spans clinics, so they're never booked in two places at once; and hours that overlap another clinic are refused, compared as real instants across time zones. `POST /appointments` is unchanged: the clinic is the one whose hours the slot falls in, or an optional `facility_id`. Removing a doctor from one clinic leaves their others.
- **Accounts by email.** New staff, and patients a clinic books in, are emailed a one-use invite (patients see their visit in it). `POST /auth/forgot-password` emails a 30-minute reset link and answers the same whether or not the account exists; each link works once because it carries a digest of the password hash it was issued against. Emails go out after the response, so a slow mail server never delays a request.
- **Operations.**
  - Structured JSON logs with a request id on every line, returned as `X-Request-ID`.
  - Rate limiting, with tighter limits on login and sign-up.
  - `/health` checks the database and Redis and reports the deployed commit.

---

## 2. API

Full, interactive documentation is generated at `/docs` (Swagger) and
`/redoc`. The brief's endpoints:

| Brief | Endpoint | Behaviour |
| --- | --- | --- |
| Book a slot | `POST /api/v1/appointments` `{doctor_id, starts_at}` | **201** with the appointment. **400** off the half-hour grid, outside working hours, in the past or under an hour away. **404** unknown or inactive doctor. **409** slot taken, or the patient is already booked then. **422** malformed body (e.g. no UTC offset). Optional `Idempotency-Key` header makes retries safe |
| Availability | `GET /api/v1/doctors/{id}/availability?date=YYYY-MM-DD` | Free slots on that clinic-local date, in clinic time. **404** unknown doctor, **422** missing or invalid date |
| Cancel | `PATCH /api/v1/appointments/{id}/cancel` `{reason}` | **200** with the cancelled appointment; the slot is bookable again. **409** already cancelled or already started. **422** missing or blank reason. **404** not yours |
| Reschedule | `PATCH /api/v1/appointments/{id}/reschedule` `{starts_at}` | **200**; the old slot is freed in the same transaction. The new slot is validated exactly as a booking. **409** if cancelled, started or the new slot is taken |
| Bonus: upcoming | `GET /api/v1/patients/{id}/appointments` | Booked appointments that haven't ended, soonest first. The patient sees all of theirs; a clinic admin those at their clinic; a doctor those with them. **404** for anyone else |
| Bonus: 1-hour rule | (booking and reschedule) | Slots less than an hour away are refused (**400**) and are not listed as available |

Errors always look like `{"detail": "..."}`, with the status codes above.
Validation errors (422) use FastAPI's standard list of field errors.

### Trying it

1. `POST /api/v1/auth/register` with `facility_name` and `timezone` creates a
   clinic and its admin. Each sign-up returns an `access_token`; send it as
   `Authorization: Bearer <token>`.
2. As that admin:
   - `POST /api/v1/facilities/{id}/members` adds a doctor. The response includes an invite token; the doctor accepts it at `/auth/accept-invite`.
   - `PUT /api/v1/facilities/{id}/doctors/{doctor_id}/working-hours` sets the doctor's hours.
3. `POST /api/v1/auth/register` without a facility creates a patient, who can
   then check availability and book.

---

## 3. Running locally

You need Docker, and Python 3.13 for running the tests outside Docker.

```bash
cp .env.example .env          # fill in the passwords and JWT_SECRET_KEY
pip install -r requirements-dev.txt
task docker:dev               # Postgres, Redis, migrations, then the API with reload
```

The API is at <http://localhost:8000> (docs at `/docs`). `docker:dev` also
starts pgAdmin and Redis Commander; `task --list` shows every task.

Without Docker for the API itself (Postgres and Redis still need to be
running):

```bash
alembic upgrade head
task dev                      # uvicorn with reload
```

### Tests

```bash
pytest                        # 202 tests
pytest --cov                  # with coverage
```

The tests need Postgres, not SQLite, because the partial unique indexes and
the concurrency tests only mean anything there. They create and migrate their
own database (the `DATABASE_URL` database name plus `_test`), and use Redis
database 15.

| Area | Where |
| --- | --- |
| Slot arithmetic, DST, the 1-hour rule | `test_availability_unit.py`, `test_facility_availability.py` |
| Booking rules, idempotency | `test_booking.py` |
| Cancel and reschedule rules | `test_cancel_reschedule.py` |
| Concurrent booking of one slot | `test_concurrency.py` |
| HTTP status codes and bodies | `test_api.py`, `test_auth.py` |
| Invites, password resets and email templates | `test_emails.py` |
| Doctors at several clinics, across time zones | `test_multi_clinic.py` |
| Live events, logging, rate limits, config | `test_events.py`, `test_logging.py`, `test_rate_limit.py`, `test_config.py` |

---

## 4. CI/CD

GitHub Actions ([.github/workflows/ci-cd.yml](.github/workflows/ci-cd.yml)),
deploying to [Coolify](https://coolify.io) on my own server.

- **Every pull request** runs five jobs in parallel:
  - **Lint:** `ruff check` and `ruff format --check`.
  - **Tests:** the full suite against Postgres 17 and Redis 7 service containers, with coverage. Coverage must stay above a floor.
  - **Migrations:** one Alembic head, upgrade from empty, the models match the migrations, then downgrade to base and upgrade again.
  - **Docker image:** the production image builds.
  - **Audit:** `pip-audit` on the dependencies.
- **Merging into `master`** runs the same checks, then the **deploy** job:
  1. It calls Coolify's deploy API.
  2. Coolify builds the image and starts the new container, which applies migrations on start.
  3. Traffic switches over only once `/health` passes.
  4. The job then polls `/health` until it reports this commit, so a failed deploy fails the pipeline.
- Only `master` deploys. Other branches and PRs only run the checks.

Setup, required secrets and rollback are in
[docs/Deployment.md](docs/Deployment.md).

---

## 5. Decisions on ambiguous requirements

- **One clinic or many?** Many (each with its own timezone), with the brief's
  single clinic as the simplest case. See the trade-off table.
- **Who is the patient when booking?** The signed-in user. The API never
  accepts a `patient_id` from the client for self-booking, so no one can book
  as someone else.
- **Cancellation reason** is required (1–500 characters) and stored with who
  cancelled. Cancelling an appointment that has already started is refused.
- **Repeated cancellation** returns **409** and changes nothing.
- **Rescheduling** keeps the same doctor (moving to another doctor is a new
  booking). Moving to the time it already has is a no-op 200, so retries are
  safe.
- **The 1-hour rule** applies to everyone, including clinic staff booking a
  walk-in, so availability and booking always agree. Exactly one hour ahead is
  allowed.
- **"Upcoming"** includes an appointment in progress (not yet ended).
- **Times** are accepted with any UTC offset, stored as UTC instants, and
  returned in the clinic's local time with its offset.
- **API prefix:** routes are under `/api/v1` so a breaking change can ship as
  `/api/v2` alongside it.

---

## 6. AI reflection

> TODO (Mike): write this section in your own words. The questions from the
> brief are below.

1. What did you use AI for across the four sections?
2. Give one example where an AI suggestion improved your work. What did you
   prompt it with?
3. Give one example where AI output was wrong or incomplete, and how you
   caught it.
4. Name two decisions you made without AI. Why did you trust your own
   judgment there?
