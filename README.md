# homelab

> **Template notice.** This is a sanitized snapshot of a real, running stack.
> Placeholders to make your own before it will run: `example.com` (your
> domain), `192.0.2.x` addresses (RFC 5737 documentation range; substitute
> your LAN), the `/opt/homelab` paths in `backup/` and `collections/`, and
> every value in `.env.example` (copy to `.env`, fill in, never commit).
> Group IDs like `992`/`44` (render/video) are distro-specific; check yours
> with `getent group render video`.

A 41-container Docker Compose stack on a single Linux box, fronted by
Nginx Proxy Manager, monitored by Uptime Kuma + Beszel + a Glance dashboard,
with media on a CIFS-mounted NAS and everything else on local NVMe.

This repo tracks only what is needed to rebuild the stack: the compose files,
two locally-built images, the dashboard, the backup and report scripts, and
this file. Everything else in the
working directory is runtime state and is deliberately untracked (see
[Secrets and the gitignore](#secrets-and-the-gitignore)).

## Layout

```
docker-compose.yml       index only: name, include list, networks
compose.infra.yml        proxy, monitoring, ops tooling
compose.media.yml        jellyfin + the *arr stack
compose.download.yml     VPN and everything that lives inside it
compose.apps.yml         productivity apps, gitea, vaultwarden, calagopus
compose.ai.yml           local LLM serving, chat UI, OpenClaw assistant
.env                     secrets and per-host config (never committed)
.env.example             committed template for the above
backup/                  weekly dump + rsync mirror script and its cron unit
collections/             jellyfin studio-coverage report script and cron unit
couchdb/                 local.ini with the settings Obsidian LiveSync needs
llm/                     llama-swap model roster for the local LLM server
pia-wireguard/scripts/   port-forward sync script the VPN container runs
gpu-exporter/            local image: Intel Arc metrics + health endpoint
beszel-agent-gpu/        local image: beszel agent with working Arc GPU stats
glance/config|assets/    dashboard definition and theme (only tracked glance files)
```

`docker compose up -d` at the root brings up everything via the `include:`
list. Each domain file is self-contained enough to reason about alone.

## Services

Ports are host ports. Anything without a port is either internal-only or
reachable through another container's network namespace.

### Infrastructure — `compose.infra.yml`

| Service | Port | Notes |
|---|---|---|
| nginx-proxy-manager | 80, 443, 81 | SSL termination and routing for `*.example.com` |
| uptime-kuma | 3001 | 30+ monitors; feeds the dashboard status line |
| beszel | 8090 | host metrics hub |
| beszel-agent | — | local build, host network; see [Custom images](#custom-images) |
| gpu-exporter | — | local build; Arc GPU metrics + `/health.json` for Glance |
| dozzle | 8080 | live container logs |
| portainer | 9000, 9443 | |
| autoheal | — | restarts unhealthy containers |

### Media — `compose.media.yml`

| Service | Port | Notes |
|---|---|---|
| jellyfin | — | macvlan `192.0.2.41` for DLNA/discovery + proxy network; QSV transcode and HDR tonemapping on the Arc B580 via `/dev/dri` |
| jellyseerr | 9600 | requests |
| prowlarr | 9200 | indexers |
| radarr / radarr4k | 9300 / 9301 | HD and 4K libraries are separate instances |
| sonarr / sonarr4k | 9400 / 9401 | |
| bazarr / bazarr4k | 9500 / 9501 | |
| tdarr | 8265, 8266 | transcode pipeline |
| checkrr | 8585 | library corruption scans |
| flaresolverr | 8191 | |
| byparr | 8192 | |

### Downloads — `compose.download.yml`

Everything here except the VPN container itself runs with
`network_mode: "service:pia-wireguard"`: the containers share the VPN's
network namespace, so if WireGuard is down they have **no** network rather
than a leaking one. Consequence: their ports are published **on the
pia-wireguard container**, and `depends_on` the VPN is required or a client
that starts first is stranded on a dead namespace.

| Service | Port (via pia-wireguard) | Notes |
|---|---|---|
| pia-wireguard | — | Private Internet Access, WireGuard |
| qbittorrent | 9100 (UI), 6881 | internal UI port 8081, moved off 8080 |
| sabnzbd | 9700 | internal port 8085, set in sabnzbd.ini |
| flood | 9101 | alternative torrent UI |
| bitmagnet | 3333 (UI + Torznab), 3334 (DHT) | |
| bitmagnet-db | — | pgautoupgrade (tracks latest postgres major) |

### Apps — `compose.apps.yml`

| Service | Port | Notes |
|---|---|---|
| gitea | 8100, 2222 (SSH) | git.example.com; GitHub theme (lutinglt/gitea-github-theme); + gitea-db (postgres) |
| glance | 8200 | dashboard, 4 pages; paper-and-ink theme in `glance/assets/custom.css` |
| stirling-pdf | 8500 | |
| vaultwarden | 8800 | signups/invitations controlled from `.env` |
| it-tools | 8900 | |
| couchdb | 5984 | Obsidian LiveSync backend |
| calagopus | 8000 | panel.example.com; game server panel (Rust Pterodactyl rewrite). NPM host needs **Websockets Support** on or the console hangs. + calagopus-db (postgres), calagopus-cache (valkey) |
| calagopus-wings | 2022 (SFTP) | node daemon. HTTP API unpublished — the panel proxies it over `proxy`. Game ports are published per-server by wings, see [Game servers](#game-servers) |

### AI — `compose.ai.yml`

| Service | Port | Notes |
|---|---|---|
| llama-swap | 9810 | llama.cpp (SYCL) behind an OpenAI-compatible proxy; loads models from `llm/` on demand onto the Arc B580, unloads when idle. Model roster: `llm/llama-swap.yaml` |
| open-webui | 3000 | chat UI over llama-swap |
| openclaw | 18789 | personal AI agent, Claude subscription auth, web UI only. Deliberately caged: no docker socket, no homelab mount, dropped net caps, `no-new-privileges` — its blast radius is its own workspace dir |

Model sizing on this hardware (B580 12 GB VRAM, 32 GB system RAM): MoE models
up to ~30B run via partial expert offload (`--n-cpu-moe`); dense models cap
out around 14B. Anything Kimi/frontier-class is API-only.

## Networking

- **`proxy`** (bridge): every web UI joins it; NPM routes hostnames to
  container names, so host ports above are mostly a LAN/debugging convenience.
- **`jellyfin_lan`** (macvlan on `eth0`): gives Jellyfin a real LAN address
  (`192.0.2.41`, range `192.0.2.40/29`) so DLNA and client discovery work.
  Note macvlan means the *host* cannot reach that IP directly — test from
  another machine.
- **VPN namespace**: see the downloads section above.

## Storage

- **Media** lives on a NAS CIFS share mounted at `/mnt/media` by
  `/etc/fstab` — credentials in `/root/.smbcred`, mounted with
  `x-systemd.automount` so boot-time network races cannot leave it empty, and
  a Docker drop-in (`RequiresMountsFor=/mnt/media`) so containers cannot
  start against an unmounted path. Containers bind-mount the host path and
  never see SMB credentials. inotify does not work on CIFS: anything watching
  for new files must poll.
- **Application state** (every `./<service>/` directory) is bind-mounted from
  this directory on local NVMe, and backed up weekly (Sunday 03:30) by a
  host cron job — no backup container. `backup/backup.sh` takes consistent
  postgres/sqlite dumps, then rsync-mirrors this tree to
  `/mnt/media/backups` on the NAS (8 weeks of dumps kept); the NAS itself
  syncs that folder to Backblaze B2. Install once with
  `sudo cp backup/homelab-backup.cron /etc/cron.d/homelab-backup`; logs in
  `/var/log/homelab-backup.log`. Restore procedure: `backup/RESTORE.md`.
- **Game server files** are the one exception to the `./<service>/` rule: they
  live at `/var/lib/calagopus-wings/` outside this tree. See below for why —
  and note they are therefore *not* covered by `backup/backup.sh` yet.

## Game servers

Calagopus is a Rust reimplementation of Pterodactyl, split here into the panel
(`calagopus`) and the node daemon (`calagopus-wings`).

**How the ports actually work.** Wings does not host game servers inside
itself. It holds this host's docker socket and creates each server as a
*sibling* container on the same daemon, handing it the port bindings from its
allocation. So a server on 25565 publishes 25565 the moment it is created,
and `calagopus-wings` must **not** publish that range itself — doing so takes
the ports hostage and every server fails to bind. Allocations are configured
in the panel UI, not in compose. The range set up here is **25565–25575**:
one Velocity/BungeeCord proxy on 25565 plus up to ten backends.

**The subnet collision.** Wings defaults its game-server network
(`calagopus_nw`) to `172.18.0.0/16`, which is exactly what `proxy` already
occupies here. Docker refuses the overlapping pool and server creation fails
with no obvious cause. `config.yml` is therefore edited after the panel
generates it, to move the v4 interface to `172.20.0.0/16` (172.17/172.18/
172.19 are taken by `bridge`, `proxy`, `homelab_default`).

**Identical paths.** The four `/etc|var/lib|log|tmp/calagopus-wings` mounts use
the same path on both sides of the colon on purpose. Wings asks the *host*
daemon to bind-mount world files into each game container, so the path it
names must resolve identically on the host — remapping the left side into
`./calagopus/` breaks container creation. This is why they sit outside the
repo.

**Sizing.** The control plane is ~350–500 MB total (panel ~90 MB, postgres
~150 MB, valkey ~20 MB, wings ~100 MB). The JVMs are the real cost: budget
~0.5–1 GB for a Velocity proxy, 1.5–2 GB for a small lobby, 3–4 GB for a
Paper survival server, and 6–10 GB for anything heavily modded. Minecraft's
tick loop is single-threaded per server, so figure roughly one fast core per
populated server. A four-server network lands near 9–10 GB on a 32 GB host.

**Blast radius.** Wings holds a read-write docker socket, which is
root-equivalent on whatever daemon it shares with your other services. That
is inherent to every Pterodactyl-style panel, and the opposite of how
`openclaw` is caged. Keep panel registration disabled, hand out subusers
rather than admin accounts, and consider a separate host for Wings if the
rest of the stack matters to you.

## Secrets and the gitignore

`.gitignore` is an **allowlist**: first line `*`, then explicit `!` entries
for each tracked file. New source files must be allowlisted or they silently
stay untracked; the payoff is that no data directory or secret can ever be
committed by accident.

- `.env` holds all secrets, documented per consumer. Copy `.env.example` and
  fill it in. Verify it is ignored: `git check-ignore -v .env`.
- Compose delivers variables **only** via `${VAR}` interpolation — there is
  no `env_file:` anywhere — so a variable no compose file names reaches no
  container.
- `docker compose config` output contains resolved plaintext secrets. If you
  must write it to disk, use `./.compose-check/` (gitignored, 700/600), never
  `/tmp`.

## Custom images

Two images are built locally, both because upstream could not see the Intel
Arc B580 (Battlemage, `xe` driver):

- **`gpu-exporter/`** — reads engine busy-ticks straight from the xe PMU
  (video engines live on gt1 on this card), serves Prometheus-style metrics
  for the dashboard plus `/health.json`, an aggregate of Uptime Kuma used by
  Glance's status line. Needs `CAP_PERFMON`.
- **`beszel-agent-gpu/`** — the upstream agent image only ships
  `intel_gpu_top` (i915-only, fails on Battlemage). This build compiles
  nvtop 3.3.2 and wraps it in a shim that fills in the `device_name` nvtop
  returns as null, without which Beszel drops every GPU sample.

After editing either: `docker compose build <service> && docker compose up -d <service>`.

## First boot

A bare `docker compose up -d` gets most of the way; these need a hand:

- **NIC**: set the macvlan `parent:` in `docker-compose.yml` to your real
  interface (`ip link` shows it; the file ships with `eth0`).
- **sabnzbd**: boots on internal port 8080 until you set `port = 8085` in
  `./sabnzbd/config/sabnzbd.ini` (created on first start) and restart it;
  only then does the published `9700:8085` mapping work.
- **checkrr**: create `./checkrr/config/checkrr.yaml` before first start,
  or docker creates a directory at that path.
- **beszel-agent**: restart-loops until you add the system in the beszel
  hub UI and paste the key/token pair into `.env`.
- **calagopus-wings**: paste the panel-generated `config.yml` into
  `/etc/calagopus-wings/` on the host before starting wings.

## Operations

```bash
docker compose up -d                 # apply compose changes
docker compose up --dry-run          # check what would change first
docker compose logs -f <service>     # or use dozzle
docker compose build <service>       # rebuild a local image
```

- **Updates**: nearly everything tracks `:latest`, so `docker compose pull
  && docker compose up -d` updates the stack. The postgres containers run
  pgautoupgrade images that migrate their data dirs across majors on their
  own (note the PGDATA override in compose — postgres 18+ images silently
  ignore the bind mount without it). Deliberate exceptions: tdarr pinned
  2.85.01 (2.86.01, still the newest, hot-loops on inotify), openclaw rides
  the 2026.7.2 beta line until stable regains its model catalog, uptime-kuma
  tracks `:2` because upstream parks `:latest` on the frozen 1.x line, and
  beszel-agent is a local build (upstream's Intel tooling can't read
  Battlemage). gitea's GitHub-theme templates are version-locked to the
  Gitea minor: after a pull that lands a new minor, install the matching
  lutinglt/gitea-github-theme release, or move
  `gitea/data/gitea/templates` aside to fall back to the stock layout.
- **Do not** run `docker compose down` casually: the VPN-namespace containers
  make teardown ordering matter. Restart individual services instead.
- **Health**: Uptime Kuma is the source of truth; the Glance Home page
  headline reads from it via gpu-exporter's `/health.json`.

## Worth doing early

- Attach notification channels to Uptime Kuma before you need them — red
  monitors that page no one are just decoration.
- Run `PRAGMA integrity_check` on your SQLite-backed apps once in a while;
  snapshots copy corruption just as faithfully as they copy data.
