# Deployment (Coolify)

The API runs on Coolify as a Dockerfile application, with a Coolify-managed
Postgres and Redis in the same project. GitHub Actions deploys it: when a PR
merges into `master` and every check passes, the `deploy` job calls Coolify's
API, then waits until `/health` reports the new commit.

## One-time setup

### 1. Databases

In a Coolify project (e.g. `clinic`, environment `production`), add:

- **PostgreSQL 17**: name `clinic-db`, database `clinic`.
- **Redis 7**: name `clinic-redis`.

Leave both unpublished (not exposed to the internet). Start them, then copy
each one's **internal** URL (Postgres `postgres://…`, Redis `redis://…`) for the
app's environment below. `postgres://` URLs are fine: the app rewrites them to
psycopg 3.

### 2. Application

Add a resource > **Private Repository (GitHub App)** (or public), this repo,
branch `master`.

| Setting | Value |
| --- | --- |
| Build pack | Dockerfile |
| Dockerfile location | `/Dockerfile` |
| Docker build stage target | `runner` |
| Ports exposes | `8000` |
| Domains | the API's URL, e.g. `https://api.clinic.example.com` |
| Auto deploy (Advanced) | **off**. The pipeline deploys, after CI passes |
| Include Source Commit in Build (Advanced) | **on**. `/health` reports the commit, which the pipeline waits for |
| Health check | on, path `/health`, port `8000` |

The health check is what makes a deploy safe: Coolify starts the new container
alongside the old one and switches traffic only once it is healthy. A container
that fails its migration exits, so the previous version keeps serving.

### 3. Environment variables

Set these on the application. Mark secrets as such, and leave "Build Variable"
off for all of them (they are runtime only):

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | clinic-db's internal URL |
| `REDIS_URL` | clinic-redis's internal URL |
| `MIGRATE_ON_START` | `true` (runs `alembic upgrade head` before uvicorn starts) |
| `JWT_SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `CORS_ORIGINS` | the web client's URL(s), comma-separated |
| `FRONTEND_URL` | the web client's URL, used in invite emails |
| `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_FROM` | SMTP settings |
| `GOOGLE_CLIENT_ID` | optional; unset disables Google sign-in |

Deploy once by hand from Coolify to check it comes up, then open
`https://<domain>/health`.

### 4. GitHub

1. In Coolify, enable the API (**Settings > Advanced > API Access**), then
   create a token under **Keys & Tokens > API tokens** with the `deploy`
   permission.
2. In the GitHub repo, **Settings > Environments > production**, add:

   | Kind | Name | Value |
   | --- | --- | --- |
   | Secret | `COOLIFY_TOKEN` | the token |
   | Variable | `COOLIFY_URL` | your Coolify instance, e.g. `https://coolify.example.com` |
   | Variable | `COOLIFY_APP_UUID` | the app's UUID, from its URL in Coolify |
   | Variable | `PRODUCTION_URL` | the API's domain from step 2 |

   Optionally, add yourself as a required reviewer on the environment to
   approve each deploy.

## How a deploy runs

1. A PR merges into `master`. Lint, tests, migration checks, the image build
   and the dependency audit all pass.
2. The `deploy` job calls `GET /api/v1/deploy?uuid=…` on Coolify, which builds
   the head of `master`.
3. The new container runs migrations, starts, and passes its health check.
   Coolify then moves traffic to it and stops the old one.
4. The job polls `/health` until `commit` is this commit (or a newer one that
   includes it), and fails after 25 minutes otherwise.

Coolify can't deploy a specific commit, only the branch head. So if master
moves on before a deploy job starts, that job skips its trigger and leaves the
deploy to the newer commit's run.

## Rolling back

In Coolify, open the application's **Rollback** tab and redeploy the last good
image. Migrations don't roll back on their own: to undo one, run
`alembic downgrade -1` from the app's **Terminal** before redeploying the older
version.
