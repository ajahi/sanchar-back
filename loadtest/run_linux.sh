#!/usr/bin/env bash
# Load test + DB connection sampler for a Linux box. Needs: docker, k6, python venv (.venv) with requirements installed.
#
#   bash loadtest/run_linux.sh 10 30 50 100
#   WORKERS=4 bash loadtest/run_linux.sh 10 20 40      # four uvicorn workers; SAMPLER=0 turns the pg sampler off
#   LT_QUEUE=2 bash loadtest/run_linux.sh 50           # replies via RabbitMQ (own container, :5674) with 2 reply workers
#   WORKERS=4 LT_QUEUE=1 bash loadtest/run_linux.sh 10 20 30 40 50      # production's shape and DB pools (docker-compose.yml)
#   REPLY_CONCURRENCY=130 WORKERS=4 LT_QUEUE=1 ...     # more model calls at once per reply worker (default 20)
#   RABBITMQ_URL=amqp://... LT_QUEUE=2 ...             # use a throwaway broker you already run, never production's:
#                                                      # these workers would take its real jobs. Drain is then a plain wait.
#   DRAIN=600 ...                                      # longest wait for the queues to empty before verifying (default 300);
#                                                      # with no queue, a plain wait (default 40)
#
# Everything is throwaway and separate from production: its own Postgres container (lt-postgres, :5436),
# its own app (:8006) and mock Graph/Groq (:9000). It does not touch the real app, DB or nginx.
# Run it off-peak or on another machine: k6 + the app eat the CPU of whatever box they share.
#
# Besides k6's numbers it samples pg_stat_activity every 0.5s. A connection "idle in transaction" is one the
# app opened and is holding while it does something else (a Graph/LLM call, Python work). Its age and its
# last query say where the pool time goes. Results land in loadtest/out/<TAG>/<peak>/, TAG defaulting to
# w<WORKERS>-q<LT_QUEUE>, so runs of different shapes do not overwrite each other.
set -u
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
PEAKS=${*:-10 50}
WORKERS=${WORKERS:-1}   # uvicorn worker processes for the app under test, e.g. WORKERS=4
SAMPLER=${SAMPLER:-1}   # 0 = skip the pg_stat_activity sampler (its docker exec calls cost a little CPU)
PG=lt-postgres; PGPORT=5436; APP_PORT=8006; MOCK_PORT=9000
export DATABASE_URL=postgresql+asyncpg://lt:lt@localhost:$PGPORT/loadtest
LT_QUEUE=${LT_QUEUE:-0}  # >0 = publish replies to RabbitMQ and run that many app.worker processes
DRAIN=${DRAIN:-$([ "$LT_QUEUE" -gt 0 ] && echo 300 || echo 40)}
TAG=${TAG:-w$WORKERS-q$LT_QUEUE}
RMQ=lt-rabbit; OWN_RMQ=0
# Under Postgres' 100, as compose does: the default 15 + 5 x 4 processes + a worker would be exactly 100.
API_ENV=; WORKER_ENV=; POOL_MAX=$((WORKERS * 20))
if [ "$LT_QUEUE" -gt 0 ]; then
  API_ENV="DB_POOL_SIZE=8 DB_MAX_OVERFLOW=4"; WORKER_ENV="DB_POOL_SIZE=10 DB_MAX_OVERFLOW=5"
  POOL_MAX=$((WORKERS * 12 + LT_QUEUE * 15))
else
  export RABBITMQ_URL=   # a .env that sets one must not send a no-queue run to a broker
fi
APP_PID=; MOCK_PID=; SAMPLER_PID=; CPU_PID=; QUEUE_PID=; WORKER_PIDS=
[ -x "$PY" ] || { echo "no python at $PY. Create it: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt (or run: PY=/path/to/python bash loadtest/run_linux.sh ...)"; exit 1; }
psqlx() { docker exec "$PG" psql -U lt -d "${DB:-loadtest}" -At -F, "$@"; }

cleanup() { kill $APP_PID $MOCK_PID $SAMPLER_PID $CPU_PID $QUEUE_PID $WORKER_PIDS 2>/dev/null; docker rm -f $PG $RMQ >/dev/null 2>&1; }
trap cleanup EXIT

docker rm -f $PG >/dev/null 2>&1
docker run -d --name $PG -e POSTGRES_USER=lt -e POSTGRES_PASSWORD=lt -e POSTGRES_DB=loadtest -p $PGPORT:5432 postgres:16 >/dev/null
until docker exec $PG pg_isready -U lt -d loadtest >/dev/null 2>&1; do sleep 1; done
$PY -m alembic upgrade head >/dev/null || { echo "alembic failed"; exit 1; }

if [ "$LT_QUEUE" -gt 0 ] && [ -z "${RABBITMQ_URL:-}" ]; then   # RABBITMQ_URL preset = use that broker, start none
  docker rm -f $RMQ >/dev/null 2>&1; docker run -d --name $RMQ -p 5674:5672 rabbitmq:3.13 >/dev/null
  # -u rabbitmq matters: a root `docker exec` that runs before the server has made its erlang cookie creates it
  # owned by root, and the server then dies with eacces.
  for _ in $(seq 90); do docker exec -u rabbitmq $RMQ rabbitmq-diagnostics -q check_running >/dev/null 2>&1 && break; sleep 1; done
  docker exec -u rabbitmq $RMQ rabbitmq-diagnostics -q check_running >/dev/null 2>&1 || { echo "rabbitmq did not start:"; docker logs $RMQ 2>&1 | tail -5; exit 1; }
  export RABBITMQ_URL=amqp://guest:guest@localhost:5674/
  OWN_RMQ=1
fi

PORT=$MOCK_PORT $PY -m loadtest.mock_external >/dev/null 2>&1 & MOCK_PID=$!
INSTAGRAM_APP_SECRET=loadtest-secret INSTAGRAM_GRAPH_BASE=http://localhost:$MOCK_PORT \
  GROQ_URL=http://localhost:$MOCK_PORT/chat/completions \
  env $API_ENV $PY -m uvicorn app.main:app --port $APP_PORT --workers $WORKERS > loadtest/app.log 2>&1 & APP_PID=$!
for _ in $(seq 40); do curl -s -m 2 localhost:$APP_PORT/docs -o /dev/null && break; sleep 1; done
curl -s -m 3 localhost:$APP_PORT/docs -o /dev/null || { echo "app did not start, see loadtest/app.log"; exit 1; }
sleep 2   # let every worker finish starting
: > loadtest/worker.log
for _ in $(seq "$LT_QUEUE"); do   # no-op when LT_QUEUE=0
  INSTAGRAM_APP_SECRET=loadtest-secret INSTAGRAM_GRAPH_BASE=http://localhost:$MOCK_PORT \
    GROQ_URL=http://localhost:$MOCK_PORT/chat/completions DB_POOL_PRE_PING=false env $WORKER_ENV $PY -m app.worker >> loadtest/worker.log 2>&1 & WORKER_PIDS="$WORKER_PIDS $!"
done
workers_up() { [ "$(grep -c 'reply worker up' loadtest/worker.log)" -ge "$LT_QUEUE" ]; }
if [ "$LT_QUEUE" -gt 0 ]; then
  for _ in $(seq 30); do workers_up && break; sleep 1; done
  workers_up || { tail -20 loadtest/worker.log; echo "reply workers did not start, see loadtest/worker.log"; exit 1; }
  echo "reply workers via RabbitMQ: $LT_QUEUE, ${REPLY_CONCURRENCY:-20} jobs at once each"
fi
RUNNING=$(grep -c 'Started server process' loadtest/app.log)   # one line per worker; child processes would also count multiprocessing's resource tracker
echo "uvicorn workers requested: $WORKERS, started: $RUNNING, child processes of the parent: $(pgrep -P $APP_PID | wc -l) (includes a helper that is not a worker)"
[ "$RUNNING" -eq "$WORKERS" ] || { echo "worker count mismatch, not testing. Check loadtest/app.log"; exit 1; }

sampler() {  # $1 = output dir
  echo "t,total,active,idle_in_tx,max_idle_in_tx_s" > "$1/conns.csv"
  while true; do
    psqlx -c "select to_char(now(),'HH24:MI:SS.MS'), count(*), count(*) filter (where state='active'),
      count(*) filter (where state='idle in transaction'),
      coalesce(round(max(extract(epoch from now()-state_change)) filter (where state='idle in transaction')::numeric,2),0)
      from pg_stat_activity where datname='loadtest' and pid<>pg_backend_pid()" >> "$1/conns.csv" 2>/dev/null
    psqlx -c "select left(regexp_replace(query,'\s+',' ','g'),90) from pg_stat_activity
      where datname='loadtest' and state='idle in transaction' and pid<>pg_backend_pid()" >> "$1/idle_queries.txt" 2>/dev/null
    sleep 0.5
  done
}

cpu_sampler() {  # $1 = output dir; instantaneous %CPU of the app processes, once a second
  local pids; pids="$APP_PID$(pgrep -P $APP_PID | sed 's/^/,/' | tr -d '\n')$(printf ',%s' $WORKER_PIDS)"
  [ -n "$WORKER_PIDS" ] || pids=${pids%,}
  echo "$pids" | tr ',' '\n' > "$1/cpu_pids.txt"
  exec top -b -d 1 -p "$pids" > "$1/cpu.txt" 2>/dev/null   # exec: killing this job must stop top, not leave it writing for days
}

queue_depth() {  # jobs waiting or in progress; -1 = could not ask, so a failed call never reads as empty
  docker exec $RMQ rabbitmqctl -q list_queues name messages --no-table-headers 2>/dev/null |
    awk '$1=="replies" || $1=="replies.retry" || $1=="tasks" || $1=="webhooks" || $1=="webhooks.retry" { s+=$2 } END { print NR ? s+0 : -1 }'
}

queue_sampler() {  # $1 = output dir; every 10s, a rabbitmqctl call costs about a second of CPU
  echo "t,jobs" > "$1/queue.csv"
  while true; do echo "$(date +%T),$(queue_depth)" >> "$1/queue.csv"; sleep 10; done
}

drain() {  # sets DRAINED to the seconds it took, or leaves it empty if the queues never emptied
  DRAINED=; [ "$OWN_RMQ" = 1 ] || { sleep "$DRAIN"; return; }
  local start=$SECONDS
  while [ $((SECONDS - start)) -lt "$DRAIN" ]; do
    [ "$(queue_depth)" = 0 ] && { DRAINED=$((SECONDS - start)); return; }
    sleep 2
  done
}

loss_summary() {  # $1 = output dir. k6 saw these fail so Meta would redeliver them; a silent one got a 200 and is gone
  local sent stored failed
  sent=$(grep -oE 'messages_sent[. ]+: *[0-9]+' "$1/k6.txt" | grep -oE '[0-9]+$')
  stored=$(grep -oE 'customer messages stored: *[0-9]+' "$1/verify.txt" | grep -oE '[0-9]+$')
  failed=$(grep -oE '✗ [0-9]+' "$1/k6.txt" | grep -oE '[0-9]+' | head -1)
  sent=${sent:-0}; stored=${stored:-0}; failed=${failed:-0}
  local lost=$((sent - stored)) silent=$((sent - stored - failed)); [ "$silent" -lt 0 ] && silent=0
  echo "lost: $lost of $sent | k6 saw $failed requests fail | silent (200 sent, not stored): $silent"
}

mkdir -p "loadtest/out/$TAG"
{ echo "host: $(hostname)  cores: $(nproc)  commit: $(git rev-parse --short HEAD)  date: $(date -u)"
  echo "workers: $WORKERS  reply workers: $LT_QUEUE  reply concurrency: ${REPLY_CONCURRENCY:-20}  llm_rpm: ${LLM_RPM:-0}  llm_tpm: ${LLM_TPM:-0}  LT_AI: ${LT_AI:-1}  peaks: $PEAKS"
} > "loadtest/out/$TAG/run-info.txt"

for P in $PEAKS; do
  OUT=loadtest/out/$TAG/$P; rm -rf "$OUT"; mkdir -p "$OUT"
  echo "=== PEAK=$P msgs/s ==="
  psqlx -c "truncate messages, conversations, customers, notifications, handover_events cascade" >/dev/null 2>&1  # clean slate: counts are per step
  $PY -m loadtest.seed_tenants 20 >/dev/null 2>&1
  curl -s -X POST localhost:$MOCK_PORT/reset >/dev/null; : > loadtest/app.log; : > loadtest/worker.log
  [ "$OWN_RMQ" = 1 ] && for q in replies replies.retry replies.dead tasks tasks.dead webhooks webhooks.retry webhooks.dead; do
    docker exec $RMQ rabbitmqctl -q purge_queue $q >/dev/null 2>&1   # counts are per step
  done
  [ "$SAMPLER" = 1 ] && { sampler "$OUT" & SAMPLER_PID=$!; }
  cpu_sampler "$OUT" & CPU_PID=$!
  [ "$OWN_RMQ" = 1 ] && { queue_sampler "$OUT" & QUEUE_PID=$!; }
  k6 run -q -e BASE_URL=http://localhost:$APP_PORT -e PEAK=$P loadtest/webhook_burst.js 2>&1 | tee "$OUT/k6.txt" \
    | grep -E "messages_sent|dropped_iter|http_req_duration.*webhook|p\(9|webhook 200"
  drain   # let the reply backlog drain
  kill $SAMPLER_PID $CPU_PID $QUEUE_PID 2>/dev/null; QUEUE_PID=
  if [ "$OWN_RMQ" = 1 ]; then
    [ -n "$DRAINED" ] && echo "queues empty ${DRAINED}s after k6 stopped" || echo "QUEUES NOT EMPTY after ${DRAIN}s: reply counts below are low, raise DRAIN"
    awk -F, 'NR>1 && $2+0>m {m=$2+0} END {print "largest backlog seen: " m+0 " jobs"}' "$OUT/queue.csv"
  fi
  $PY -m loadtest.verify 2>&1 | tail -7 | tee "$OUT/verify.txt"
  loss_summary "$OUT" | tee "$OUT/summary.txt"
  if [ "$LT_QUEUE" -gt 0 ]; then   # CPU the reply workers burned, per reply sent
    TICKS=$(getconf CLK_TCK); CPU_S=0
    for pid in $WORKER_PIDS; do CPU_S=$(awk -v t=$TICKS -v s=$CPU_S '{print s + ($14+$15)/t}' /proc/$pid/stat); done
    awk -v c=$CPU_S -v w=$LT_QUEUE -F: '/ai replies stored/{n=$2+0} END{ if(n) printf "reply workers: %d process(es), %.1f cpu-s in total, %.1f ms of CPU per reply (REPLY_CONCURRENCY=%s)\n", w, c, c/n*1000, ENVIRON["REPLY_CONCURRENCY"]?ENVIRON["REPLY_CONCURRENCY"]:"20" }' "$OUT/verify.txt"
  fi
  echo "pool timeouts in logs: $(cat loadtest/app.log loadtest/worker.log | grep -ac 'DB pool exhausted')"
  if [ "$LT_QUEUE" -gt 0 ]; then
    echo "webhooks that fell back to in-process replies: $(grep -acE 'RabbitMQ unavailable|publishing to .* failed' loadtest/app.log)   <- must be 0, or this run did not test the queue"
    echo "reply job failures: $(grep -acE 'reply job .* failed' loadtest/worker.log)"
  fi
  [ "$SAMPLER" = 1 ] && awk -F, 'NR>1{ if($2>mt)mt=$2; if($4>mi)mi=$4; if($5>ma)ma=$5; if($2>=pm)full++; n++ }
    END{ printf "connections: max %d of %d | max held idle-in-tx %d | longest idle-in-tx %.2fs | pools full in %d of %d samples\n", mt,pm,mi,ma,full,n }' pm=$POOL_MAX "$OUT/conns.csv"
  grep '^%Cpu' "$OUT/cpu.txt" | sed -E 's/.* ([0-9.]+) id.*/\1/' | awk '{ s+=$1; n++; if($1<2)z++ } END { if(n) printf "box CPU idle: avg %.0f%%, under 2%% idle for %d of %d seconds\n", s/n, z, n }'
  echo "CPU per app process, reply workers included (100 = one full core; top 5 busiest seconds shown as max):"
  awk 'NR==FNR{want[$1]=1;next} ($1 in want){ n[$1]++; sum[$1]+=$9; if($9>mx[$1])mx[$1]=$9 }
       END{ for(p in n) printf "  pid %s: avg %.0f%%, max %.0f%%\n", p, sum[p]/n[p], mx[p] }' "$OUT/cpu_pids.txt" "$OUT/cpu.txt" | sort
  [ "$SAMPLER" = 1 ] && { echo "what the app was doing while holding connections (last query of idle-in-tx connections):"
  sort "$OUT/idle_queries.txt" 2>/dev/null | uniq -c | sort -rn | head -5; }
done
echo "raw samples: loadtest/out/$TAG/<peak>/conns.csv, queue.csv, idle_queries.txt, cpu.txt; logs of the last peak: loadtest/app.log, worker.log"
