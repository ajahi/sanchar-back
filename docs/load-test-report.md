# Webhook load test report

Date: 2026-10-05. Branch: `tst/auto-reply`.

## Setup

- Real app (uvicorn, port 8006) with the real `/api/v1/webhooks/instagram` handler and the real auto-reply pipeline.
- Outbound calls pointed at `loadtest/mock_external.py` (fake Graph and fake Groq) through `INSTAGRAM_GRAPH_BASE` and `GROQ_URL`.
  - LLM latency 2s, Graph latency 0.3s, each jittered 0.5x to 1.5x.
  - Groq rate limits were OFF for this run. The mock can enforce them with `LLM_RPM=30 LLM_TPM=8000`, but that was not exercised.
- k6 `loadtest/webhook_burst.js`: open-model ramp (warm-up, spike, 2 min sustained, wind-down), 20 tenants, 60% of traffic to one hot shop, 25% of customers send a follow-up, 5% duplicate deliveries.
- Throwaway Postgres database `loadtest` (dropped afterwards), default SQLAlchemy pool (5 + 10 overflow, 30s wait).
- Everything ran on one Windows machine with Docker Desktop, so absolute numbers are pessimistic. The failure pattern is the finding, not the exact figures.

## Results

| Peak msgs/s | Webhook p95 | Webhook p99 | Messages sent / stored | AI replies stored | Pool timeouts |
|------------ |---   ------ |--   -----  -|-- ----------          -|-                --|-            --|
| 10          | 33.5s       | 34.0s       | 1727 / 715             | 296               | 1,500         |
| 50          | 33.3s       | 34.5s       | 5305 / 859             | 189               | 5,392         |

- Targets: ack p95 < 500ms, p99 < 1.5s. Both failed by a wide margin at 10 msgs/s already.
- Reply latency (inbound stored to AI reply stored): p50 29.7s, p95 33.3s, p99 33.6s at 10 msgs/s.
- 100 and 200 msgs/s were not run: 50 msgs/s was already fully broken.
- k6 also dropped 166 iterations at 10 msgs/s and 3,457 at 50 msgs/s (it could not start new requests).
- The webhook returned 200 for most messages that failed to store, so Meta would not redeliver them. These are silent losses.

## Cause

The `p95` of about 33s is the pool's 30s wait plus the work itself. The pool (5 + 10) is exhausted because connections are held across slow external calls:

1. **Webhook handler** (`app/api/v1/webhooks.py`): holds a DB connection while it fetches the customer profile from Graph (`_get_or_create_customer`). Every new customer triggers that fetch.
2. **`auto_reply`** (`app/services/reply_pipeline.py`): the session opened in `run_auto_reply` stays open through the whole LLM call (about 2s). At 10 msgs/s that needs around 20 connections.

## Other findings

- A 429 from Groq raises `HTTPStatusError`. `_generate` retries once immediately (useless against a per-minute limit), then the reply is dropped with only a log line and the customer gets nothing.
- Groq free-tier limits for `openai/gpt-oss-120b`: 30 requests/min, 1K requests/day, 8K tokens/min, 200K tokens/day. The whole knowledge base is sent on every reply, so the token cap (8K/min) probably binds before the request cap.
- The webhook handler logs full headers and body at INFO on every hit. This adds cost under load and writes customer data to logs.
- Harness issues: `loadtest.seed_tenants --cleanup` fails on a foreign key from `messages`. The dev database on port 5435 was missing the `shop_media` table (migration not applied there).

## Suggested fixes (not applied)

1. In `auto_reply`, read what is needed, commit or close the session before the LLM call, and open a fresh session to store the reply.
2. Move the Graph profile fetch out of the webhook request (background task, or commit before fetching). The ack must never wait on Graph.
3. Raise the pool (for example `pool_size=20, max_overflow=20`), cap concurrent LLM calls with a semaphore, and back off on 429 instead of dropping the reply.
4. Drop the per-request body logging, or lower it to DEBUG.
5. Fix `seed_tenants --cleanup` so the harness can be re-run on one database.

## Re-run

```
python -m loadtest.mock_external                 # add LLM_RPM=30 LLM_TPM=8000 for the limits test
python -m loadtest.seed_tenants 20
# app with INSTAGRAM_APP_SECRET=loadtest-secret INSTAGRAM_GRAPH_BASE=http://localhost:9000
#          GROQ_URL=http://localhost:9000/chat/completions DATABASE_URL=<throwaway>
k6 run -e BASE_URL=http://localhost:8006 -e PEAK=10 loadtest/webhook_burst.js   # then 50, 100, 200
python -m loadtest.verify
```
