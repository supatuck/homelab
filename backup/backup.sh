#!/bin/bash
# Weekly host-side backup: consistent db dumps + an rsync mirror of the
# homelab tree to the NAS share. Run as root from /etc/cron.d/homelab-backup
# -- root because several data dirs (postgres, npm certs) are unreadable
# otherwise. The NAS syncs /mnt/media/backups to B2 on its own; nothing here
# uploads offsite.

# bash, not sh: /bin/sh is dash, which has no pipefail. Without it a failing
# `docker exec ... | zstd` still exits 0 on zstd's success and writes a 13-byte
# empty archive that looks exactly like a real dump.
set -euo pipefail

REPO=/opt/homelab
DEST=/mnt/media/backups
MIRROR="$DEST/homelab"
DUMPS="$DEST/dumps/$(date +%F)"
KEEP_DUMPS=8
CONF=/root/.config/homelab-backup/backup.conf

log() { echo "$(date '+%F %T') $*"; }

# One run at a time.
exec 9>/run/homelab-backup.lock
flock -n 9 || { log "another run holds the lock; exiting"; exit 1; }

# The share must actually be mounted, or rsync would fill the local disk
# through the empty mountpoint directory.
mountpoint -q /mnt/media || { log "FATAL: /mnt/media not mounted"; exit 1; }
mkdir -p "$MIRROR" "$DEST/dumps"

log "=== backup start ==="

# --- consistent db dumps ----------------------------------------------------
# sqlite's online .backup needs real file locking, which CIFS cannot promise:
# dump everything to local disk first, copy to the NAS after.
STAGING=$(mktemp -d /var/tmp/homelab-backup.XXXXXX)
trap 'rm -rf "$STAGING"' EXIT
mkdir -p "$STAGING/pg" "$STAGING/sqlite"

# postgres: pg_dump inside each db container; credentials resolve there.
# A container that is simply gone means the stack changed: warn and carry on,
# so one dropped service cannot take the mirror below down with it. A dump
# that fails on a container that IS running stays fatal.
for db in gitea-db calagopus-db; do
  if ! docker inspect "$db" >/dev/null 2>&1; then
    log "WARN: $db not present, skipping dump"
    continue
  fi
  docker exec "$db" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    | zstd -q -3 -o "$STAGING/pg/$db.sql.zst"
done
# bitmagnet grows large but is rebuildable from DHT; the weekly
# cadence of this script is the cap on how often it is dumped. It sets no
# POSTGRES_USER, hence the explicit -U postgres.
if docker inspect bitmagnet-db >/dev/null 2>&1; then
  docker exec bitmagnet-db pg_dump -U postgres -d bitmagnet \
    | zstd -q -3 -o "$STAGING/pg/bitmagnet.sql.zst"
else
  log "WARN: bitmagnet-db not present, skipping dump"
fi

# sqlite: online .backup straight off the live files. Same rule as the pg
# dumps -- a path that has gone away is drift (warn), a .backup that errors on
# a file that IS there is fatal. sqlite3 does not fail on a missing file, it
# helpfully creates an empty one, so the -f test has to come first.
sqlite_dump() {
  if [ ! -f "$1" ]; then
    log "WARN: $1 not present, skipping dump"
    return 0
  fi
  sqlite3 "$1" ".backup '$2'"
}
sqlite_dump "$REPO/vaultwarden/data/db.sqlite3"           "$STAGING/sqlite/vaultwarden.sqlite3"
sqlite_dump "$REPO/uptime-kuma/data/kuma.db"              "$STAGING/sqlite/kuma.db"
sqlite_dump "$REPO/npm/data/database.sqlite"              "$STAGING/sqlite/npm.sqlite"
sqlite_dump "$REPO/jellyfin/config/data/data/jellyfin.db" "$STAGING/sqlite/jellyfin.db"
sqlite_dump "$REPO/open-webui/data/webui.db"              "$STAGING/sqlite/webui.db"

rm -rf "$DUMPS"
mkdir -p "$DUMPS"
cp -r "$STAGING/pg" "$STAGING/sqlite" "$DUMPS/"
log "dumps done: $(du -sh "$DUMPS" | cut -f1)"

# Retention: the newest KEEP_DUMPS dated dump dirs survive.
ls -1d "$DEST/dumps"/*/ | sort | head -n -"$KEEP_DUMPS" | xargs -r rm -rf

# --- mirror -----------------------------------------------------------------
# CIFS cannot hold ownership or modes (the mount forces uid/gid), so none are
# preserved; RESTORE.md covers re-owning on the way back. Excludes match what
# the previous backup tool skipped: rebuildable artwork, caches, models. Raw postgres
# data dirs are excluded because they are torn while the db runs -- the dumps
# above are the restore path. The last three are symlink farms CIFS refuses.
RSYNC_RC=0
rsync -rlt --delete --delete-excluded \
  --exclude '/.compose-check/' \
  --exclude '/llm/models/' \
  --exclude '/jellyfin/config/cache/' \
  --exclude '/jellyfin/cache/' \
  --exclude '/jellyfin/config/data/metadata/' \
  --exclude '/jellyfin/config/data/transcodes/' \
  --exclude '/tdarr/transcode_cache/' \
  --exclude '/npm/data/logs/' \
  --exclude '/bitmagnet/db/' \
  --exclude '/gitea/db/' \
  --exclude '/calagopus/postgres/' \
  --exclude '/open-webui/data/cache/' \
  "$REPO"/ "$MIRROR"/ || RSYNC_RC=$?

case $RSYNC_RC in
  0)     log "mirror done" ;;
  23|24) log "mirror done, some files skipped/vanished mid-copy (rc=$RSYNC_RC)"; RSYNC_RC=0 ;;
  *)     log "FATAL: rsync failed rc=$RSYNC_RC"; exit "$RSYNC_RC" ;;
esac

# --- dead-man switch --------------------------------------------------------
# Reaches this line only on success. PUSH_URL (a Kuma push monitor) comes
# from backup.conf; unset means the switch is not wired up yet.
if [ -f "$CONF" ]; then . "$CONF"; fi
if [ -n "${PUSH_URL:-}" ]; then
  curl -fsS -o /dev/null "$PUSH_URL" || log "WARN: kuma push failed"
fi

log "=== backup complete ==="
