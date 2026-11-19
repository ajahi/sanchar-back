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

## Update 2026-11-05: server baseline, no queue

The "before" numbers for the reply queue: the current app on the real server, with replies still running inside the API processes.

Setup:

- Server `vmi3189379`: 4 cores, 7.9 GB RAM. k6, the app, Postgres and the mock all ran on it, beside the live stack, so they competed for the same CPU.
- `WORKERS=4 bash loadtest/run_linux.sh 10 20 30 40 50`, 05:28 to 05:50 UTC. Commit `0b8dffd`, the branch head at that time (the run did not record it). The queue merged at 06:26 UTC, after this run.
- 4 uvicorn processes, each with a 15 + 5 DB pool (80 connections) and 20 LLM slots. Replies were FastAPI background tasks.
- Mock LLM 2s and Graph 0.3s, no Groq rate limit. 20 tenants, 60% of traffic to one shop, Instagram only, default funnel, so the hours check, order saving and kitchen send did not run.
- `webhook_burst.js` and `run_linux.sh` were corrected by hand for this run: the broken default URL, the orphaned `top`, and the pool-timeout search. Each peak ran 3.5 minutes plus a 40s drain.

| Peak msgs/s | Sent / stored | AI replies | Ack p95 / p99 | Reply p50 / p95 / max | Pool timeouts | Dropped by k6 |
|---|---|---|---|---|---|---|
| 10 | 1978 / 1978 | 1978 | 917ms / 2.23s | 2.9s / 5.2s / 8.5s | 0 | 0 |
| 20 | 3907 / 3906 | 3906 | 1.49s / 2.48s | 3.1s / 6.4s / 11.9s | 0 | 0 |
| 30 | 5863 / 5824 | 5824 | 2.71s / 5.03s | 8.7s / 14.5s / 26.1s | 0 | 0 |
| 40 | 7114 / 6692 | 5880 | 30.16s / 30.46s | 21.1s / 52.8s / 82.2s | 1879 | 565 |
| 50 | 8960 / 8226 | 6861 | 30.21s / 30.44s | 24.8s / 58.7s / 89.8s | 3161 | 611 |

The ack target (p95 < 500 ms, p99 < 1.5 s) failed at every peak. Replies at 40 and 50 are a lower bound: the slowest replies took 82 to 90s and the script verified after 40s.

**Where the messages went:**

| Peak | Lost | k6 saw a failure | Silent (200 sent, not stored) |
|---|---|---|---|
| 10 | 0 | 0 | 0 |
| 20 | 1 | 1 | 0 |
| 30 | 39 | 39 | 0 |
| 40 | 422 (5.9%) | 7 | 415 |
| 50 | 734 (8.2%) | 2 | 732 |

- Up to 30 msgs/s every loss was visible: `connection reset by peer` or `EOF`, no 200, so Meta would redeliver. No pool timeouts were logged. The cause is not established; a reused keep-alive connection is one candidate (uvicorn closes idle ones after 5s).
- From 40 msgs/s the DB pool wait reaches its 30s timeout and the handler catches the error, rolls back and still answers 200 (`app/api/v1/webhooks.py`), so Meta never redelivers. Ack p95 sits on 30s at the same peaks. This is the vanishing-message bug.

**CPU and database** (sustained part of each run, seconds 61 to 180):

| Peak | Server CPU idle | Seconds at zero idle | App CPU total | Busiest app process |
|---|---|---|---|---|
| 10 | 29% | 0 of 120 | 127% | 69% |
| 20 | 13% | 3 of 120 | 176% | 76% |
| 30 | 4% | 11 of 120 | 212% | 75% |
| 40 | 4% | 23 of 120 | 210% | 78% |
| 50 | 3% | 27 of 120 | 219% | 80% |

- The server runs out of CPU at about 30 msgs/s. App CPU stops growing there and no process reaches 90%, so the limit is the whole machine, not one event loop. About half of the 400% went to k6, the mock, Postgres and whatever else ran on the box, so the app's own ceiling is probably higher; this run cannot say by how much.
- Replies level off with it: 5824 at peak 30 against 5880 at peak 40.
- Postgres and the disk were not the limit: about 2 active connections on average, I/O wait about 1%, and memory peaked at 5.5 GB of 7.9 GB.
- No connection was held across a slow call: the longest idle transaction was 0.89s. Connections stayed busy because webhooks and reply tasks queued for the same pool on a CPU-starved machine, so a bigger pool would not help.
- `BEGIN` and `;`, the two statements `pool_pre_ping` adds to every checkout, were about 40% of the idle-in-transaction samples in the top 12 at peak 40. That is where the app was waiting, not proof of what the ping costs.

**Against the 7 October run** (same server, 4 workers, 5 + 10 pool per process, older code):

| Peak | Ack p95, Oct -> Nov | Ack p99, Oct -> Nov | Reply p95, Oct -> Nov | Lost, Oct -> Nov | Most connections, Oct -> Nov |
|---|---|---|---|---|---|
| 10 | 130ms -> 917ms | 366ms -> 2.23s | 3.7s -> 5.2s | 0 -> 0 | 24 -> 79 |
| 20 | 364ms -> 1.49s | 883ms -> 2.48s | 4.3s -> 6.4s | 0 -> 1 | 58 -> 80 |
| 30 | 2.03s -> 2.71s | 4.7s -> 5.03s | 11.6s -> 14.5s | 111 -> 39 | 61 -> 81 |

Ack and reply times got worse while losses at 30 fell. The cause is not established. The pool, the reply path and whatever else was running on the server all changed between the runs.

**With the bot off** (same morning, k6 against the live app with `LT_AI=0`, so no reply work; other setup differs, so this is a rough guide):

| Peak | Ack p95 / p99 | Requests failed |
|---|---|---|
| 10 | 112ms / 301ms | 0 of 2060 |
| 20 | 255ms / 613ms | 1 of 4085 |

At 10 msgs/s all 1979 messages were stored. The stored count at 20 was not captured. Compared with the table above, ack was about 6 to 8 times slower with replies running in the same processes, which is the work the queue moves out.

Not measured: real Groq latency and rate limits, real Meta traffic (echoes, delivery statuses), Messenger and WhatsApp, and the bot-off run at 30 to 50 msgs/s, which `aio_pika` being missing from the server's `.venv` stopped.

To compare the queue with this baseline: `DRAIN=900 WORKERS=4 LT_QUEUE=1 bash loadtest/run_linux.sh 10 20 30 40 50`. It should show zero silent losses at 40 and 50, ack p95 under 500 ms, and zero chats with no AI reply once the queues are empty.

## Update 2026-11-09: after the publish-timeout fix

Commit `811ac7e` (publish after the ack, publish timeout follows `REPLY_MAX_AGE_MINUTES`, pool timeout answers 503) plus a one-line import fix in `app/api/v1/webhooks.py`. Setup: `LT_QUEUE=1 REPLY_CONCURRENCY=130 DRAIN=600 bash loadtest/run_linux.sh 50 200` on one machine (`alphaBeast`: AMD Ryzen 7 5800HS, 16 threads, 15 GB RAM, Linux 6.8, a laptop-class CPU) that also ran k6, the app, Postgres, RabbitMQ and the mock (LLM 2s, Graph 0.3s, no Groq rate limit). One uvicorn process, one reply worker with 130 slots. Raw output: `loadtest/out/w1-q1/`.

| Peak msgs/s | Webhook p95 / p99 | Sent / stored | Failed requests (503) | AI replies | Reply latency p50 / p95 | Worker CPU per reply |
|---|---|---|---|---|---|---|
| 50 | 8.7 ms / 11.5 ms | 9752 / 9752 | 0 | 8713 | 4.0s / 5.2s | 14.2 ms |
| 200 | 9.35s / 15.5s | 35911 / 35897 | 16 | 29049 | 175s / 338s | 15.7 ms |

- **50 msgs/s is healthy:** nothing lost, no pool timeouts, no fallback to in-process replies, and the ack targets (p95 < 500 ms, p99 < 1.5s) pass. The queue took 454s to empty after k6 stopped, but that was the `tasks` queue (profile lookups); replies kept up.
- **200 msgs/s: far less loss, but not none.** This run answered 503 on a pool timeout, and the 14 missing messages were those (Meta would redeliver them). That 503 was then reverted, because sustained non-200 answers risk Meta disabling the webhook; those pool timeouts now answer 200 and the same 14 would be silent losses (about 0.04%). Before the fix the same rate lost 576. No webhook fell back to in-process replies, so the broker path held under overload.
- **200 msgs/s still fails the ack target:** p95 9.35s, and k6 dropped 2,381 iterations. The API process hit 101% CPU in bursts and the pool was full in 11 of 1,240 samples (28 connections against a limit of 27). This is the ingest ceiling of one API process. Run more API processes (`WORKERS=N`) to move it.
- **Replies fall behind at 200 msgs/s:** one worker tops out near 64 replies/s (15.7 ms of CPU each) against about 168 messages/s arriving, so the backlog peaked at 57,756 jobs and p50 reply latency reached 175s. Even the p95 of 338s is under the 10-minute `REPLY_MAX_AGE_MINUTES`, so nothing was dropped as stale. Add reply workers to fix it.
- **Left unfinished at 200:** 25,630 `tasks` jobs (profile lookups) were still queued 600s after k6 stopped. They share the worker with replies, so they wait behind them.
- **Bug found by this run:** the first attempt at this commit had 0 replies and a flood of 500s. `811ac7e` removed the `reply_queue` import from `webhooks.py` while `reply_queue.run_later` was still called, so every request that needed a profile lookup raised `NameError` after the commit. The import is restored. The existing tests did not catch it.
- Everything here is still against mocks on one machine. Real Groq latency, its rate limits and real Meta responses are untested.

## Update 2026-11-09 (later): webhook ingest moved to the worker

The API now only verifies the signature, publishes the raw body to the `webhooks` queue and answers 200; the worker stores it (`process_webhook_job`). Same machine and setup as the section above (`alphaBeast`; `LT_QUEUE=1 REPLY_CONCURRENCY=130`, one API process, one worker with 130 reply slots, 8 ingest slots), after the 503 was reverted. Raw output: `loadtest/out/w1-q1/`; the run before this change is in `loadtest/out/w1-q1-before-ingest-queue/`.

| Peak msgs/s | Webhook p95 / p99 | Sent / stored | Failed requests | AI replies | Reply latency p50 / p95 | Max DB connections |
|---|---|---|---|---|---|---|
| 50 | 3.6 ms / 9.3 ms | 9754 / 9754 | 0 | 8713 | 4.0s / 5.3s | 15 |
| 200 | 15.7 ms / 27.8 ms | 39031 / 39031 | 0 | 31430 | 161s / 267s | 16 |

| 200 msgs/s | Ingest inline (before) | Ingest queued (now) |
|---|---|---|
| Webhook p95 / p99 | 9.35s / 15.5s | 15.7 ms / 27.8 ms |
| Messages lost | 14 (pool timeouts, 503 then reverted to 200) | 0 |
| k6 dropped iterations | 2,381 | 21 |
| Pool timeouts | 14 | 0 |
| Pool full (samples) | 11 of 1,240 | 0 of 1,240 |

- **The ack no longer depends on the database.** Both ack targets (p95 < 500 ms, p99 < 1.5s) now pass at 200 msgs/s, with the API process averaging 4% CPU. The offered load also rose (39,031 messages against 35,911), because the API is no longer the bottleneck.
- **Nothing was lost** at either rate, and no webhook fell back to inline ingest. The `webhooks` queue was empty by the end of both runs.
- **The bottleneck moved to the worker.** At 200 msgs/s the worker process hit 101% CPU (avg 62%), and the backlog peaked at 54,180 jobs. One worker now does ingest and replies, so reply latency is about 160s at p50, in line with the previous run (175s). A reply still goes out within `REPLY_MAX_AGE_MINUTES` (the p95 of 267s is under 10 minutes), but it is slow. Add reply workers (`LT_QUEUE=N`) to bring it down.
- **Profile lookups are the first thing to fall behind.** 29,694 `tasks` jobs were still queued 600s after k6 stopped, because they share the worker with replies and ingest.
- **CPU per reply is not comparable to earlier runs.** The 20.8 ms at 200 msgs/s is all worker CPU divided by replies, and the worker now also does the ingest work, so it no longer measures replies alone.
- Still against mocks on one machine.

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
