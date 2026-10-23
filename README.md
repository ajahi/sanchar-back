# NepSocial Backend

Multi-tenant customer-messaging backend for Nepali businesses (Instagram AI customer chat).
This repo currently implements **V1 Milestones 1 & 2**: the data model (10 core tables,
Alembic migrations) and tenant authentication (JWT + role checks + tenant isolation).

Meta webhooks, message sending, and the AI/handover layer (M3–M7) are intentionally not
built yet — see the plan in the project notes.

## Stack

FastAPI · SQLAlchemy 2.0 (async) · asyncpg · Alembic · PostgreSQL 16 · Pydantic v2 · JWT · Fernet

## Setup

```bash
# 1. Python env
python -m venv .venv
. .venv/Scripts/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Config
cp .env.example .env
#   Fill JWT_SECRET and TOKEN_ENCRYPTION_KEY:
python -c "import secrets; print('JWT_SECRET=', secrets.token_urlsafe(64))"
python -c "from cryptography.fernet import Fernet; print('TOKEN_ENCRYPTION_KEY=', Fernet.generate_key().decode())"

# 3. Database
docker compose up -d            # Postgres 16 on localhost:5432
alembic upgrade head            # create all tables

# 4. Seed a tenant + owner (optional, for testing)
python -m scripts.seed

# 5. Run
uvicorn app.main:app --reload   # http://localhost:8000/docs
```

## Auto-replies and the reply queue

A customer message is stored by the webhook, which answers Meta right away. The AI reply is a separate job
(`reply_pipeline.auto_reply`) that takes only the message id and reloads everything from the DB.

- **Without `RABBITMQ_URL`** the job runs in the API process as a background task. A restart loses in-flight replies.
- **With `RABBITMQ_URL`** the webhook publishes the id to a durable RabbitMQ queue (`replies`), and a separate
  worker sends the reply. If the broker is down the webhook falls back to the in-process task, so nothing is lost.

```bash
docker compose up -d postgres rabbitmq     # broker on localhost:5672, management UI on :15672
#   .env: RABBITMQ_URL=amqp://nepsocial:nepsocial@localhost:5672/
python -m app.worker                       # or the compose `worker` service
```

How the worker behaves:
- Handles `REPLY_CONCURRENCY` jobs at once (default 20). **Raise this before adding worker processes**: a call
  waiting on the LLM costs no CPU. One process measured about 14 ms of CPU per reply, so it tops out near 70 replies/s.
- Drops a job that is older than `REPLY_MAX_AGE_MINUTES` (10), already answered, or superseded by a newer
  message in the same chat. A late or doubled DM is worse than none.
- A failed job goes to `replies.retry` (waits `REPLY_RETRY_SECONDS`), and after 3 tries to `replies.dead` for a person
  to look at. An LLM failure is retried; a failed Meta send is not (it may have gone out).
- Delivery is at-least-once: a crash between sending and storing the reply can still send one duplicate.

Shop knowledge is cached per process for `KNOWLEDGE_CACHE_SECONDS` (30), so an edit or sold-out toggle can take
that long to reach the bot. All settings are in `.env.example`.

### Load test

`loadtest/run_linux.sh` starts a throwaway Postgres, the app, a mock Meta/Groq and k6. Needs docker, k6 and the venv.

```bash
bash loadtest/run_linux.sh 10 50                    # in-process replies at 10 and 50 msgs/s
LT_QUEUE=1 REPLY_CONCURRENCY=130 bash loadtest/run_linux.sh 50   # replies via RabbitMQ, one worker
```
It prints stored messages vs sent, reply latency, DB connection use and the worker's CPU per reply.
`RABBITMQ_URL=amqp://... LT_QUEUE=N` uses a broker you already run. Results land in `loadtest/out/<rate>/`.

## API (V1 so far)

| Method | Path                          | Auth            |
|--------|-------------------------------|-----------------|
| POST   | /api/v1/auth/login            | public (form)   |
| POST   | /api/v1/auth/refresh          | refresh token   |
| GET    | /api/v1/tenants/me            | any active user |
| PATCH  | /api/v1/tenants/me            | owner / admin   |
| GET    | /api/v1/tenant-users          | owner / admin   |
| POST   | /api/v1/tenant-users          | owner / admin   |
| PATCH  | /api/v1/tenant-users/{id}     | owner / admin   |
| GET    | /health                       | public          |

Login uses OAuth2 password form: `username` = email, `password` = password.

## Tenant isolation

Every tenant-owned query filters on `tenant_id` resolved from the JWT (`app/core/tenant.py`).
No route loads a tenant-owned row by `id` alone.

## Migrations

```bash
alembic revision --autogenerate -m "message"   # after model changes
alembic upgrade head
alembic downgrade -1
```
The DB URL comes from `app/core/config` at runtime (see `app/db/migrations/env.py`), not `alembic.ini`.
