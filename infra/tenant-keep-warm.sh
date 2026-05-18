#!/bin/sh
# Tenant dashboard keep-warm. Cron'd every 2 minutes on each host that
# runs tenant containers (Hetzner prod, tenants-LXC dev).
#
# Why: the /dca/console Next.js proxy on Vercel/home has a 25s timeout
# on the upstream fetch to the tenant. Cold-start of the FastAPI
# dashboard + CCXT exchange-client init (OKX/Binance/BitOasis market
# data + price calls) can blow past 25s and surfaces to the user as
# random 502s. Periodic pings keep the CCXT state hot.
#
# Implementation: `docker ps` lists every `-dashboard` container, then
# `docker exec` shells in and hits the local FastAPI /healthz endpoint
# via python3 (curl isn't installed in the bot image). 5s per ping
# with a 20s outer wait. Output is logged so we can confirm the cron
# fires AND watch cold-start trends over time.
set -eu

LOG=${LOG:-/var/log/bitcoiners-tenant-keep-warm.log}
mkdir -p "$(dirname "$LOG")"

ts=$(date -u +%FT%TZ)

# List all running dashboard containers (excludes -daemon siblings).
containers=$(docker ps --filter "name=-dashboard" --format "{{.Names}}" 2>/dev/null || true)

if [ -z "$containers" ]; then
  echo "$ts no_tenants" >> "$LOG"
  exit 0
fi

for name in $containers; do
  # In-container ping. python3 stdlib only — no extra deps required.
  out=$(docker exec "$name" python3 -c '
import urllib.request, time, json, sys
start = time.time()
try:
    r = urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=20)
    body = r.read().decode()
    dt = time.time() - start
    j = json.loads(body)
    n = len(j.get("exchanges_configured", []))
    print("%s %.2fs exchanges=%d" % (r.status, dt, n))
except Exception as e:
    dt = time.time() - start
    print("err %.2fs %s:%s" % (dt, type(e).__name__, e), file=sys.stderr)
    sys.exit(1)
' 2>&1) || true
  echo "$ts $name $out" >> "$LOG"
done
