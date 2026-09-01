# Logseq self-hosted sync backend

Self-hosted RTC sync for **Logseq 2.0 (DB version)**. Notes live on your own box;
no subscription needed. Set up 2026-08/09.

Placeholders: `$SERVER` = tailnet host (e.g. `host.tailnet.ts.net`), `$BASE` = deploy dir,
`$UID` = owning uid.

## Architecture

| Plane | Where it runs |
|---|---|
| Notes, assets, sync state | **Self-hosted** (`$BASE/data`) |
| Identity / login | **Federated to Logseq's AWS Cognito** — hardcoded in the client |

Logseq never sees note content. But the client's Cognito pool is a *compile-time*
constant (`frontend/config.cljs`: `USER-POOL-ID`, `COGNITO-IDP`), so a Logseq
account is required unless you rebuild the client. Only `sync-server-url` and
`publish-server-url` are runtime-configurable (localStorage).

## Image choice: `sync`, not `sync-worker`

`sync` is the **node adapter** — upstream's designated self-hosting path
(`deps/db-sync/README.md`), distroless, plain SQLite + files on disk.

`sync-worker` runs `wrangler dev` permanently with emulated D1/DO/R2. It adds
REST/OpenAPI/**MCP** APIs (requires a **non-E2EE graph**), but stores state
opaquely under `/data/wrangler`. **Storage is incompatible — pick one up front.**
Measured: 253 MB / 83 MiB idle vs 488 MB / 639 MiB.

Not a one-way door: RTC is local-first, so switching = point client at new URL and re-upload.

## Deploy

`$BASE/.env` (mode 600):

```sh
DB_SYNC_PORT=8787
DB_SYNC_BASE_URL=https://$SERVER:10000   # must be the externally reachable URL
DB_SYNC_DATA_DIR=/app/data
DB_SYNC_STORAGE_DRIVER=sqlite
DB_SYNC_ASSETS_DRIVER=filesystem
DB_SYNC_LOG_LEVEL=info
# Logseq's official pool — keeps the normal Logseq login
COGNITO_ISSUER=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_dtagLnju8
COGNITO_CLIENT_ID=69cs1lgme7p8kbgld8n5kseii6
COGNITO_JWKS_URL=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_dtagLnju8/.well-known/jwks.json
DB_SYNC_ADMIN_TOKEN=<openssl rand -hex 32>
```

`$BASE/docker-compose.yml`:

```yaml
services:
  sync:
    image: ghcr.io/yshalsager/logseq-selfhost-sync:20260829-39b4311  # pin; repo tracks master
    container_name: logseq-selfhost-sync
    user: "$UID:$UID"          # so a bind mount stays owned by a real user
    env_file: [.env]
    ports: ["127.0.0.1:8787:8787"]
    volumes: ["$BASE/data:/app/data"]
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    restart: unless-stopped
```

Expose over the tailnet (gives a real cert; WS proxies fine). HTTPS ports allowed: 443, 8443, 10000:

```sh
sudo tailscale serve --bg --https=10000 8787
```

Verify: `curl https://$SERVER:10000/health` → `{"ok":true}`; `/graphs` → `401`.

## Client setup

1. Sign in to a Logseq account. **Without a token the client never opens a connection at all.**
2. Settings → Advanced → **Sync Server URL** = `https://$SERVER:10000`
3. Sidebar graph name → **Create db graph** → tick **"Use Logseq Sync?"**

**A graph cannot be converted to synced after creation** — it must be created that way.
Encryption is *optional*, despite docs implying otherwise. Unencrypted = server-side
SQLite is readable, so backups are restorable on their own; E2EE = backups are
useless without the user-held password.

## Backup

Graph data only (`data/`), never the compose/`.env`. Own restic repo, separate from
other backups. Offset the timer (`OnCalendar=*:30`) to avoid contending with other jobs.

```ini
# /etc/systemd/system/restic-backup-logseq.service
[Service]
Type=oneshot
User=<user>
Environment="RESTIC_PASSWORD_FILE=/etc/restic/password"
Environment="RESTIC_REPOSITORY=rclone:<remote>:backups/logseq"
Environment="RCLONE_CONFIG=/home/<user>/.config/rclone/rclone.conf"
ExecStart=restic backup $BASE/data
ExecStartPost=restic forget --prune --keep-hourly 24 --keep-daily 30 --keep-monthly 12
```

DB is rollback-journal mode (header bytes 18/19 = `1 1`), so no `-wal` to miss. An
hourly snapshot can still catch a mid-write; low stakes since every client holds a
full local copy.

## Gotchas

- **No subscription needed.** PR #12459 changed the RTC entitlement check to
  `(or (some? custom-sync-server-url) (user-groups ...))` — a custom URL bypasses the
  paid/invite gate. It also means the **UI offers sync while logged out**, then silently
  does nothing (no token → no connection).
- **The node adapter logs no requests, at any log level.** Raising `DB_SYNC_LOG_LEVEL`
  is useless for debugging traffic. Use the proxy recipe below.
- **`DB_SYNC_ALLOW_UNVERIFIED_JWT_CLAIMS=true`** makes the server accept JWT claims
  without verifying signatures (`worker/auth.cljs`). Still requires a present,
  unexpired token — it is *not* a login bypass. Only sane behind a tailnet.
- Docker `NET I/O` counters are a poor proxy for "did the client connect" — they misled
  a diagnosis. Get real status codes instead.

## Troubleshooting

**Is the client even talking to us?** Insert a logging reverse proxy: nginx on `:8788`
proxying to `:8787` with `proxy_set_header Upgrade/Connection` and a log format
including `$status` and `$http_upgrade`; repoint `tailscale serve` at 8788. Log the
*presence* of `$http_authorization`, never its value.

> nginx logs a WebSocket only when it **closes** — an open RTC socket shows zero
> upgrade lines. Confirm with `ss -tnp | grep :8787` instead.

**Client-side checks** (macOS): `lsof -iTCP -sTCP:ESTABLISHED -n -P | grep -i logseq`;
`localStorage.getItem('sync-server-url')` in devtools. Auth keys live in
`~/Library/Application Support/Logseq/Local Storage/leveldb`.

**Read the graph** (no sqlite3 needed):

```sh
docker run --rm -v $BASE/data/graphs/<graph-uuid>:/db:ro alpine \
  sh -c 'apk add --no-cache sqlite >/dev/null; sqlite3 /db/db.sqlite \
  "select t, outliner_op, tx from tx_log order by t desc limit 5;"'
```

Schema: `kvs(addr, content, addresses)`, `tx_log(t, tx, created_at, outliner_op)`,
`sync_meta`. Block text appears as `:block/title` in transit-JSON — readable iff
the graph is unencrypted. Journal page UUIDs encode the date
(`00000001-2026-0901-...` = 2026-09-01).

## Resources

- [yshalsager/logseq-selfhost](https://github.com/yshalsager/logseq-selfhost) — the images
- [deps/db-sync README](https://github.com/logseq/logseq/blob/master/deps/db-sync/README.md) — env vars, admin tooling (`download-graph-db`, `show-sqlite-checksum`)
- [PR #12459](https://github.com/logseq/logseq/pull/12459) — custom sync URL + entitlement bypass
- [PR #12896](https://github.com/logseq/logseq/pull/12896) — semantic REST + MCP (sync-worker)
- [db-version.md](https://github.com/logseq/docs/blob/master/db-version.md) — official DB/RTC docs
- [4shutosh guide](https://4shutosh.com/selfhost-logseq) — pre-#12459 route; only needed for own-Cognito + patched client
