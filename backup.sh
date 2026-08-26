#!/bin/sh
# Nightly database dump (IMPLEMENTATION.md §16.6).
#
# Runs in the `backup` service, on the same postgres:16 image as `db` so
# pg_dump and the server can never drift apart in version -- the most common
# way a dump turns out to be unrestorable.
#
# Deliberately not a host cron entry: the operator installs with one command
# and should not have to know that a second, invisible piece of setup exists on
# the host (DESIGN.md §21.4). Equally deliberately, this is the only thing that
# writes here -- the web UI lists and downloads, nothing more.

set -eu

DIR=${BACKUP_TARGET:-/backups}
HOUR=${BACKUP_HOUR_UTC:-3}
RETENTION=${BACKUP_RETENTION_DAYS:-30}
DB_HOST=${BACKUP_DB_HOST:-db}
DB_USER=${POSTGRES_USER:-psycho}
DB_NAME=${POSTGRES_DB:-psychobooking}

# The uid `web` runs as (see Dockerfile). The directory is 0700 because the
# dumps contain clients' problem text (§17), and owned by that uid because
# `web` mounts it read-only to serve downloads to a therapist with no shell
# access.
APP_UID=${APP_UID:-1000}
APP_GID=${APP_GID:-1000}

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"; }

case "$HOUR" in
  ''|*[!0-9]*) log "FATAL: BACKUP_HOUR_UTC must be 0-23, not '$HOUR'"; exit 1 ;;
esac
[ "$HOUR" -le 23 ] || { log "FATAL: BACKUP_HOUR_UTC must be 0-23, not '$HOUR'"; exit 1; }

mkdir -p "$DIR"
chown "$APP_UID:$APP_GID" "$DIR" 2>/dev/null || log "WARNING: could not chown $DIR"
chmod 0700 "$DIR"

dump() {
  # A name that is free, so a second run in one day never overwrites the first.
  name="psychobooking-$(date -u +%F).dump"
  if [ -e "$DIR/$name" ]; then
    name="psychobooking-$(date -u +%F-%H%M%S).dump"
  fi

  # Written under a temporary name and moved into place: the admin UI matches
  # the final pattern only, so a half-written dump is never listed and never
  # served (§16.6).
  tmp="$DIR/.in-progress-$$.dump"
  rm -f "$tmp"

  log "dumping $DB_NAME from $DB_HOST"
  if ! pg_dump -h "$DB_HOST" -U "$DB_USER" -Fc "$DB_NAME" > "$tmp"; then
    rm -f "$tmp"
    log "ERROR: pg_dump failed; keeping every existing dump"
    return 1
  fi

  chown "$APP_UID:$APP_GID" "$tmp" 2>/dev/null || true
  chmod 0600 "$tmp"
  mv "$tmp" "$DIR/$name"
  log "wrote $name ($(wc -c < "$DIR/$name") bytes)"

  # Only after a success. A failed run is exactly the one whose predecessors
  # matter most (§16.6).
  find "$DIR" -maxdepth 1 -type f -name 'psychobooking-*.dump' \
    -mtime "+$RETENTION" -print -delete | while read -r gone; do
      log "pruned $(basename "$gone")"
    done
}

seconds_until_hour() {
  # Recomputed from the wall clock every iteration rather than sleeping a flat
  # 86400, so a restart does not permanently shift the hour (§16.6). Always
  # strictly in the future, so a restart right after a run does not dump again.
  now_h=$(date -u +%-H)
  now_m=$(date -u +%-M)
  now_s=$(date -u +%-S)
  target=$(( HOUR * 3600 ))
  current=$(( now_h * 3600 + now_m * 60 + now_s ))
  delta=$(( target - current ))
  if [ "$delta" -le 0 ]; then
    delta=$(( delta + 86400 ))
  fi
  echo "$delta"
}

log "started; dumping daily at ${HOUR}:00 UTC into $DIR, keeping $RETENTION days"

# A fresh install would otherwise show an empty backups page for up to a day,
# which reads as "backups are not working" rather than "not yet".
if [ -z "$(find "$DIR" -maxdepth 1 -type f -name 'psychobooking-*.dump' 2>/dev/null)" ]; then
  log "no dump exists yet; taking one now"
  dump || exit 1
fi

while true; do
  wait_for=$(seconds_until_hour)
  log "next dump in ${wait_for}s"
  sleep "$wait_for"
  dump || exit 1
done
