# NepSocial Backend

Multi-tenant customer-messaging backend for Nepali businesses (Instagram AI customer chat).

FastAPI · SQLAlchemy 2.0 (async) · asyncpg · Alembic · PostgreSQL 16 · Pydantic v2 · JWT · Fernet

## What works today

| Area | Status |
|------|--------|
| Data model (16 tables) + Alembic migrations | done |
| Tenant auth: JWT, RBAC, tenant isolation, audit log | done |
| Facebook Login for Business (Page token) | done (secondary flow) |
| Instagram Login (primary flow) + token refresh | done |
| Webhook ingest (signature-verified, idempotent, per-event isolation) | done |
| Inbox: list threads, read messages, reply as an agent | done |
| **Historical conversation import / resync** | done |
| LLM answer layer (`/api/v1/ai/reply`) | **not built yet** |

## Meta auth model

Two flows are supported and they are **not interchangeable**. Which one applies to an
account is stored in `social_accounts.auth_provider` and mapped to a host + path root in
one place (`app/services/meta/target.py`).

| provider | consent screen | Graph host | path root | token lifetime |
|---|---|---|---|---|
| `instagram_login` **(primary)** | instagram.com | `graph.instagram.com` | Instagram account id | ~60 days, refreshed |
| `facebook_login` (secondary) | facebook.com | `graph.facebook.com` | Page id | no expiry |

**Instagram Login** is the default. It is Instagram-branded, needs no Facebook Page, and is
what the dashboard's "Continue with Instagram" button uses.

**Facebook Login for Business** is kept for Messenger/WhatsApp later, and for Instagram
accounts reachable only through a Page.

> **The credential trap.** These are two different app identities with two different id and
> secret pairs. The Instagram pair lives under *Instagram → API setup with Instagram login*;
> the Facebook pair under *App Settings → Basic*. Mixing them is the single most confusing
> failure here: an Instagram app id sent to Facebook's dialog returns *"Invalid app ID: The
> provided app ID does not look like a valid app ID"*, and used as an app token it returns
> code 190 *"Error validating application"*. The app logs both ids at startup so this is
> visible immediately.

### Instagram Login setup

- App product: **Instagram → API setup with Instagram login**.
- Register the redirect URI (`INSTAGRAM_REDIRECT_URI`) under that product's
  **Business login settings** — it must match exactly, including scheme and trailing slash.
- On the Instagram account: *Settings → Messages and story replies → Message controls →
  Connected Tools →* **Allow Access to Messages**. Without it, messaging silently fails.
- Webhook: subscribe the **`instagram`** object to
  `https://<host>/api/v1/webhooks/instagram`, verify token = `META_WEBHOOK_VERIFY_TOKEN`.
  Instagram DM payloads arrive as `{"object": "instagram"}` with `entry[].id` = the
  Instagram professional account id.
- **The app must be published** (regardless of App Review status) to receive webhooks.
- Under Standard Access the Conversations API returns only threads belonging to users with
  a role on the app; **Advanced Access (App Review) is required for real customers.**

### Token refresh — Instagram Login only

This is the maintenance the Facebook flow did not need. An Instagram long-lived token lasts
roughly 60 days and then stops working, taking the inbox with it. Meta accepts a refresh
only once the token is at least 24 hours old, and refuses it after expiry — at which point
the account must be reconnected through the browser.

Run this daily from cron:

```bash
docker exec nepsocial-api python3 -m scripts.refresh_tokens --within-days 10
```

`--status` reports every account's provider, expiry and resolved route without changing
anything. The command exits non-zero if any refresh failed, so cron surfaces the problem.

## Historical import — and its hard limit

Webhooks only deliver messages sent *after* an account is subscribed, so a newly connected
account would otherwise show an empty inbox. `app/services/conversation_sync.py` crawls
`GET /{path-root}/conversations?platform=instagram` and persists the threads.

> **Meta truncates a conversation to its 20 most recent messages.** This is documented
> behaviour on both the Conversation and Message nodes, so **a complete DM archive cannot
> be reconstructed through this API.** The import restores threads, participants and recent
> context; depth accumulates from webhooks from the moment the account is connected. Run
> the import early and let webhooks build the rest.

Other properties worth knowing:

- **Idempotent.** Safe to re-run. Two independent keys prevent duplicates: Meta's message
  id (normalised, see below) and a natural key of timestamp + sender + body.
- **Message ids are normalised.** The Conversations API historically prefixes a Graph
  message id with `m_` while webhooks do not (`m_mid.x` vs `mid.x`). Both sides are
  collapsed by `app/services/meta/ids.py` so an imported message and its later webhook echo
  share one row.
- **Failures are isolated.** Each thread is imported inside its own savepoint, so one bad
  payload costs one thread, not the run.
- **Timestamps are preserved.** `messages.created_at` is set from Meta's `created_time`, so
  imported history sorts correctly instead of bunching up at import time.
- **Rate-limit aware.** Paginated crawls are paced (`GRAPH_MIN_INTERVAL_SECONDS`) because
  the Conversations API allows only ~2 calls/second per account and Meta treats a burst as
  abuse (error 613, subcode 1996).

### Running an import

```bash
python -m scripts.sync_conversations --list
python -m scripts.sync_conversations --account <uuid-or-ig-id>
python -m scripts.sync_conversations --all
python -m scripts.sync_conversations --account <id> --max-conversations 100 --fetch-profiles
python -m scripts.sync_conversations --account <id> --subscribe-webhooks
python -m scripts.refresh_tokens --status
```

Or over HTTP (runs in the background, returns a pollable run):

```
POST /api/v1/social-accounts/{account_id}/sync        -> 202 + SyncRun
GET  /api/v1/social-accounts/{account_id}/sync-runs    -> recent runs
```

`SyncRun.status` is `succeeded` / `partial` / `failed`. A `partial` result means some
threads failed or the ceiling was hit — re-running is the intended response.

## Setup

```bash
python -m venv .venv
. .venv/Scripts/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env
#   Fill JWT_SECRET, TOKEN_ENCRYPTION_KEY, META_APP_ID/SECRET/REDIRECT_URI,
#   META_WEBHOOK_VERIFY_TOKEN.
python -c "import secrets; print('JWT_SECRET=', secrets.token_urlsafe(64))"
python -c "from cryptography.fernet import Fernet; print('TOKEN_ENCRYPTION_KEY=', Fernet.generate_key().decode())"

docker compose up -d            # Postgres 16 on localhost:5435
alembic upgrade head
python -m scripts.seed          # optional: a tenant + owner for local testing

uvicorn app.main:app --reload   # http://localhost:8000/docs
```

Tests need the same Postgres and run inside a transaction that is always rolled back:

```bash
python -m pytest tests/ -q
```

## API

| Method | Path | Auth |
|--------|------|------|
| POST   | /api/v1/auth/login | public (form) |
| POST   | /api/v1/auth/refresh | refresh token |
| GET    | /api/v1/tenants/me | any active user |
| PATCH  | /api/v1/tenants/me | owner / admin |
| GET    | /api/v1/users | owner / admin |
| POST   | /api/v1/users | owner / admin |
| PATCH  | /api/v1/users/{id} | owner / admin |
| GET    | /api/v1/social-accounts | any active user |
| GET    | /api/v1/social-accounts/instagram/login | public (redirect) — **primary** |
| GET    | /api/v1/social-accounts/instagram/callback | public (redirect) |
| GET    | /api/v1/social-accounts/facebook/login | public (redirect) — secondary |
| GET    | /api/v1/social-accounts/facebook/callback | public (redirect) |
| POST   | /api/v1/social-accounts/{id}/sync | any active user |
| GET    | /api/v1/social-accounts/{id}/sync-runs | any active user |
| GET    | /api/v1/conversations | any active user |
| GET    | /api/v1/conversations/{id}/messages | any active user |
| POST   | /api/v1/conversations/{id}/messages | any active user |
| GET    | /api/v1/webhooks/instagram | public (Meta handshake) |
| POST   | /api/v1/webhooks/instagram | public (HMAC-verified) |
| GET    | /health | public |

`GET /api/v1/conversations` returns every thread status by default so imported history is
visible; pass `status=open` for just the live queue. Both list endpoints are bounded by
`limit`/`offset`.

Reply failures are translated rather than passed through raw: a closed 24-hour messaging
window (Meta error `1545041`) returns **409** with a human-readable reason, a revoked token
returns 409 telling you to reconnect, and throttling returns 429.

## Webhook handling

`POST /api/v1/webhooks/instagram` verifies `X-Hub-Signature-256` (HMAC-SHA256 over the raw
body) before parsing anything.

- **One savepoint per event.** A single malformed event no longer rolls back the whole
  delivery — previously that lost every message in the batch while still answering `200`,
  so Meta never redelivered.
- **A total failure returns 503** so Meta retries. A *partial* failure returns `200`
  deliberately, so a permanently poisonous event cannot trap the delivery in a retry loop
  that ends with Meta disabling the webhook. Duplicate deliveries are harmless.
- `is_deleted` events and non-message events (read, delivery, reaction, postback) are ignored.
- Both `messaging` and `standby` arrays are ingested. `standby` carries messages handled
  while another app owns the thread.
- No Graph calls happen on the request path: profile lookups are deferred to a background
  task (`app/services/customer_profiles.py`) so a third-party API cannot stall the response.

## Tenant isolation

Every tenant-owned query filters on `tenant_id` resolved from the JWT (`app/core/tenant.py`).
No route loads a tenant-owned row by `id` alone.

## Migrations

```bash
alembic revision --autogenerate -m "message"   # after model changes
alembic upgrade head
alembic downgrade -1
```
The DB URL comes from `app/core/config` at runtime (see `app/db/migrations/env.py`), not
`alembic.ini`.
