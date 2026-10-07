#!/usr/bin/env bash
# Load test + DB connection sampler for a Linux box. Needs: docker, k6, python venv (.venv) with requirements installed.
#
#   bash loadtest/run_linux.sh 10 30 50 100
#   WORKERS=4 bash loadtest/run_linux.sh 10 20 40      # four uvicorn workers; SAMPLER=0 turns the pg sampler off
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
APP_PID=; MOCK_PID=; SAMPLER_PID=; CPU_PID=
[ -x "$PY" ] || { echo "no python at $PY. Create it: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt (or run: PY=/path/to/python bash loadtest/run_linux.sh ...)"; exit 1; }
psqlx() { docker exec "$PG" psql -U lt -d "${DB:-loadtest}" -At -F, "$@"; }

cleanup() { kill $APP_PID $MOCK_PID $SAMPLER_PID $CPU_PID 2>/dev/null; docker rm -f $PG >/dev/null 2>&1; }
trap cleanup EXIT

docker rm -f $PG >/dev/null 2>&1
docker run -d --name $PG -e POSTGRES_USER=lt -e POSTGRES_PASSWORD=lt -e POSTGRES_DB=loadtest -p $PGPORT:5432 postgres:16 >/dev/null
until docker exec $PG pg_isready -U lt -d loadtest >/dev/null 2>&1; do sleep 1; done
$PY -m alembic upgrade head >/dev/null || { echo "alembic failed"; exit 1; }

PORT=$MOCK_PORT $PY -m loadtest.mock_external >/dev/null 2>&1 & MOCK_PID=$!
INSTAGRAM_APP_SECRET=loadtest-secret INSTAGRAM_GRAPH_BASE=http://localhost:$MOCK_PORT \
  GROQ_URL=http://localhost:$MOCK_PORT/chat/completions \
  $PY -m uvicorn app.main:app --port $APP_PORT --workers $WORKERS > loadtest/app.log 2>&1 & APP_PID=$!
for _ in $(seq 40); do curl -s -m 2 localhost:$APP_PORT/docs -o /dev/null && break; sleep 1; done
curl -s -m 3 localhost:$APP_PORT/docs -o /dev/null || { echo "app did not start, see loadtest/app.log"; exit 1; }
sleep 2   # let every worker finish starting
CHILDREN=$(pgrep -P $APP_PID | wc -l)
RUNNING=$(( WORKERS > 1 ? CHILDREN : 1 ))
echo "uvicorn workers requested: $WORKERS, running: $RUNNING, started-server-process lines in app.log: $(grep -c 'Started server process' loadtest/app.log)"
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
  [ "$SAMPLER" = 1 ] && { sampler "$OUT" & SAMPLER_PID=$!; }
  cpu_sampler "$OUT" & CPU_PID=$!
  k6 run -q -e BASE_URL=http://localhost:$APP_PORT -e PEAK=$P loadtest/webhook_burst.js 2>&1 | tee "$OUT/k6.txt" \
    | grep -E "messages_sent|dropped_iter|http_req_duration.*webhook|p\(9|webhook 200"
  sleep 40   # let the reply backlog drain
  kill $SAMPLER_PID $CPU_PID 2>/dev/null
  $PY -m loadtest.verify 2>&1 | tail -4 | tee "$OUT/verify.txt"
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
