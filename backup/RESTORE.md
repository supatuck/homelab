# Restoring from backups

Backups are plain files on the NAS share, written weekly (Sunday 03:30) by
`backup/backup.sh` via `/etc/cron.d/homelab-backup`:

- `/mnt/media/backups/homelab/` — rsync mirror of this directory (minus the
  excludes listed in the script)
- `/mnt/media/backups/dumps/<YYYY-MM-DD>/` — consistent postgres/sqlite
  dumps, last 8 weeks kept

The NAS syncs `/mnt/media/backups` to Backblaze B2 itself; for anything the
NAS has lost, pull the same paths back from B2 first.

**Always restore databases from `dumps/`, never from the live db files in
the mirror** — those were mid-write when rsync read them.

## Postgres (gitea, bitmagnet, calagopus)

With the `<name>-db` container running and its app container stopped:

```bash
zstd -d < dumps/<date>/pg/gitea-db.sql.zst | docker exec -i gitea-db \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Into a fresh volume this just works; onto an existing database, drop and
recreate first. bitmagnet: same with `-U postgres -d bitmagnet`.

## SQLite (vaultwarden, kuma, npm, jellyfin, open-webui)

Stop the container, put the file back under its expected name, delete stale
`-wal`/`-shm` siblings, start:

```bash
docker stop vaultwarden
cp dumps/<date>/sqlite/vaultwarden.sqlite3 vaultwarden/data/db.sqlite3
rm -f vaultwarden/data/db.sqlite3-{wal,shm}
docker start vaultwarden
```

Destinations: `uptime-kuma/data/kuma.db`, `npm/data/database.sqlite`,
`jellyfin/config/data/data/jellyfin.db`, `open-webui/data/webui.db`.

Vaultwarden also needs the rest of `vaultwarden/data/` (RSA keys,
attachments) from the mirror. Gitea = `gitea/` tree from the mirror +
the gitea-db dump **from the same week**, or repos and DB rows disagree.

## File trees

Copy the path out of the mirror straight over the target (container
stopped). The mirror layout matches the homelab directory 1:1, **but CIFS
holds no ownership or modes** — everything comes back as the copying user.
After restoring, re-own what containers expect, e.g.:

```bash
sudo chown -R 1000:1000 sonarr/            # linuxserver-style apps (PUID/PGID)
```

Postgres raw data dirs should not be restored this way at all — use the
dumps. When in doubt, start the container and read its permission errors.

CIFS also refuses symlinks, so the mirror silently drops them (rsync logs
each one). The notable case is `npm/letsencrypt/live/npm-*/` — those are
symlinks into `../../archive/npm-*/`, which the mirror does keep. After
restoring, either recreate the symlinks against the highest-numbered files
in `archive/` or just let NPM re-issue the certs.

## Not in the backups

- media (the NAS's own backup problem), `llm/models/` (re-downloadable),
  jellyfin metadata/cache/transcodes, npm logs — all rebuildable
- anything newer than the last Sunday run
- the compose stack and glance config — git: git.example.com/you/homelab
  (clone, restore `.env` from the mirror, `docker compose up -d`)
