#!/usr/bin/env bash
# Load test + DB connection sampler for a Linux box. Needs: docker, k6, python venv (.venv) with requirements installed.
#
#   bash loadtest/run_linux.sh 10 30 50 100
#   WORKERS=4 bash loadtest/run_linux.sh 10 20 40      # four uvicorn workers; SAMPLER=0 turns the pg sampler off
#   LT_QUEUE=2 bash loadtest/run_linux.sh 50           # replies via RabbitMQ (own container, :5674) with 2 reply workers
#   RABBITMQ_URL=amqp://... LT_QUEUE=2 ...             # use a broker you already run instead of starting one
#   DRAIN=120 ...                                      # seconds to wait for the reply backlog before verifying (default 40)
#
# Everything is throwaway and separate from production: its own Postgres container (lt-postgres, :5436),
# its own app (:8006) and mock Graph/Groq (:9000). It does not touch the real app, DB or nginx.
# Run it off-peak or on another machine: k6 + the app eat the CPU of whatever box they share.
#
# Besides k6's numbers it samples pg_stat_activity every 0.5s. A connection "idle in transaction" is one the
# app opened and is holding while it does something else (a Graph/LLM call, Python work). Its age and its
# last query say where the pool time goes. Results land in loadtest/out/<peak>/.
set -u
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
PEAKS=${*:-10 50}
WORKERS=${WORKERS:-1}   # uvicorn worker processes for the app under test, e.g. WORKERS=4
SAMPLER=${SAMPLER:-1}   # 0 = skip the pg_stat_activity sampler (its docker exec calls cost a little CPU)
PG=lt-postgres; PGPORT=5436; APP_PORT=8006; MOCK_PORT=9000
export DATABASE_URL=postgresql+asyncpg://lt:lt@localhost:$PGPORT/loadtest
LT_QUEUE=${LT_QUEUE:-0}  # >0 = publish replies to RabbitMQ and run that many app.worker processes
DRAIN=${DRAIN:-40}
RMQ=lt-rabbit
APP_PID=; MOCK_PID=; SAMPLER_PID=; CPU_PID=; WORKER_PIDS=
[ -x "$PY" ] || { echo "no python at $PY. Create it: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt (or run: PY=/path/to/python bash loadtest/run_linux.sh ...)"; exit 1; }
psqlx() { docker exec "$PG" psql -U lt -d "${DB:-loadtest}" -At -F, "$@"; }

cleanup() { kill $APP_PID $MOCK_PID $SAMPLER_PID $CPU_PID $WORKER_PIDS 2>/dev/null; docker rm -f $PG $RMQ >/dev/null 2>&1; }
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
fi

PORT=$MOCK_PORT $PY -m loadtest.mock_external >/dev/null 2>&1 & MOCK_PID=$!
INSTAGRAM_APP_SECRET=loadtest-secret INSTAGRAM_GRAPH_BASE=http://localhost:$MOCK_PORT \
  GROQ_URL=http://localhost:$MOCK_PORT/chat/completions \
  $PY -m uvicorn app.main:app --port $APP_PORT --workers $WORKERS > loadtest/app.log 2>&1 & APP_PID=$!
for _ in $(seq 40); do curl -s -m 2 localhost:$APP_PORT/docs -o /dev/null && break; sleep 1; done
curl -s -m 3 localhost:$APP_PORT/docs -o /dev/null || { echo "app did not start, see loadtest/app.log"; exit 1; }
sleep 2   # let every worker finish starting
for _ in $(seq "$LT_QUEUE"); do   # no-op when LT_QUEUE=0
  INSTAGRAM_APP_SECRET=loadtest-secret INSTAGRAM_GRAPH_BASE=http://localhost:$MOCK_PORT \
    GROQ_URL=http://localhost:$MOCK_PORT/chat/completions DB_POOL_PRE_PING=false $PY -m app.worker >> loadtest/worker.log 2>&1 & WORKER_PIDS="$WORKER_PIDS $!"
done
[ "$LT_QUEUE" -gt 0 ] && echo "reply workers via RabbitMQ: $LT_QUEUE"
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
  local pids; pids="$APP_PID$(pgrep -P $APP_PID | sed 's/^/,/' | tr -d '\n')"
  echo "$pids" | tr ',' '\n' > "$1/cpu_pids.txt"
  top -b -d 1 -p "$pids" > "$1/cpu.txt" 2>/dev/null
}

for P in $PEAKS; do
  OUT=loadtest/out/$P; rm -rf "$OUT"; mkdir -p "$OUT"
  echo "=== PEAK=$P msgs/s ==="
  psqlx -c "truncate messages, conversations, customers, notifications, handover_events cascade" >/dev/null 2>&1  # clean slate: counts are per step
  $PY -m loadtest.seed_tenants 20 >/dev/null 2>&1
  curl -s -X POST localhost:$MOCK_PORT/reset >/dev/null; : > loadtest/app.log
  [ "$LT_QUEUE" -gt 0 ] && docker exec $RMQ rabbitmqctl -q purge_queue replies >/dev/null 2>&1
  [ "$SAMPLER" = 1 ] && { sampler "$OUT" & SAMPLER_PID=$!; }
  cpu_sampler "$OUT" & CPU_PID=$!
  k6 run -q -e BASE_URL=http://localhost:$APP_PORT -e PEAK=$P loadtest/webhook_burst.js 2>&1 | tee "$OUT/k6.txt" \
    | grep -E "messages_sent|dropped_iter|http_req_duration.*webhook|p\(9|webhook 200"
  sleep $DRAIN   # let the reply backlog drain
  kill $SAMPLER_PID $CPU_PID 2>/dev/null
  $PY -m loadtest.verify 2>&1 | tail -6 | tee "$OUT/verify.txt"
  if [ "$LT_QUEUE" -gt 0 ]; then   # CPU the reply workers burned, per reply sent
    TICKS=$(getconf CLK_TCK); CPU_S=0
    for pid in $WORKER_PIDS; do CPU_S=$(awk -v t=$TICKS -v s=$CPU_S '{print s + ($14+$15)/t}' /proc/$pid/stat); done
    awk -v c=$CPU_S -v w=$LT_QUEUE -F: '/ai replies stored/{n=$2+0} END{ if(n) printf "reply workers: %d process(es), %.1f cpu-s in total, %.1f ms of CPU per reply (REPLY_CONCURRENCY=%s)\n", w, c, c/n*1000, ENVIRON["REPLY_CONCURRENCY"]?ENVIRON["REPLY_CONCURRENCY"]:"20" }' "$OUT/verify.txt"
  fi
  echo "pool timeouts in app log: $(grep -c 'QueuePool limit' loadtest/app.log)"
  [ "$SAMPLER" = 1 ] && awk -F, 'NR>1{ if($2>mt)mt=$2; if($4>mi)mi=$4; if($5>ma)ma=$5; if($2>=15)full++; n++ }
    END{ printf "connections: max %d of 15 | max held idle-in-tx %d | longest idle-in-tx %.2fs | pool full in %d of %d samples\n", mt,mi,ma,full,n }' "$OUT/conns.csv"
  echo "CPU per app process (100 = one full core; top 5 busiest seconds shown as max):"
  awk 'NR==FNR{want[$1]=1;next} ($1 in want){ n[$1]++; sum[$1]+=$9; if($9>mx[$1])mx[$1]=$9 }
       END{ for(p in n) printf "  pid %s: avg %.0f%%, max %.0f%%\n", p, sum[p]/n[p], mx[p] }' "$OUT/cpu_pids.txt" "$OUT/cpu.txt" | sort
  [ "$SAMPLER" = 1 ] && { echo "what the app was doing while holding connections (last query of idle-in-tx connections):"
  sort "$OUT/idle_queries.txt" 2>/dev/null | uniq -c | sort -rn | head -5; }
done
echo "raw samples: loadtest/out/<peak>/conns.csv, idle_queries.txt, cpu.txt; app log: loadtest/app.log"
