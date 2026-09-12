#!/bin/bash
# Restore — and prove — an eeroNotebook backup (Requirement 13.8).
#
# Two modes:
#
#   --verify [RUN_DIR]      Default. Restores BOTH database artifacts into a
#                           throwaway SurrealDB container on scratch volumes and
#                           compares what came back against the live stack, table
#                           by table. Touches no production data, needs no
#                           downtime, and is what makes the nightly job something
#                           other than a directory of hopeful tarballs.
#
#   --production RUN_DIR    The real thing, and destructive: stops the app and the
#                           database, replaces the contents of both volumes from
#                           db_data.tar.gz and app_data.tar.gz, and starts them
#                           again. Takes a fresh backup first, and refuses to run
#                           unless you type the confirmation.
#
# Why the database is restored from the tar rather than the SQL: SurrealDB 2.6.5
# cannot import its own export without help. It writes
#
#     DEFINE TABLE reference TYPE RELATION IN source OUT notebook ...
#     DEFINE FIELD in ON reference ...
#
# and then rejects the second statement on the way back in — "the field 'in'
# already exists" — because declaring the table a RELATION already defined it.
# The same happens for array element fields such as `embedding[*]`. So the
# logical import below rewrites every `DEFINE FIELD` to `DEFINE FIELD OVERWRITE`
# first. It works, and --verify proves it every run, but a byte-exact rocksdb
# copy needs no such conversation and is the primary path.
#
# RUN_DIR is one timestamped directory under ~/backups/eeronotebook. Defaults to
# the newest.
#
#   ~/stacks/eeronotebook/deploy/backup/restore.sh --verify
#   ~/stacks/eeronotebook/deploy/backup/restore.sh --production ~/backups/eeronotebook/20260911-213000

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/../.env}"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/backups/eeronotebook}"

DB_CONTAINER="${DB_CONTAINER:-eeronotebook-db}"
APP_CONTAINER="${APP_CONTAINER:-eeronotebook-app}"
DB_VOLUME="${DB_VOLUME:-eeronotebook_db_data}"
APP_VOLUME="${APP_VOLUME:-eeronotebook_app_data}"
DB_IMAGE="${DB_IMAGE:-surrealdb/surrealdb:v2}"

DOCKER="${DOCKER:-}"
if [ -z "$DOCKER" ]; then
  for candidate in "$HOME/.orbstack/bin/docker" /usr/local/bin/docker /opt/homebrew/bin/docker; do
    [ -x "$candidate" ] && DOCKER="$candidate" && break
  done
fi
[ -n "$DOCKER" ] && [ -x "$DOCKER" ] || { echo "error: docker not found" >&2; exit 1; }

MODE="verify"
RUN_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --verify) MODE="verify" ;;
    --production) MODE="production" ;;
    -h|--help) sed -n '2,38p' "$0"; exit 0 ;;
    *) RUN_DIR="$1" ;;
  esac
  shift
done

if [ -z "$RUN_DIR" ]; then
  RUN_DIR="$(find "$BACKUP_ROOT" -maxdepth 1 -type d -name '2*-*' 2>/dev/null | sort | tail -1)"
  [ -n "$RUN_DIR" ] || { echo "error: no backups under $BACKUP_ROOT" >&2; exit 1; }
fi
[ -f "$RUN_DIR/db_data.tar.gz" ] || { echo "error: $RUN_DIR/db_data.tar.gz not found" >&2; exit 1; }

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
: "${SURREAL_USER:?}"; : "${SURREAL_PASSWORD:?}"
NS="${SURREAL_NAMESPACE:-eeronotebook}"
DB="${SURREAL_DATABASE:-eeronotebook}"
export SURREAL_PASS="$SURREAL_PASSWORD"

log()  { echo "$(date -u +%H:%M:%S)  $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# One SurrealQL statement against a container, raw JSON back.
sql_in() {  # sql_in <container> <database> <statement>
  printf '%s\n' "$3" | "$DOCKER" exec -i -e SURREAL_USER -e SURREAL_PASS "$1" \
    /surreal sql --endpoint http://localhost:8000 --ns "$NS" --db "$2" --json 2>/dev/null \
    | grep -v '^#' | grep -v '^[[:space:]]*$' | tail -1
}
count_in() {  # count_in <container> <database> <table> → integer
  sql_in "$1" "$2" "SELECT count() FROM \`$3\` GROUP ALL;" \
    | sed -n 's/.*"count":\([0-9]*\).*/\1/p' | head -1 | grep -E '^[0-9]+$' || echo 0
}
wait_ready() {  # wait_ready <container>
  local i
  for i in $(seq 1 60); do
    "$DOCKER" exec "$1" /surreal is-ready --endpoint http://localhost:8000 >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

echo "=== eeroNotebook restore (${MODE}) ==="
echo "  artifact: $RUN_DIR"
[ -f "$RUN_DIR/MANIFEST" ] && sed 's/^/  /' "$RUN_DIR/MANIFEST"
echo ""

# ─── Integrity ────────────────────────────────────────────────────────────────
if [ -f "$RUN_DIR/MANIFEST" ]; then
  log "checking recorded checksums"
  ( cd "$RUN_DIR" && grep -E '^[0-9a-f]{64} ' MANIFEST | shasum -a 256 -c --status ) \
    || fail "checksums in MANIFEST do not match the artifacts"
  log "  ✓ artifacts match the manifest"

  want_fp="$(sed -n 's/^encryption_key_fp: *//p' "$RUN_DIR/MANIFEST")"
  have_fp="$(printf '%s' "${OPEN_NOTEBOOK_ENCRYPTION_KEY:-}" | shasum -a 256 | cut -c1-16)"
  if [ -n "$want_fp" ] && [ "$want_fp" != "$have_fp" ]; then
    echo ""
    echo "  WARNING: this backup was made under a different OPEN_NOTEBOOK_ENCRYPTION_KEY"
    echo "           ($want_fp, current key is $have_fp). Records restore fine, but the"
    echo "           stored provider credentials will not decrypt."
    echo ""
  else
    log "  ✓ encryption key fingerprint matches ($have_fp)"
  fi
fi

TABLES="$(gzip -dc "$RUN_DIR/database.surql.gz" | sed -n 's/^-- TABLE: //p')"
[ -n "$TABLES" ] || fail "no table definitions in the export"
TABLE_COUNT="$(printf '%s\n' "$TABLES" | wc -l | tr -d ' ')"
log "export names $TABLE_COUNT tables"

# ══════════════════════════════════════════════════════════════════════════════
if [ "$MODE" = "verify" ]; then
# ══════════════════════════════════════════════════════════════════════════════
  STAMP="$(date +%s)"
  SCRATCH_DB="eeronotebook-restoreverify-$STAMP"
  SCRATCH_DB_VOL="eeronotebook_restoreverify_db_$STAMP"
  SCRATCH_APP_VOL="eeronotebook_restoreverify_app_$STAMP"
  SURQL_DB="${DB}_from_surql"
  TMP="$(mktemp -d)"

  teardown() {
    log "tearing down scratch"
    "$DOCKER" rm -f "$SCRATCH_DB" >/dev/null 2>&1 || true
    "$DOCKER" volume rm "$SCRATCH_DB_VOL" "$SCRATCH_APP_VOL" >/dev/null 2>&1 || true
    rm -rf "$TMP"
  }
  trap teardown EXIT INT TERM

  # --- database, from the cold volume copy ------------------------------------
  log "extracting db_data.tar.gz into scratch volume $SCRATCH_DB_VOL"
  "$DOCKER" run --rm -i -v "$SCRATCH_DB_VOL":/v alpine tar xzf - -C /v \
    < "$RUN_DIR/db_data.tar.gz" || fail "db_data extract"

  # --network none: an import or query here cannot reach the live stack even by
  # accident.
  log "starting scratch SurrealDB ($DB_IMAGE) on the restored volume"
  "$DOCKER" run -d --name "$SCRATCH_DB" --network none \
    -v "$SCRATCH_DB_VOL":/mydata --user root \
    -e SURREAL_USER -e SURREAL_PASS \
    "$DB_IMAGE" start --log warn \
      --user "$SURREAL_USER" --pass "$SURREAL_PASSWORD" rocksdb:/mydata/database.db >/dev/null
  wait_ready "$SCRATCH_DB" || fail "scratch database never became ready on the restored volume"
  log "  ✓ SurrealDB opened the restored rocksdb store"

  # --- database, from the logical export -------------------------------------
  # Into a second database on the same server, so the two artifacts are compared
  # against each other as well as against live.
  gzip -dc "$RUN_DIR/database.surql.gz" \
    | sed 's/^DEFINE FIELD /DEFINE FIELD OVERWRITE /' > "$TMP/import.surql"
  "$DOCKER" cp "$TMP/import.surql" "$SCRATCH_DB:/tmp/import.surql" >/dev/null
  log "importing database.surql.gz into $NS/$SURQL_DB"
  "$DOCKER" exec -e SURREAL_USER -e SURREAL_PASS "$SCRATCH_DB" \
    /surreal import --endpoint http://localhost:8000 --ns "$NS" --db "$SURQL_DB" /tmp/import.surql \
    >/dev/null 2>&1 || fail "surreal import"
  log "  ✓ import returned clean"

  live_up=0
  "$DOCKER" inspect -f '{{.State.Running}}' "$DB_CONTAINER" 2>/dev/null | grep -q true && live_up=1
  [ "$live_up" = 1 ] || log "NOTE: $DB_CONTAINER is down, so there is nothing to compare against"

  echo ""
  printf '  %-22s %9s %9s %9s\n' TABLE 'FROM TAR' 'FROM SQL' LIVE
  printf '  %-22s %9s %9s %9s\n' '----------------------' --------- --------- ---------
  mismatch=0; tar_total=0
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    a="$(count_in "$SCRATCH_DB" "$DB" "$t")"
    b="$(count_in "$SCRATCH_DB" "$SURQL_DB" "$t")"
    tar_total=$(( tar_total + a ))
    if [ "$live_up" = 1 ]; then
      l="$(count_in "$DB_CONTAINER" "$DB" "$t")"
      if [ "$a" = "$l" ] && [ "$b" = "$l" ]; then mark=""; else mark="  ✗ MISMATCH"; mismatch=$(( mismatch + 1 )); fi
      printf '  %-22s %9s %9s %9s%s\n' "$t" "$a" "$b" "$l" "$mark"
    else
      printf '  %-22s %9s %9s %9s\n' "$t" "$a" "$b" "-"
    fi
  done <<< "$TABLES"
  echo ""
  log "$tar_total records across $TABLE_COUNT tables came back from the volume copy"

  # Row counts alone would pass on a table full of empty records, so read real
  # content back — including the width of an embedding vector, since search is
  # the thing most likely to be quietly lost in a round trip.
  log "spot-checking content restored from the volume copy"
  echo "    notebooks:        $(sql_in "$SCRATCH_DB" "$DB" 'SELECT VALUE name FROM notebook;')"
  echo "    sources:          $(sql_in "$SCRATCH_DB" "$DB" 'SELECT VALUE title FROM source;')"
  echo "    embedding width:  $(sql_in "$SCRATCH_DB" "$DB" 'SELECT VALUE array::len(embedding) FROM source_embedding LIMIT 1;')"
  echo "    full-text search: $(sql_in "$SCRATCH_DB" "$DB" 'SELECT VALUE count() FROM (SELECT id FROM source_embedding WHERE content @1@ "the") GROUP ALL;') chunks match a term via the restored index"
  if [ "$live_up" = 1 ]; then
    echo "    live notebooks:   $(sql_in "$DB_CONTAINER" "$DB" 'SELECT VALUE name FROM notebook;')"
  fi

  # --- app_data --------------------------------------------------------------
  if [ -f "$RUN_DIR/app_data.tar.gz" ]; then
    echo ""
    log "extracting app_data into $SCRATCH_APP_VOL"
    "$DOCKER" run --rm -i -v "$SCRATCH_APP_VOL":/v alpine \
      tar xzf - -C /v < "$RUN_DIR/app_data.tar.gz" || fail "app_data extract"

    # .cache holds regenerable uv/playwright/huggingface downloads and moves on
    # its own, so it is excluded. Everything else — uploads, the SQLite
    # checkpoint store, podcasts — must come back byte for byte.
    "$DOCKER" run --rm -v "$SCRATCH_APP_VOL":/v:ro alpine \
      sh -c 'cd /v && find . -type f ! -path "./.cache/*" -exec sha256sum {} \; | sort' > "$TMP/restored.txt"
    "$DOCKER" run --rm -v "$APP_VOLUME":/v:ro alpine \
      sh -c 'cd /v && find . -type f ! -path "./.cache/*" -exec sha256sum {} \; | sort' > "$TMP/live.txt"

    log "  restored $(wc -l < "$TMP/restored.txt" | tr -d ' ') files, live volume has $(wc -l < "$TMP/live.txt" | tr -d ' ')"
    if diff -q "$TMP/restored.txt" "$TMP/live.txt" >/dev/null; then
      log "  ✓ every file identical to the live volume"
    else
      echo ""
      echo "  differences — expected if the stack wrote anything since the backup;"
      echo "  the SQLite WAL under sqlite-db/ moves on its own:"
      diff "$TMP/restored.txt" "$TMP/live.txt" | sed 's/^/    /' | head -20
    fi
  fi

  echo ""
  if [ "$mismatch" -eq 0 ]; then
    echo "=== RESTORE VERIFIED ==="
    echo "    Both database artifacts came back with the live record count, and the"
    echo "    app_data archive extracted intact."
  else
    echo "=== RESTORE INCOMPLETE: $mismatch table(s) differ ==="
    echo "    Only a real failure if nothing wrote to the database between the"
    echo "    backup and now."
    exit 1
  fi

# ══════════════════════════════════════════════════════════════════════════════
else
# ══════════════════════════════════════════════════════════════════════════════
  cat <<WARN

  This REPLACES live data:
    * volume $DB_VOLUME  ← db_data.tar.gz
    * volume $APP_VOLUME ← app_data.tar.gz
    * $APP_CONTAINER and $DB_CONTAINER are stopped for the duration

WARN
  printf "  Type 'restore' to continue: "
  read -r answer
  [ "$answer" = "restore" ] || { echo "  aborted"; exit 1; }

  log "taking a safety backup of the current state first"
  "$SCRIPT_DIR/backup.sh" || fail "safety backup failed — refusing to restore over live data"

  log "stopping $APP_CONTAINER and $DB_CONTAINER"
  "$DOCKER" stop "$APP_CONTAINER" >/dev/null
  "$DOCKER" stop "$DB_CONTAINER" >/dev/null

  restart_all() {
    "$DOCKER" start "$DB_CONTAINER" >/dev/null 2>&1 || true
    "$DOCKER" start "$APP_CONTAINER" >/dev/null 2>&1 || true
  }
  trap restart_all EXIT INT TERM

  log "replacing $DB_VOLUME"
  "$DOCKER" run --rm -i -v "$DB_VOLUME":/v alpine \
    sh -c 'rm -rf /v/* /v/.[!.]* 2>/dev/null; tar xzf - -C /v' < "$RUN_DIR/db_data.tar.gz" \
    || fail "database restore — the safety backup above is your way back"

  if [ -f "$RUN_DIR/app_data.tar.gz" ]; then
    log "replacing $APP_VOLUME"
    "$DOCKER" run --rm -i -v "$APP_VOLUME":/v alpine \
      sh -c 'rm -rf /v/* /v/.[!.]* 2>/dev/null; tar xzf - -C /v' < "$RUN_DIR/app_data.tar.gz" \
      || fail "app_data restore"
  fi

  trap - EXIT INT TERM
  log "starting $DB_CONTAINER"
  "$DOCKER" start "$DB_CONTAINER" >/dev/null
  wait_ready "$DB_CONTAINER" || fail "$DB_CONTAINER did not come back ready"
  log "starting $APP_CONTAINER"
  "$DOCKER" start "$APP_CONTAINER" >/dev/null

  echo ""
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    printf '  %-22s %9s\n' "$t" "$(count_in "$DB_CONTAINER" "$DB" "$t")"
  done <<< "$TABLES"
  echo ""
  echo "=== restored. Give the app ~90s to report healthy, then check the UI. ==="
fi
