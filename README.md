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
