# logseq_selfhost

Operations script for a self-hosted [Logseq](https://logseq.com) DB sync server
(`deps/db-sync` node adapter), exposed over a Tailscale tailnet.

Run it **on the server itself**. Requires [uv](https://docs.astral.sh/uv/), Docker,
Tailscale, and restic for backups.

```sh
uv run https://github.com/hessammehr/logseq_selfhost/raw/main/logseq.py --help
```

Or clone and run `./logseq.py --help` — uv resolves dependencies from the
PEP 723 header either way.

## Configure

`--base-dir` and `--tailnet-hostname` are required; everything else has a
default. All options have an environment variable fallback:

```sh
export LOGSEQ_BASE_DIR=/srv/logseq-sync
export LOGSEQ_TAILNET_HOSTNAME=server.tailnet.ts.net
export LOGSEQ_RESTIC_REPOSITORY=rclone:remote:backups/logseq
```

`logseq.py config --show-files` prints the resolved settings and the compose
file and systemd units it would write.

## Workflow

```sh
logseq.py deploy            # env file, compose file, pull, start
logseq.py expose            # tailscale serve on 443/8443/10000
logseq.py backup install    # restic service + hourly timer
logseq.py doctor            # verify everything end to end
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
