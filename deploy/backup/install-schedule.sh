#!/bin/bash
# Put backup.sh on a nightly schedule (Requirement 13.8).
#
# launchd, not cron: this is macOS, and a user LaunchAgent needs no sudo — which
# matters, because this host does not grant sudo without a password. See the
# comments in the plist template for why it is an agent rather than a daemon.
#
# Run this ON the Dev Server:
#
#   ~/stacks/eeronotebook/deploy/backup/install-schedule.sh
#
# Idempotent: re-running re-renders the plist and reloads the job.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.eeronotebook.backup"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/backups/eeronotebook}"
LOG="$BACKUP_ROOT/launchd.log"
HOUR="${BACKUP_HOUR:-3}"
MINUTE="${BACKUP_MINUTE:-30}"

case "$(uname -s)" in
  Darwin) ;;
  *) echo "error: launchd is macOS only. Run this on the Dev Server." >&2; exit 1 ;;
esac

# 0700 from the start: the database export inside carries the encrypted
# credential table, so this directory is not world-readable at any point.
mkdir -p "$BACKUP_ROOT"
chmod 700 "$BACKUP_ROOT"
mkdir -p "$HOME/Library/LaunchAgents"

echo "=== Scheduling $LABEL ==="
echo "  script:  $SCRIPT_DIR/backup.sh"
echo "  when:    daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE") local"
echo "  output:  $BACKUP_ROOT"
echo "  log:     $LOG"
echo ""

sed -e "s|__SCRIPT__|$SCRIPT_DIR/backup.sh|g" \
    -e "s|__WORKDIR__|$SCRIPT_DIR|g" \
    -e "s|__LOG__|$LOG|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__HOUR__|$HOUR|g" \
    -e "s|__MINUTE__|$MINUTE|g" \
    "$SCRIPT_DIR/com.eeronotebook.backup.plist.template" > "$PLIST"
chmod 644 "$PLIST"
plutil -lint "$PLIST" >/dev/null || { echo "error: rendered plist is not valid" >&2; exit 1; }
echo "  ✓ $PLIST"

# bootout first so a changed schedule actually takes: launchd keeps the job
# definition it loaded, not the file on disk.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
echo "  ✓ loaded into gui/$UID"
echo ""

launchctl print "gui/$UID/$LABEL" 2>/dev/null \
  | grep -E '^\s*(state|path|program|runs|last exit code) ' | sed 's/^/  /' || true

echo ""
echo "Useful afterwards:"
echo "  run it now:   launchctl kickstart -p gui/$UID/$LABEL"
echo "  check it:     launchctl print gui/$UID/$LABEL | grep -E 'runs|last exit'"
echo "  prove it:     $SCRIPT_DIR/restore.sh --verify"
echo "  remove it:    launchctl bootout gui/$UID/$LABEL && rm $PLIST"
echo ""
echo "One thing this cannot do for you: OPEN_NOTEBOOK_ENCRYPTION_KEY is deliberately"
echo "not in the backups. Keep a copy somewhere else, or a restored database will"
echo "come back with provider credentials nothing can decrypt."
