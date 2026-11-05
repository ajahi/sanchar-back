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

## Update 2026-10-23: fixes applied, then the reply queue

Most of the "Suggested fixes" above landed in earlier commits (connections released before slow calls, profile fetch moved after the ack, LLM and profile calls capped, pool 15 + 5, body logging removed). Then replies moved to RabbitMQ (`app/services/reply_queue.py`, `app/worker.py`).

Setup: `bash loadtest/run_linux.sh` on one Linux box that also ran k6, the app, Postgres and the mock (LLM 2s, Graph 0.3s, keep-alive, no Groq rate limit). Peak 50 msgs/s, 20 tenants, one uvicorn process. "Slots" = `REPLY_CONCURRENCY`, the model calls in flight per process.

| Replies | Stored / sent | AI replies | Reply latency p50 / p95 | Worker CPU per reply |
|---|---|---|---|---|
| In process, 20 slots (before the queue) | 9798 / 9798 | 2406 | 74s / 167s | n/a |
| Queue, 2 workers x 20 slots | 9813 / 9814 | 4638 | 78s / 162s | not measured |
| Queue, 6 workers x 20 slots | 9756 / 9756 | 8252 | 5.4s / 6.8s | not measured |
| Queue, 1 worker x 130 slots (HTTP limit still 100) | 9725 / 9725 | 8022 | 16.3s / 28.8s | 17.8 ms |
| same, after the CPU fixes below | 9818 / 9818 | 8048 | 16.3s / 28.7s | 15.1 ms |
| same, HTTP limit raised with the slots | 9758 / 9758 | 8524 | 4.6s / 5.9s | 13.8 ms |

- Webhook p95 stayed 35 to 80 ms in every run (target < 500 ms). The webhook is no longer the problem.
- Fewer replies than messages is by design: only the newest message of a quick burst in a chat is answered.
- **What capped one worker:** the shared HTTP session allowed 100 connections in total, so 130 slots could never run at once (about 36 replies/s against about 38/s arriving). The limit now follows `REPLY_CONCURRENCY`. Six workers avoided it only because each had its own session.
- **CPU per reply** went from about 20 ms to about 13 ms in the per-reply benchmark: SQL statements 11 to 5 (stale checks and the human-active flag folded into the load query), shop knowledge cached for 30s, aiohttp no longer re-scanning the environment for proxy settings on every request (about 1 ms per request, also on the API's profile lookups), and no pool pre-ping on the worker (about 2 ms). One process tops out near 70 replies/s, so add worker processes only after raising slots.
- **Durability:** in the 2-worker run the script stopped the workers while 4051 jobs were still queued, and they stayed in `replies`. With in-process replies those would have been lost.
- Not resolved: in two runs 1 and 3 of about 9,700 messages were not stored although the webhook had answered (one failed 200 check each). No pool timeouts or errors were logged, and the cause was not found. The other runs lost none.
- Not re-run with the queue: 200 msgs/s. The in-process run earlier the same day lost about half the messages (14% of webhook calls failed) at that rate, so the ingest ceiling is a separate question from reply speed.
- Everything here is against mocks on one machine. Real Groq latency, its rate limits and real Meta responses are untested.

## Re-run

```
bash loadtest/run_linux.sh 10 50                 # everything below in one go; LT_QUEUE=N adds RabbitMQ and N reply workers

# or by hand:
python -m loadtest.mock_external                 # add LLM_RPM=30 LLM_TPM=8000 for the limits test
python -m loadtest.seed_tenants 20
# app with INSTAGRAM_APP_SECRET=loadtest-secret INSTAGRAM_GRAPH_BASE=http://localhost:9000
#          GROQ_URL=http://localhost:9000/chat/completions DATABASE_URL=<throwaway>
k6 run -e BASE_URL=http://localhost:8006 -e PEAK=10 loadtest/webhook_burst.js   # then 50, 100, 200
python -m loadtest.verify
```
