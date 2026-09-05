# logseq_selfhost

Operations script for a self-hosted [Logseq](https://logseq.com) DB sync server
(`deps/db-sync` node adapter), exposed over a Tailscale tailnet.

Run it **on the server itself**. Requires [uv](https://docs.astral.sh/uv/), Docker,
Tailscale, and restic for backups.

```sh
./logseq_selfhost.py --help          # uv resolves deps from the PEP 723 header
uv run logseq_selfhost.py --help     # equivalent
```

## Configure

Every setting is a flag with an environment variable fallback:

```sh
export LOGSEQ_TAILNET_HOSTNAME=server.tailnet.ts.net
export LOGSEQ_BASE_DIR=/storage/private/logseq-sync
export LOGSEQ_RESTIC_REPOSITORY=rclone:remote:backups/logseq
```

`logseq_selfhost.py config --show-files` prints the resolved settings and the
compose file and systemd units it would write.

## Workflow

```sh
logseq_selfhost.py deploy            # env file, compose file, pull, start
logseq_selfhost.py expose            # tailscale serve on 443/8443/10000
logseq_selfhost.py backup install    # restic service + hourly timer
logseq_selfhost.py doctor            # verify everything end to end
```

Day to day: `status`, `logs`, `restart`, `update <tag>`, `graphs`, `graph <id>`,
`backup run`, `backup snapshots`.

## Notes

- The image is pinned to a dated tag; upstream tracks `master`, so avoid `latest`.
- Clients point at `https://$LOGSEQ_TAILNET_HOSTNAME:$LOGSEQ_PUBLISHED_PORT`
  via `Settings > Advanced > Sync Server URL`.
- Login is federated to Logseq's Cognito; a self-hosted sync URL bypasses the
  paid gate, but an account is still required.
- `graphs` reports each graph's `graph_e2ee` flag. Encryption is fixed when a
  graph is created and applies in transit and on the server, never to the
  client's local copy.
- Backups cover `data/` only, never the env file or compose file.
