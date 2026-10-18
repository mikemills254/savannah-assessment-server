# Clinic Appointment Booking System

## 1. Overview

This project is the foundation of a clinic appointment booking platform. Patients can view a doctor's availability for a given date, book a 30-minute appointment, and cancel an existing booking. The system must prevent two patients from booking the same doctor's time slot, including when booking requests arrive concurrently.

The initial clinic has five doctors. The architecture is intentionally small and practical: a **modular monolith** built with FastAPI, SQLAlchemy, and PostgreSQL. It keeps deployment and operations simple while separating responsibilities so the application can evolve as requirements grow.

## 2. Goals and scope

### Initial functional requirements

- List active doctors.
- Maintain each doctor's recurring working hours.
- Show available 30-minute slots for a doctor on a specific date.
- Allow an authenticated patient to book an available slot.
- Prevent overlapping or duplicate active bookings for the same doctor.
- Allow patients to view their own appointments and cancel them.
- Make a cancelled slot available for another patient.

### Non-functional requirements

- **Correctness:** concurrent requests must not double-book a slot.
- **Data integrity:** invalid appointments must be rejected by application validation and database constraints where possible.
- **Security:** patients can access and cancel only their own appointments; privileged staff access is explicitly authorized.
- **Maintainability:** business rules should be testable independently of HTTP routes.
- **Reliability:** booking and cancellation state changes must be transactional.
- **Scalability:** the system should be able to add doctors and serve more traffic without an immediate architectural rewrite.
- **Observability:** important operations and failures should be diagnosable through structured logs and metrics.

### Assumptions

- Each appointment lasts 30 minutes and starts on a 30-minute boundary.
- A booking is for one patient and one doctor.
- Doctors have recurring weekly working hours.
- The clinic has one configured timezone initially.
- Patients must authenticate to book, view their own appointments, or cancel them.
- Payment, medical records, prescriptions, and telemedicine are out of scope for the initial version.
- Cancellation is allowed for a booked appointment unless a later business rule introduces a cutoff.
- The server determines the patient from the authenticated identity; clients cannot choose another patient's ID when booking.

These assumptions should be revisited if the clinic's actual policies differ.

## 3. Architecture at a glance

```text
Patient / Staff Web Client
          |
          v
      FastAPI API
          |
          v
   Routes / Controllers
          |
          v
        Services
    (business rules)
          |
          v
 SQLAlchemy ORM / Queries
          |
          v
      PostgreSQL
```

For the initial release, the API, business logic, and data-access code are deployed as one application. PostgreSQL is the source of truth for appointments and schedule data.

Background workers and a message queue are **not required for the first booking flow**. They can be introduced when asynchronous reminders and other background tasks are added.

### Component responsibilities

- **Routes / controllers:** define HTTP endpoints, parse path/query/body inputs, apply authentication and authorization dependencies, call services, and return HTTP responses. Keep route handlers thin.
- **Pydantic schemas:** validate requests and shape responses. They are not database models and should not contain database queries.
- **Services:** implement use cases and business rules, including availability calculation, booking, cancellation, and rescheduling if added.
- **SQLAlchemy models:** represent persistent entities and relationships.
- **Database session/dependencies:** provide a scoped SQLAlchemy session per request and ensure it is closed correctly.
- **PostgreSQL:** enforce relational integrity and prevent conflicting active bookings.
- **Authentication / authorization:** identify the caller and enforce ownership and staff permissions.
- **Configuration:** load environment-specific settings from environment variables, with secrets excluded from source control.
- **Logging and metrics:** capture request IDs, booking outcomes, database conflicts, and operational errors without logging sensitive patient data.

A separate repository class for every model is not necessary at this scale. Add repositories when they create a useful abstraction, support complex queries, or improve testing—not simply to increase the number of files.

## 4. Suggested project structure

```text
app/
├── main.py
├── core/
│   ├── config.py
│   ├── security.py
│   └── permissions.py
├── db/
│   ├── session.py
│   └── base.py
├── models/
│   ├── patient.py
│   ├── doctor.py
│   ├── doctor_working_hour.py
│   └── appointment.py
├── schemas/
│   ├── patient.py
│   ├── doctor.py
│   ├── availability.py
│   └── appointment.py
├── services/
│   ├── availability.py
│   └── appointments.py
├── routes/
│   ├── doctors.py
│   └── appointments.py
└── dependencies/
    └── auth.py

alembic/
tests/
README.md
```

This is a suggested organization, not a requirement to create every file before it is needed. For a small submission, closely related route and controller logic can live together.

## 5. Data model

### Patient

Stores a patient's account.

| Field | Type / constraint | Purpose |
|---|---|---|
| `id` | Primary key | Stable internal identifier |
| `name` | Required string | Patient's display name |
| `email` | Required, unique | Login and communication address |
| `hashed_password` | Nullable only if external authentication is supported | Password hash; never store plaintext passwords |
| `is_active` | Boolean, default true | Allows an account to be disabled |
| `created_at` | Timestamp | Audit information |

Email normalization and uniqueness must be handled consistently. If email-based login is used, normalize it before checking and storing it.

### Doctor

Represents a doctor who can accept appointments.

| Field | Type / constraint | Purpose |
|---|---|---|
| `id` | Primary key | Stable identifier |
| `name` | Required string | Display name |
| `is_active` | Boolean, default true | Prevents new bookings for inactive doctors |
| `created_at` | Timestamp | Audit information |

### DoctorWorkingHour

Defines recurring weekly availability.

| Field | Type / constraint | Purpose |
|---|---|---|
| `id` | Primary key | Stable identifier |
| `doctor_id` | Foreign key to `Doctor.id` | Owner of the schedule rule |
| `weekday` | Integer or enum | Day of week |
| `start_time` | Local time | Inclusive start of working period |
| `end_time` | Local time | Exclusive end of working period |

A doctor can have multiple working-hour records per weekday, such as 09:00–12:00 and 13:00–17:00. Validate that `end_time` is after `start_time`. If overnight shifts are ever required, model them explicitly rather than relying on ambiguous time comparisons.

### Appointment

Represents a booking and its lifecycle.

| Field | Type / constraint | Purpose |
|---|---|---|
| `id` | Primary key | Stable appointment identifier |
| `patient_id` | Foreign key to `Patient.id` | Patient who booked |
| `doctor_id` | Foreign key to `Doctor.id` | Doctor being booked |
| `starts_at` | `TIMESTAMPTZ`, required | Appointment start instant |
| `ends_at` | `TIMESTAMPTZ`, required | Appointment end instant |
| `status` | Enum/string, required | Initially `BOOKED` or `CANCELLED` |
| `created_at` | Timestamp | Booking creation time |
| `cancelled_at` | Nullable timestamp | When cancellation occurred |

For the initial scope, the service sets `ends_at` to 30 minutes after `starts_at`; it should not trust a client-supplied end time. Store appointment instants in PostgreSQL using `TIMESTAMPTZ`. Interpret recurring working hours in the clinic's configured local timezone, then convert requested appointment times to a consistent instant for storage and comparison.

Cancelled appointments are retained for history and auditability. A future system may add statuses such as `COMPLETED`, `NO_SHOW`, or `CANCELLED_BY_CLINIC`, but the initial status set should remain small.

### Relationships

- One patient can have many appointments.
- One doctor can have many appointments.
- One doctor can have many working-hour records.
- Each appointment belongs to exactly one patient and one doctor.

Use foreign keys and appropriate deletion policies. Avoid cascading deletion of appointment history when a patient or doctor record is deactivated.

## 6. API design

All patient-specific endpoints require authentication.

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/doctors` | List active doctors |
| `GET` | `/doctors/{doctor_id}` | Retrieve doctor details |
| `GET` | `/doctors/{doctor_id}/availability?date=YYYY-MM-DD` | Return available slots for a local clinic date |
| `POST` | `/appointments` | Book a slot for the authenticated patient |
| `GET` | `/appointments/me` | List the authenticated patient's appointments |
| `GET` | `/appointments/{appointment_id}` | Retrieve an appointment the caller is allowed to access |
| `POST` | `/appointments/{appointment_id}/cancel` | Cancel an appointment the caller is allowed to cancel |

### Example booking request

```json
{
  "doctor_id": 3,
  "starts_at": "2026-10-20T09:00:00+03:00"
}
```

The authenticated patient is derived from the security context, not from a `patient_id` supplied by the client. The server calculates the end time, checks schedule rules, and attempts to create the booking.

### Example availability response

```json
{
  "doctor_id": 3,
  "date": "2026-10-20",
  "available_slots": [
    {
      "starts_at": "2026-10-20T09:00:00+03:00",
      "ends_at": "2026-10-20T09:30:00+03:00"
    },
    {
      "starts_at": "2026-10-20T09:30:00+03:00",
      "ends_at": "2026-10-20T10:00:00+03:00"
    }
  ]
}
```

The values are illustrative. The response should only include slots that satisfy working-hour rules and have no conflicting active appointment at the time of the query.

### Expected error behavior

- `400 Bad Request`: malformed input or invalid time boundary.
- `401 Unauthorized`: missing or invalid authentication.
- `403 Forbidden`: authenticated user lacks the required permission.
- `404 Not Found`: doctor or appointment does not exist, or the API deliberately hides a resource the caller cannot access.
- `409 Conflict`: slot was taken by another request, or the appointment is already cancelled.
- `422 Unprocessable Entity`: request fails schema validation, such as an invalid date or missing field.

Use a consistent error response format across endpoints.

## 7. Availability calculation

Availability is calculated dynamically rather than storing every possible slot as a separate database row.

For a requested doctor and local date:

1. Validate the date and verify that the doctor exists and is active.
2. Determine the weekday and load that doctor's working-hour records.
3. Interpret those records in the clinic timezone.
4. Generate candidate slots in 30-minute increments within each working period.
5. Exclude slots that are in the past, if the clinic does not allow past bookings.
6. Query active appointments that overlap the relevant time range.
7. Remove any candidate slot that conflicts with an active appointment.
8. Return the remaining slots.

For a working period of 09:00–12:00, the candidates are 09:00–09:30, 09:30–10:00, 10:00–10:30, 10:30–11:00, 11:00–11:30, and 11:30–12:00.

**Availability is a snapshot, not a reservation.** Another patient may book a slot after it has been displayed. The booking endpoint must independently validate the requested slot, and the database must be the final authority on concurrent booking conflicts.

The first implementation can query appointments for the requested day and filter in the service. Keep the query bounded to the doctor's requested date. Add query optimization or caching only if measurements show that availability reads are expensive.

## 8. Preventing double booking

This is the most important correctness requirement.

A naive implementation checks whether a slot is free and then inserts an appointment. Two concurrent requests can both pass the check before either insert commits. An application-level check alone is therefore insufficient.

### Database-enforced uniqueness for fixed slots

Because the initial system has fixed 30-minute appointments aligned to half-hour boundaries, PostgreSQL can enforce one active booking per doctor and start time with a partial unique index:

```sql
CREATE UNIQUE INDEX uq_active_doctor_slot
ON appointments (doctor_id, starts_at)
WHERE status = 'BOOKED';
```

This assumes appointment timestamps are normalized consistently and all appointment starts are aligned to the clinic's 30-minute slot boundaries. The service must validate both assumptions.

The booking workflow is:

1. Authenticate the patient.
2. Validate the doctor, requested date/time, working hours, and slot alignment.
3. Calculate the end time on the server.
4. Attempt to insert the appointment within a database transaction.
5. Commit the transaction.
6. Return the confirmed appointment.
7. If the unique index rejects a concurrent duplicate, roll back and return `409 Conflict`.

Do not perform unrelated work inside the transaction. Catch database integrity errors at the appropriate boundary, roll back as needed, and distinguish an expected slot conflict from other integrity failures.

### If appointment durations or start times become flexible

A unique `(doctor_id, starts_at)` index does not prevent all possible overlaps if appointment durations vary or starts are not aligned to the fixed grid. In that case, consider a PostgreSQL exclusion constraint using a timestamp range and a condition for active bookings, or another explicit locking/reservation strategy.

The invariant is that two active appointments for the same doctor must never overlap. The database design should enforce the invariant, not merely assume the service always checks correctly.

## 9. Booking and cancellation workflows

### Booking

- Validate the request schema.
- Authenticate the patient and derive their ID.
- Validate that the doctor is active.
- Validate the requested slot against working hours and schedule exceptions, if supported.
- Calculate the end timestamp.
- Insert and commit the appointment.
- Translate a confirmed slot conflict into `409 Conflict`.
- Return the persisted appointment.

### Cancellation

- Fetch the appointment.
- Verify the caller owns it or has a permitted staff role.
- Verify its status is `BOOKED`.
- Set its status to `CANCELLED` and record `cancelled_at`.
- Commit the update.
- Return the updated appointment.

Cancellation should be implemented as a state transition rather than deleting the record. A repeated cancellation should have a deliberate, documented response and must not corrupt appointment history.

### Rescheduling

Rescheduling is not required for the initial scope, but it is a likely extension. It must secure the new slot before releasing the old one, or otherwise use a transaction/workflow that guarantees a failed reschedule leaves the original appointment intact. The new slot must be protected by the same concurrency controls as a normal booking.

## 10. Security and privacy

- Hash passwords with a suitable password-hashing algorithm; never store plaintext passwords.
- Use established authentication mechanisms and protect all patient-specific endpoints.
- Enforce object-level authorization: patients can only view and cancel their own appointments.
- Keep staff and administrator privileges separate from ordinary patient permissions.
- Validate and normalize user input.
- Store secrets in environment variables or a secrets manager, never in the repository.
- Use HTTPS in production.
- Avoid putting sensitive health details in URLs, logs, or error messages.
- Collect only the personal information needed for the booking workflow.
- Define appropriate retention, access, backup, and audit policies before storing medical records or other sensitive clinical data.
- Apply least-privilege database credentials and restrict production database access.

The initial product is an appointment scheduler, not an electronic medical record system. Medical history, diagnoses, prescriptions, and clinical notes require additional privacy and access-control design.

## 11. Transactions, sessions, and migrations

- Use a SQLAlchemy session scoped to a request or use case and close it reliably.
- Keep transaction boundaries explicit in booking and cancellation services.
- Never share one SQLAlchemy session across unrelated concurrent requests.
- Use Alembic to version and apply schema changes.
- Test migrations against PostgreSQL, including partial indexes and any PostgreSQL-specific constraints.
- Ensure integrity errors cause appropriate rollback before the session is reused.
- Use a production connection pool configured for the database's capacity.

SQLite can be convenient for isolated experiments, but concurrency and PostgreSQL-specific constraint behavior should be tested against PostgreSQL for this assignment.

## 12. Indexing and performance

Start with indexes that support the actual access patterns:

- Unique index on normalized patient email.
- Index on `DoctorWorkingHour(doctor_id, weekday)`.
- Partial unique index on active appointment `(doctor_id, starts_at)` for the fixed-slot design.
- Index on `Appointment(patient_id, starts_at)` for patient appointment history.
- Index on `Appointment(doctor_id, starts_at)` if it is not already adequately supported by the unique index for the queries being run.

Check query plans before adding more indexes. Indexes improve some reads but add storage and write overhead. Use pagination for appointment history if it can grow large.

## 13. Testing strategy

Testing should cover behavior and failure cases, not just successful HTTP responses.

### Unit tests

- Generate 30-minute slots correctly.
- Respect multiple working periods and boundaries.
- Exclude occupied slots.
- Reject times outside working hours and invalid slot boundaries.
- Apply cancellation state transitions correctly.

### Integration tests with PostgreSQL

- Create and retrieve appointments.
- Cancel an appointment and confirm the slot becomes available.
- Reject duplicate bookings for the same doctor and start time.
- Confirm a patient cannot cancel another patient's appointment.
- Confirm inactive doctors cannot accept new appointments.
- Verify that database constraints and migrations work as expected.

### Concurrency test

Send two booking requests concurrently for the same doctor and slot using different patients. Assert that exactly one booking succeeds and the other receives a conflict response. Run this test against PostgreSQL, not only a mocked repository or SQLite.

### Other cases

- Daylight-saving/timezone boundaries if applicable to the deployment timezone.
- Doctor schedule changes and leave.
- Past appointment requests.
- Repeated cancellation requests.
- Database failures and transaction rollback.

## 14. Observability and operations

For the initial release:

- Emit structured logs with a request/correlation ID.
- Record booking success, cancellation, conflict, and error counts.
- Track API latency and database errors.
- Avoid logging passwords, tokens, or sensitive patient details.
- Provide health checks for application readiness and liveness.
- Back up PostgreSQL and document a restore procedure.
- Monitor error rates, connection pool saturation, and slow queries.

Useful domain metrics include appointment booking success rate, booking conflict rate, cancellations, no-show rate (once attendance is tracked), and doctor schedule utilization.

## 15. Future evolution

The initial design should leave room for growth without implementing speculative infrastructure prematurely.

### Phase 1: Reliable scheduling

- Doctor working hours and availability.
- Booking, cancellation, authentication, and ownership checks.
- Database-enforced conflict prevention.
- Tests, migrations, logging, and backups.

### Phase 2: Clinic operations

- Schedule exceptions for leave, holidays, and closures.
- Rescheduling.
- Appointment reminders by SMS, email, or WhatsApp.
- Receptionist/staff workflows and walk-in bookings.
- Waiting lists for cancelled slots.
- Appointment status tracking, including completed and no-show appointments.

Notifications should be processed asynchronously so provider delays do not block booking. When reliable delivery is required, use retry policies and idempotent job handling. A transactional outbox can prevent losing an event when a database commit succeeds but publishing to a queue fails.

### Phase 3: Multiple services and clinics

- Different appointment durations based on service type.
- Multiple clinic locations and doctor assignments.
- Per-clinic timezone, schedules, and cancellation policies.
- Strict tenant-level data isolation and authorization.
- Billing and payment integration where required.
- Integration with electronic health record systems, subject to appropriate privacy and regulatory controls.

### Phase 4: Scale based on evidence

- Run multiple stateless API instances behind a load balancer.
- Tune database connections and query plans.
- Add caching for suitable, frequently read data while carefully handling invalidation.
- Scale background workers independently.
- Add read replicas only if the read workload and consistency requirements justify them.
- Extract services only when independent deployment, ownership, scaling, or isolation benefits outweigh distributed-systems complexity.

## 16. Key decisions and trade-offs

| Decision | Reason | Trade-off |
|---|---|---|
| Modular monolith | Easy to develop, test, deploy, and operate at initial scale | Components are deployed together |
| PostgreSQL as source of truth | Strong transactions, constraints, and relational modeling | Requires query and connection management |
| Dynamic slot generation | Avoids storing every potential appointment slot | Availability is calculated on request |
| Partial unique index for fixed slots | Database prevents concurrent duplicate bookings | Assumes fixed, aligned 30-minute slots |
| Keep cancelled records | Preserves booking history and auditability | Records accumulate over time |
| No Redis at the start | Current workload does not justify added complexity | May need optimization if reads become expensive |
| Queue for notifications later | External delivery should not block booking | Adds retries, monitoring, and delivery-state complexity |
| No microservices initially | Avoids network and deployment complexity | Independent scaling is limited until extraction |

## 17. Main risks and mitigations

| Risk | Mitigation |
|---|---|
| Two patients book the same slot | Database-enforced constraint and concurrency tests |
| Availability shown becomes stale | Revalidate at booking time; treat database as authoritative |
| Patient accesses another patient's booking | Server-side ownership checks on every protected operation |
| Doctor is absent after appointments are booked | Add schedule exceptions and a process to notify/reschedule affected patients |
| Notification provider is unavailable | Asynchronous delivery, retries, and failure monitoring |
| Timezone errors create incorrect bookings | Explicit clinic timezone, timezone-aware timestamps, and tests |
| Database outage or data loss | Backups, restore testing, monitoring, and documented recovery |
| Scope expands too early | Roadmap features and introduce infrastructure only when justified |

## 18. Conclusion

The initial design prioritizes correctness, clarity, and operational simplicity. FastAPI provides the HTTP layer, SQLAlchemy provides database interaction, and PostgreSQL enforces the most important data invariants. A modular monolith is sufficient for five doctors and can support meaningful growth.

The central design principle is that availability displayed to a patient is not a guarantee. The booking transaction and database constraints must remain authoritative under concurrent requests. Future capabilities—reminders, waiting lists, schedule exceptions, multiple clinics, and analytics—can be added incrementally as real operational needs emerge.
