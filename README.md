# NepSocial Backend

Multi-tenant customer-messaging backend for Nepali businesses (Instagram AI customer chat).

FastAPI · SQLAlchemy 2.0 (async) · asyncpg · Alembic · PostgreSQL 16 · Pydantic v2 · JWT · Fernet

## What works today

| Area | Status |
|------|--------|
| Data model (16 tables) + Alembic migrations | done |
| Tenant auth: JWT, RBAC, tenant isolation, audit log | done |
| Facebook Login for Business (Page token) + linked Instagram account | done |
| Webhook ingest (signature-verified, idempotent, per-event isolation) | done |
| Inbox: list threads, read messages, reply as an agent | done |
| **Historical conversation import / resync** | done |
| LLM answer layer (`/api/v1/ai/reply`) | **not built yet** |

## Meta auth model

This backend uses **Facebook Login for Business**, not "Instagram API with Instagram Login".
The reason is that a **Page** access token is what drives both Messenger and the Instagram
conversations/messages edges, so one consent screen covers Instagram now and Messenger
later. Instagram-Login tokens are scoped to `graph.instagram.com` and cannot be used
against `graph.facebook.com`.

Flow (`app/services/meta/facebook_login.py`):

1. `GET /v21.0/dialog/oauth` — consent, requesting the scopes in `META_SCOPES`.
2. `code` → short-lived user token.
3. short-lived → **long-lived** user token (~60 days).
4. `GET /me/accounts` → each Page, its Page token, and its `instagram_business_account`.

**Step 3 must run before step 4.** A Page token minted from a *short-lived* user token
expires in about an hour; minted from a long-lived user token it has no expiry, which is
why `social_accounts.token_expires_at` stays `NULL` for accounts connected this way.

One login connects **every** Page the user manages that has a linked Instagram account. A
Page already linked to a different tenant is skipped with a warning rather than failing
the whole login.

### Meta setup checklist

- App products: **Facebook Login for Business**, **Messenger** (with Instagram settings),
  and **Instagram → API setup with Facebook login**.
- Valid OAuth redirect URI must match `META_REDIRECT_URI` exactly.
- Webhook: subscribe the **`instagram`** object to `https://<host>/api/v1/webhooks/instagram`,
  verify token = `META_WEBHOOK_VERIFY_TOKEN`. Instagram DM payloads arrive as
  `{"object": "instagram"}` with `entry[].id` = the Instagram professional account id.
- **The app must be published** (regardless of App Review status) to receive webhooks.
- Under Standard Access the Conversations API only returns threads for users with a role on
  the app; **Advanced Access (App Review) is required for real customers.**
- On the Instagram account itself: *Settings → Messages and story replies → Message
  controls → Connected Tools →* **Allow Access to Messages**. Without this, messaging
  silently does not work.

## Historical import — and its hard limit

Webhooks only deliver messages sent *after* a Page is subscribed, so a newly connected
account would otherwise show an empty inbox. `app/services/conversation_sync.py` crawls
`GET /{page-id}/conversations?platform=instagram` and persists the threads.

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
| GET    | /api/v1/social-accounts/facebook/login | public (redirect) |
| GET    | /api/v1/social-accounts/facebook/callback | public (redirect) |
| GET    | /api/v1/social-accounts/instagram/login | **deprecated alias** |
| GET    | /api/v1/social-accounts/instagram/callback | **deprecated alias** |
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
