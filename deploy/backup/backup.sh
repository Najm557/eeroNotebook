#!/bin/bash
# Back up eeroNotebook's persistent data (Requirement 13.8).
#
# Everything that survives a `docker compose down` lives in two named volumes:
#
#   eeronotebook_db_data    SurrealDB's rocksdb store at /mydata/database.db
#   eeronotebook_app_data   /app/data — uploads, and langgraph's SQLite checkpoints
#
# Three artifacts per run, because the database is backed up twice on purpose:
#
#   db_data.tar.gz       A cold, byte-exact copy of the rocksdb directory, taken
#                        with SurrealDB STOPPED. This is the restore path of
#                        record: no parser sits between the backup and the data.
#                        A tar taken while the server is writing would not be a
#                        snapshot — rocksdb's WAL and SST files would be captured
#                        mid-flight — so the container really does have to stop.
#                        It comes back up within seconds and the application
#                        needs no restart: it opens a connection per query rather
#                        than holding a pool, so the outage costs at most a
#                        handful of in-flight requests at 03:30.
#
#   database.surql.gz    A logical export, taken hot. Insurance against the one
#                        thing the tar cannot survive: rocksdb files are only
#                        guaranteed to open under the SurrealDB build that wrote
#                        them, and the stack pins the floating tag
#                        surrealdb/surrealdb:v2. SurrealQL text does not care.
#
#   app_data.tar.gz      Ordinary files, tarred hot.
#
# Two things about `surreal export` that this script works around, both found by
# actually restoring what it produced (see restore.sh for the other half):
#
#   * Exporting to stdout CORRUPTS the artifact. The CLI writes its own
#     "exported successfully" log line to the same stream, so the last line of
#     the SQL is not SQL. It therefore exports to a file inside the container and
#     copies that out.
#   * SurrealDB cannot import its own export as-is — see restore.sh.
#
# The export contains the `credential` table, whose provider credentials are
# encrypted with OPEN_NOTEBOOK_ENCRYPTION_KEY. So:
#
#   * the backup directory is 0700 and every artifact 0600, and
#   * the key itself is NOT in here. Without it a restored credential table
#     cannot be decrypted, so the key must be kept somewhere else — a password
#     manager, not this directory, or a stolen backup is a stolen credential.
#     Each run records a fingerprint of the key so a restore can tell whether
#     the key it has is the key the backup was made under.
#
# Run by launchd nightly; see install-schedule.sh. Safe to run by hand:
#
#   ~/stacks/eeronotebook/deploy/backup/backup.sh
#
# Exits non-zero on any failure, and writes nothing partial: artifacts are built
# under a temporary name and moved into place only once complete, and a run that
# fails removes its own directory rather than leaving a half backup that looks
# like a whole one.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/../.env}"

BACKUP_ROOT="${BACKUP_ROOT:-$HOME/backups/eeronotebook}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
DB_CONTAINER="${DB_CONTAINER:-eeronotebook-db}"
DB_VOLUME="${DB_VOLUME:-eeronotebook_db_data}"
APP_VOLUME="${APP_VOLUME:-eeronotebook_app_data}"

# GoTrue's store. Its credentials come from the same .env the stack runs on, so a
# password rotation cannot leave the backup authenticating with a stale one.
AUTH_DB_CONTAINER="${AUTH_DB_CONTAINER:-eeronotebook-authdb}"
AUTH_DB_USER="${AUTH_DB_USER:-supabase_admin}"
AUTH_DB_NAME="${AUTH_DB_NAME:-postgres}"
# Only the auth schema. The database is Supabase's own image, which carries other
# schemas this stack neither uses nor should restore over.
AUTH_DB_SCHEMA="${AUTH_DB_SCHEMA:-auth}"

# launchd gives a job almost no PATH, and OrbStack's docker is not in it.
DOCKER="${DOCKER:-}"
if [ -z "$DOCKER" ]; then
  for candidate in "$HOME/.orbstack/bin/docker" /usr/local/bin/docker /opt/homebrew/bin/docker; do
    [ -x "$candidate" ] && DOCKER="$candidate" && break
  done
fi
[ -n "$DOCKER" ] && [ -x "$DOCKER" ] || { echo "error: docker not found" >&2; exit 1; }

log()  { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*"; }
fail() { log "FAILED: $*"; exit 1; }

[ -f "$ENV_FILE" ] || fail "$ENV_FILE not found — run this on the Dev Server, from the deployed stack"

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
: "${SURREAL_USER:?SURREAL_USER missing from $ENV_FILE}"
: "${SURREAL_PASSWORD:?SURREAL_PASSWORD missing from $ENV_FILE}"
NS="${SURREAL_NAMESPACE:-eeronotebook}"
DB="${SURREAL_DATABASE:-eeronotebook}"

# The surreal CLI reads these itself, so neither ever appears in a command line
# where `ps` could see it.
export SURREAL_PASS="$SURREAL_PASSWORD"

db_running() { "$DOCKER" inspect -f '{{.State.Running}}' "$DB_CONTAINER" 2>/dev/null | grep -q true; }

wait_db_ready() {
  local i
  for i in $(seq 1 60); do
    "$DOCKER" exec "$DB_CONTAINER" /surreal is-ready --endpoint http://localhost:8000 >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

STAMP="$(date +%Y%m%d-%H%M%S)"
RUN_DIR="$BACKUP_ROOT/$STAMP"

umask 077
mkdir -p "$BACKUP_ROOT"
chmod 700 "$BACKUP_ROOT"
mkdir -p "$RUN_DIR"

log "=== eeroNotebook backup $STAMP → $RUN_DIR ==="

# Self-heal: the compose policy is `unless-stopped`, so a run killed between the
# stop and the start below would leave the database down until someone noticed.
# Starting it here means the next night repairs it.
if ! db_running; then
  log "WARNING: $DB_CONTAINER was not running — starting it"
  "$DOCKER" start "$DB_CONTAINER" >/dev/null || fail "$DB_CONTAINER will not start"
  wait_db_ready || fail "$DB_CONTAINER never became ready"
fi

RUN_OK=""
finish() {
  # Whatever happened, the database must be left running.
  if ! db_running; then
    log "restarting $DB_CONTAINER"
    "$DOCKER" start "$DB_CONTAINER" >/dev/null 2>&1 || log "ERROR: could not restart $DB_CONTAINER"
  fi
  if [ -z "$RUN_OK" ]; then
    log "removing incomplete run directory"
    rm -rf "$RUN_DIR"
  fi
}
trap finish EXIT INT TERM

# ─── 1. Database: logical export, taken hot ───────────────────────────────────
log "exporting namespace=$NS database=$DB"
"$DOCKER" exec -e SURREAL_USER -e SURREAL_PASS "$DB_CONTAINER" \
  /surreal export --endpoint http://localhost:8000 --ns "$NS" --db "$DB" /tmp/eero-export.surql \
  >/dev/null 2>&1 || fail "surreal export"
"$DOCKER" cp "$DB_CONTAINER:/tmp/eero-export.surql" "$RUN_DIR/database.surql" >/dev/null \
  || fail "copying the export out of $DB_CONTAINER"

# Leave no copy of the export — it carries the encrypted credential table —
# inside the container. The SurrealDB image is distroless and has no `rm`, so
# overwrite the file with an empty one instead.
: > "$RUN_DIR/.empty"
"$DOCKER" cp "$RUN_DIR/.empty" "$DB_CONTAINER:/tmp/eero-export.surql" >/dev/null 2>&1 || true
rm -f "$RUN_DIR/.empty"

# An export that produced no schema is a failure wearing a zero exit code, and a
# stray CLI log line in the SQL is the corruption described above, so check the
# artifact rather than trusting the pipeline.
[ "$(grep -c '^DEFINE TABLE' "$RUN_DIR/database.surql")" -ge 1 ] \
  || fail "export contains no table definitions"
[ "$(grep -c 'surreal::cli' "$RUN_DIR/database.surql")" -eq 0 ] \
  || fail "export is contaminated with CLI log output"

gzip -9 "$RUN_DIR/database.surql"
log "  ✓ database.surql.gz ($(du -h "$RUN_DIR/database.surql.gz" | cut -f1))"

# ─── 2. Application data: tar of the volume, taken hot ────────────────────────
# Streamed to stdout rather than written through a bind mount, so the artifact
# lands with this shell's umask and no host path is exposed to the container.
log "archiving $APP_VOLUME"
"$DOCKER" run --rm -v "$APP_VOLUME":/v:ro alpine \
  tar czf - -C /v . > "$RUN_DIR/app_data.tar.gz.part" \
  || fail "tar of $APP_VOLUME"
tar tzf "$RUN_DIR/app_data.tar.gz.part" >/dev/null || fail "app_data archive does not read back"
mv "$RUN_DIR/app_data.tar.gz.part" "$RUN_DIR/app_data.tar.gz"
log "  ✓ app_data.tar.gz ($(du -h "$RUN_DIR/app_data.tar.gz" | cut -f1))"

# ─── 2b. Identity database: hot logical dump ──────────────────────────────────
# GoTrue's Postgres holds every member account. Without this, losing that volume
# locks the whole team out of a stack where member accounts are the only way in.
#
# One artifact rather than two, unlike the SurrealDB pair above, and deliberately:
# pg_dump runs in a single transaction and is the restore path of record for
# Postgres, whereas a data-directory copy is locked to the server's major version
# and is unsafe taken hot. Stopping the auth container to tar it would buy nothing
# that pg_dump does not already give.
#
# Skipped rather than fatal when the container is absent, so this script still
# works against a stack deployed before identity existed.
if "$DOCKER" inspect "$AUTH_DB_CONTAINER" >/dev/null 2>&1; then
  log "dumping $AUTH_DB_CONTAINER ($AUTH_DB_NAME, schema $AUTH_DB_SCHEMA)"
  # PGPASSWORD is required even over the container's own socket: this image's
  # pg_hba does not trust supabase_admin locally, and without it pg_dump prompts,
  # writes nothing, and exits in a way that looks like an empty database rather
  # than a refused connection.
  "$DOCKER" exec -e PGPASSWORD="$AUTH_DB_PASSWORD" "$AUTH_DB_CONTAINER" pg_dump \
    --username "$AUTH_DB_USER" --dbname "$AUTH_DB_NAME" \
    --schema "$AUTH_DB_SCHEMA" \
    --clean --if-exists --no-owner --no-privileges \
    > "$RUN_DIR/auth_db.sql.part" \
    || fail "pg_dump of $AUTH_DB_NAME schema $AUTH_DB_SCHEMA"

  # An empty dump would restore silently and lock everyone out, so refuse it here.
  [ -s "$RUN_DIR/auth_db.sql.part" ] || fail "auth dump is empty"

  # Prove it captured the schema GoTrue actually uses, not an empty database that
  # would restore silently and lock everyone out.
  grep -q 'CREATE SCHEMA auth' "$RUN_DIR/auth_db.sql.part" \
    || grep -q 'auth\.users' "$RUN_DIR/auth_db.sql.part" \
    || fail "auth dump contains no auth schema — wrong database?"

  mv "$RUN_DIR/auth_db.sql.part" "$RUN_DIR/auth_db.sql"
  gzip -9 "$RUN_DIR/auth_db.sql"
  AUTH_MEMBERS="$("$DOCKER" exec -e PGPASSWORD="$AUTH_DB_PASSWORD" "$AUTH_DB_CONTAINER" \
    psql -U "$AUTH_DB_USER" -d "$AUTH_DB_NAME" -tAc \
    'SELECT count(*) FROM auth.users;' 2>/dev/null | tr -d ' \r')"
  log "  ✓ auth_db.sql.gz ($(du -h "$RUN_DIR/auth_db.sql.gz" | cut -f1), ${AUTH_MEMBERS:-?} members)"
else
  log "  – $AUTH_DB_CONTAINER absent, skipping identity dump"
  AUTH_MEMBERS="n/a"
fi

# ─── 3. Database: cold, byte-exact copy of the volume ─────────────────────────
# Last, so that if this step fails the logical export is already safely written.
SURREAL_VERSION="$("$DOCKER" exec "$DB_CONTAINER" /surreal version 2>/dev/null | head -1)"
log "stopping $DB_CONTAINER for a consistent copy of $DB_VOLUME"
DOWN_FROM="$(date +%s)"
"$DOCKER" stop "$DB_CONTAINER" >/dev/null || fail "could not stop $DB_CONTAINER"

"$DOCKER" run --rm -v "$DB_VOLUME":/v:ro alpine \
  tar czf - -C /v . > "$RUN_DIR/db_data.tar.gz.part" \
  || fail "tar of $DB_VOLUME"

"$DOCKER" start "$DB_CONTAINER" >/dev/null || fail "could not restart $DB_CONTAINER"
wait_db_ready || fail "$DB_CONTAINER did not come back ready"
log "  ✓ $DB_CONTAINER back up after $(( $(date +%s) - DOWN_FROM ))s"

tar tzf "$RUN_DIR/db_data.tar.gz.part" >/dev/null || fail "db_data archive does not read back"
tar tzf "$RUN_DIR/db_data.tar.gz.part" | grep -q 'database.db/CURRENT' \
  || fail "db_data archive has no rocksdb CURRENT file — wrong volume?"
mv "$RUN_DIR/db_data.tar.gz.part" "$RUN_DIR/db_data.tar.gz"
log "  ✓ db_data.tar.gz ($(du -h "$RUN_DIR/db_data.tar.gz" | cut -f1))"

# ─── 4. Manifest ──────────────────────────────────────────────────────────────
# Enough to tell, at restore time, whether this artifact matches the stack in
# front of you: checksums, the SurrealDB build that wrote it — which matters for
# the rocksdb copy — and a fingerprint, not a copy, of the encryption key the
# credentials were sealed with.
KEY_FP="$(printf '%s' "${OPEN_NOTEBOOK_ENCRYPTION_KEY:-}" | shasum -a 256 | cut -c1-16)"

{
  echo "backup:            $STAMP"
  echo "host:              $(hostname)"
  echo "namespace:         $NS"
  echo "database:          $DB"
  echo "surrealdb:         $SURREAL_VERSION"
  echo "db_volume:         $DB_VOLUME"
  echo "app_volume:        $APP_VOLUME"
  echo "auth_database:     $AUTH_DB_NAME"
  # Recorded because a restore that produces the wrong number of accounts should
  # be obvious from the manifest rather than discovered by someone unable to sign in.
  echo "auth_members:      ${AUTH_MEMBERS:-n/a}"
  echo "encryption_key_fp: $KEY_FP"
  echo ""
  ( cd "$RUN_DIR" && shasum -a 256 db_data.tar.gz database.surql.gz app_data.tar.gz \
      $( [ -f "$RUN_DIR/auth_db.sql.gz" ] && echo auth_db.sql.gz ) )
} > "$RUN_DIR/MANIFEST"

chmod 600 "$RUN_DIR"/*
chmod 700 "$RUN_DIR"

RUN_OK=1

# ─── 5. Retention ─────────────────────────────────────────────────────────────
# Directory names are sortable timestamps, so age comes from the name and no
# stray `touch` can confuse it.
if [ "$RETENTION_DAYS" -gt 0 ]; then
  cutoff="$(date -u -v-"${RETENTION_DAYS}"d +%Y%m%d 2>/dev/null || date -u -d "-${RETENTION_DAYS} days" +%Y%m%d)"
  for d in "$BACKUP_ROOT"/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    case "$name" in
      [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
      *) continue ;;
    esac
    if [ "${name%%-*}" -lt "$cutoff" ]; then
      log "retention: removing $name"
      rm -rf "$d"
    fi
  done
fi

log "=== done: $(find "$BACKUP_ROOT" -maxdepth 1 -type d -name '2*-*' | wc -l | tr -d ' ') backups retained ==="
