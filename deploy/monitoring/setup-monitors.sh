#!/bin/bash
# Register eeroNotebook's health checks with the Dev Server's existing uptime
# monitoring (Requirements 13.6, 13.10).
#
# mon-uptime-kuma is Uptime Kuma 1.x, which has no HTTP API for creating
# monitors — the only programmatic surface is the socket.io channel the browser
# uses, and driving it needs the admin login. So this script writes the monitor
# rows into Kuma's own SQLite database, which is the credential-free path.
# Kuma loads its monitor list into memory at boot and never re-reads the file,
# so it has to be stopped while the rows go in and started again afterwards:
#
#   1. copy kuma.db aside     (the same habit the host already shows:
#                              ~/stacks/monitoring/kuma.db.bak-20260809-100224)
#   2. stop mon-uptime-kuma   (no concurrent writer, no WAL race)
#   3. insert                 (skipped for any monitor that already exists)
#   4. start mon-uptime-kuma
#
# Monitoring is blind for the ~10 s the container takes to come back. Nothing
# else on the host is touched.
#
# Run this ON the Dev Server:
#
#   ~/stacks/eeronotebook/deploy/monitoring/setup-monitors.sh
#
# Idempotent. Re-running only fills in whatever is missing.

set -euo pipefail

KUMA_CONTAINER="${KUMA_CONTAINER:-mon-uptime-kuma}"
KUMA_VOLUME="${KUMA_VOLUME:-monitoring_uptime_kuma_data}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/stacks/monitoring}"

DOCKER="$(command -v docker || echo "$HOME/.orbstack/bin/docker")"
[ -x "$DOCKER" ] || { echo "error: docker not found" >&2; exit 1; }

# ─── What gets monitored ──────────────────────────────────────────────────────
#
# Both are probed by container name over monitoring_net, in plaintext, which is
# how every other monitor on this host already works (earovoice-api, lh-kong,
# lh-auth). Deliberately NOT https://api.eeronotebook.local: that name has no
# DNS entry and its certificate comes from a private CA nothing trusts yet, so a
# monitor pointing there would report a failure that is not the stack's. Once an
# operator has done the /etc/hosts and CA-trust steps that
# deploy/traefik/setup-ingress.sh prints, a third monitor on the TLS front door
# is worth adding — it would cover mon-traefik and the certificate too.
#
# `keyword` rather than plain `http`, matching the newest monitors on this host:
# it fails a 200 carrying the wrong body, which is what a misrouted proxy looks
# like.
GROUP_NAME="📓 eeroNotebook Stack"

# name | url | keyword
MONITORS=(
  # Liveness only — a static {"status":"healthy"} from the API, and one of the
  # few routes exempt from the password middleware, so the monitor needs no
  # credential. It does not prove the database is reachable.
  "eeroNotebook API|http://eeronotebook-app:5055/health|\"status\":\"healthy\""
  # The gateway every inference call passes through (Requirement 13.10). Same
  # route as the container healthcheck. Liveness of the gateway process, not of
  # the backend behind it; backend failures surface to members as the errors
  # task 2.4 verified.
  "eeroNotebook Inference Gateway|http://eeronotebook-inference:4000/health/liveliness|alive"
)

INTERVAL=60
RETRIES=2
RETRY_INTERVAL=60
TIMEOUT=48

echo "=== Registering eeroNotebook with mon-uptime-kuma ==="

"$DOCKER" inspect "$KUMA_CONTAINER" >/dev/null 2>&1 || {
  echo "error: $KUMA_CONTAINER not found. Run this on the Dev Server." >&2
  exit 1
}

# Kuma's image carries sqlite3, so no extra tooling is needed on the host.
kuma_sql() {
  "$DOCKER" run --rm -v "$KUMA_VOLUME":/app/data --entrypoint sqlite3 \
    louislam/uptime-kuma:1 /app/data/kuma.db "$1"
}
kuma_sql_ro() {
  "$DOCKER" exec "$KUMA_CONTAINER" sqlite3 -readonly /app/data/kuma.db "$1"
}

sq() { printf "%s" "$1" | sed "s/'/''/g"; }

# --- Step 1: is there anything to do? ---------------------------------------
existing="$(kuma_sql_ro "SELECT name FROM monitor;")"
missing=0
echo "$existing" | grep -qxF "$GROUP_NAME" || missing=1
for spec in "${MONITORS[@]}"; do
  echo "$existing" | grep -qxF "${spec%%|*}" || missing=1
done

if [ "$missing" -eq 0 ]; then
  echo "  all monitors already present, nothing to do."
  kuma_sql_ro "SELECT '  '||id||'  '||name||'  '||COALESCE(url,'(group)') FROM monitor WHERE name LIKE '%eeroNotebook%';"
  exit 0
fi

# --- Step 2: back the database up -------------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$BACKUP_DIR/kuma.db.bak-$STAMP"
echo ""
echo "Step 1: copying kuma.db to $BACKUP"
"$DOCKER" cp "$KUMA_CONTAINER:/app/data/kuma.db" "$BACKUP"
ls -lh "$BACKUP" | awk '{print "  ✓ "$5" written"}'

# --- Step 3: stop, insert, start --------------------------------------------
echo ""
echo "Step 2: stopping $KUMA_CONTAINER (monitoring is blind for a few seconds)"
"$DOCKER" stop "$KUMA_CONTAINER" >/dev/null

restart_kuma() { "$DOCKER" start "$KUMA_CONTAINER" >/dev/null 2>&1 || true; }
trap restart_kuma EXIT

echo ""
echo "Step 3: inserting monitors"

# user_id 1 is the single admin account this Kuma was set up with.
if ! kuma_sql "SELECT 1 FROM monitor WHERE name = '$(sq "$GROUP_NAME")';" | grep -q 1; then
  kuma_sql "INSERT INTO monitor (name, active, user_id, type, interval, maxretries, retry_interval, timeout, url, accepted_statuscodes_json, weight, expiry_notification)
            VALUES ('$(sq "$GROUP_NAME")', 1, 1, 'group', $INTERVAL, 0, 0, 0, '', '[\"200-299\"]', 2000, 1);"
  echo "  ✓ group: $GROUP_NAME"
else
  echo "  · group already present: $GROUP_NAME"
fi

GROUP_ID="$(kuma_sql "SELECT id FROM monitor WHERE name = '$(sq "$GROUP_NAME")' AND type = 'group';")"
[ -n "$GROUP_ID" ] || { echo "error: group id not found after insert" >&2; exit 1; }

for spec in "${MONITORS[@]}"; do
  IFS='|' read -r name url keyword <<< "$spec"
  if kuma_sql "SELECT 1 FROM monitor WHERE name = '$(sq "$name")';" | grep -q 1; then
    echo "  · already present: $name"
    continue
  fi
  kuma_sql "INSERT INTO monitor (name, active, user_id, type, interval, maxretries, retry_interval, timeout,
                                 url, keyword, invert_keyword, method, accepted_statuscodes_json,
                                 maxredirects, ignore_tls, upside_down, weight, expiry_notification, parent)
            VALUES ('$(sq "$name")', 1, 1, 'keyword', $INTERVAL, $RETRIES, $RETRY_INTERVAL, $TIMEOUT,
                    '$(sq "$url")', '$(sq "$keyword")', 0, 'GET', '[\"200-299\"]',
                    10, 0, 0, 2000, 1, $GROUP_ID);"
  echo "  ✓ $name → $url  (keyword: $keyword)"
done

echo ""
echo "Step 4: starting $KUMA_CONTAINER"
trap - EXIT
"$DOCKER" start "$KUMA_CONTAINER" >/dev/null

for _ in $(seq 1 30); do
  sleep 2
  if [ "$("$DOCKER" inspect -f '{{.State.Health.Status}}' "$KUMA_CONTAINER" 2>/dev/null)" = "healthy" ]; then
    echo "  ✓ healthy again"
    break
  fi
done

echo ""
echo "=== Monitors ==="
kuma_sql_ro "SELECT '  '||id||'  '||name||'  '||COALESCE(NULLIF(url,''),'(group)') FROM monitor WHERE name LIKE '%eeroNotebook%' ORDER BY id;"
echo ""
echo "First heartbeat lands within ${INTERVAL}s. Dashboard: http://$(grep -E '^EERONOTEBOOK_HOST=' "$(dirname "$0")/../.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' \r" || echo localhost):3001"
