#!/usr/bin/env bash
# Load test + DB connection sampler for a Linux box. Needs: docker, k6, python venv (.venv) with requirements installed.
#
#   bash loadtest/run_linux.sh 10 30 50 100
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
PG=lt-postgres; PGPORT=5436; APP_PORT=8006; MOCK_PORT=9000
export DATABASE_URL=postgresql+asyncpg://lt:lt@localhost:$PGPORT/loadtest
psqlx() { docker exec "$PG" psql -U lt -d "${DB:-loadtest}" -At -F, "$@"; }

cleanup() { kill $APP_PID $MOCK_PID $SAMPLER_PID 2>/dev/null; docker rm -f $PG >/dev/null 2>&1; }
trap cleanup EXIT

docker rm -f $PG >/dev/null 2>&1
docker run -d --name $PG -e POSTGRES_USER=lt -e POSTGRES_PASSWORD=lt -e POSTGRES_DB=loadtest -p $PGPORT:5432 postgres:16 >/dev/null
until docker exec $PG pg_isready -U lt -d loadtest >/dev/null 2>&1; do sleep 1; done
$PY -m alembic upgrade head >/dev/null || { echo "alembic failed"; exit 1; }

PORT=$MOCK_PORT $PY -m loadtest.mock_external >/dev/null 2>&1 & MOCK_PID=$!
INSTAGRAM_APP_SECRET=loadtest-secret INSTAGRAM_GRAPH_BASE=http://localhost:$MOCK_PORT \
  GROQ_URL=http://localhost:$MOCK_PORT/chat/completions \
  $PY -m uvicorn app.main:app --port $APP_PORT > loadtest/app.log 2>&1 & APP_PID=$!
sleep 6
curl -s -m 3 localhost:$APP_PORT/docs -o /dev/null || { echo "app did not start, see loadtest/app.log"; exit 1; }

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

for P in $PEAKS; do
  OUT=loadtest/out/$P; rm -rf "$OUT"; mkdir -p "$OUT"
  echo "=== PEAK=$P msgs/s ==="
  $PY -m loadtest.seed_tenants 20 >/dev/null 2>&1
  curl -s -X POST localhost:$MOCK_PORT/reset >/dev/null; : > loadtest/app.log
  sampler "$OUT" & SAMPLER_PID=$!
  k6 run -q -e BASE_URL=http://localhost:$APP_PORT -e PEAK=$P loadtest/webhook_burst.js 2>&1 | tee "$OUT/k6.txt" \
    | grep -E "messages_sent|dropped_iter|http_req_duration.*webhook|p\(9|webhook 200"
  sleep 40   # let the reply backlog drain
  kill $SAMPLER_PID 2>/dev/null
  $PY -m loadtest.verify 2>&1 | tail -4 | tee "$OUT/verify.txt"
  echo "pool timeouts in app log: $(grep -c 'QueuePool limit' loadtest/app.log)"
  awk -F, 'NR>1{ if($2>mt)mt=$2; if($4>mi)mi=$4; if($5>ma)ma=$5; if($2>=15)full++; n++ }
    END{ printf "connections: max %d of 15 | max held idle-in-tx %d | longest idle-in-tx %.2fs | pool full in %d of %d samples\n", mt,mi,ma,full,n }' "$OUT/conns.csv"
  echo "what the app was doing while holding connections (last query of idle-in-tx connections):"
  sort "$OUT/idle_queries.txt" 2>/dev/null | uniq -c | sort -rn | head -5
  $PY -m loadtest.seed_tenants --cleanup >/dev/null 2>&1
done
echo "raw samples: loadtest/out/<peak>/conns.csv, idle_queries.txt; app log: loadtest/app.log"
