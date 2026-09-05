#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer>=0.12", "rich>=13.7", "httpx>=0.27"]
# ///

from __future__ import annotations

import json
import secrets
import shlex
import subprocess
from dataclasses import dataclass
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

SYNC_IMAGE = "ghcr.io/yshalsager/logseq-selfhost-sync:20260829-39b4311"
SQLITE_IMAGE = "alpine:latest"
CONTAINER_NAME = "logseq-selfhost-sync"
CONTAINER_PORT = 8787
TAILSCALE_HTTPS_PORTS = (443, 8443, 10000)

LOGSEQ_COGNITO_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_dtagLnju8"
LOGSEQ_COGNITO_CLIENT_ID = "69cs1lgme7p8kbgld8n5kseii6"
LOGSEQ_COGNITO_JWKS_URL = f"{LOGSEQ_COGNITO_ISSUER}/.well-known/jwks.json"

console = Console()


@dataclass(frozen=True)
class Deployment:
    ssh_target: str
    tailnet_hostname: str
    base_dir: str
    image: str
    run_as: str
    published_port: int
    restic_repository: str
    restic_password_file: str
    rclone_config: str

    @property
    def data_dir(self) -> str:
        return f"{self.base_dir}/data"

    @property
    def graphs_dir(self) -> str:
        return f"{self.data_dir}/graphs"

    @property
    def index_database(self) -> str:
        return "index.sqlite"

    @property
    def public_url(self) -> str:
        return f"https://{self.tailnet_hostname}:{self.published_port}"

    @property
    def backup_unit(self) -> str:
        return "restic-backup-logseq"


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Operate a self-hosted Logseq DB sync server (node adapter) exposed over a Tailscale tailnet.",
)
backup_app = typer.Typer(no_args_is_help=True, help="Graph-data backups into a dedicated restic repository.")
app.add_typer(backup_app, name="backup")


@app.callback()
def configure(
    ctx: typer.Context,
    ssh_target: Annotated[str, typer.Option(envvar="LOGSEQ_SSH_TARGET", help="SSH destination hosting the sync server.")] = "user@server",
    tailnet_hostname: Annotated[str, typer.Option(envvar="LOGSEQ_TAILNET_HOSTNAME", help="MagicDNS name clients connect to.")] = "server.tailnet.ts.net",
    base_dir: Annotated[str, typer.Option(envvar="LOGSEQ_BASE_DIR", help="Remote directory holding compose file, env file and graph data.")] = "/storage/private/logseq-sync",
    image: Annotated[str, typer.Option(envvar="LOGSEQ_IMAGE", help="Pinned sync server image; the upstream project tracks master, so never ride latest.")] = SYNC_IMAGE,
    run_as: Annotated[str, typer.Option(envvar="LOGSEQ_RUN_AS", help="uid:gid owning the bind-mounted data directory.")] = "1000:1000",
    published_port: Annotated[int, typer.Option(envvar="LOGSEQ_PUBLISHED_PORT", help="Tailscale HTTPS port serving the sync API.")] = 10000,
    restic_repository: Annotated[str, typer.Option(envvar="LOGSEQ_RESTIC_REPOSITORY", help="Restic repository dedicated to Logseq graph data.")] = "rclone:remote:backups/logseq",
    restic_password_file: Annotated[str, typer.Option(envvar="LOGSEQ_RESTIC_PASSWORD_FILE", help="Path to the restic password file on the server.")] = "/etc/restic/password",
    rclone_config: Annotated[str, typer.Option(envvar="LOGSEQ_RCLONE_CONFIG", help="Path to rclone.conf used by the backup unit.")] = "/home/user/.config/rclone/rclone.conf",
) -> None:
    ctx.obj = Deployment(
        ssh_target=ssh_target,
        tailnet_hostname=tailnet_hostname,
        base_dir=base_dir,
        image=image,
        run_as=run_as,
        published_port=published_port,
        restic_repository=restic_repository,
        restic_password_file=restic_password_file,
        rclone_config=rclone_config,
    )


def run_remote(deployment: Deployment, script: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", deployment.ssh_target, "bash -s"],
        input=script,
        text=True,
        capture_output=True,
    )
    if check and completed.returncode != 0:
        console.print(Panel(completed.stderr.strip() or completed.stdout.strip(), title="remote command failed", border_style="red"))
        raise typer.Exit(completed.returncode)
    return completed.stdout


def stream_remote(deployment: Deployment, script: str) -> int:
    return subprocess.run(["ssh", "-t", deployment.ssh_target, "bash -s"], input=script, text=True).returncode


def query_sqlite(deployment: Deployment, database: str, sql: str) -> list[dict]:
    inner = f"apk add --no-cache sqlite >/dev/null 2>&1; sqlite3 -json /data/{database} {shlex.quote(sql)}"
    script = f"docker run --rm -v {deployment.data_dir}:/data:ro {SQLITE_IMAGE} sh -c {shlex.quote(inner)}"
    output = run_remote(deployment, script).strip()
    return json.loads(output) if output else []


def environment_file(deployment: Deployment, admin_token: str) -> str:
    return "\n".join(
        [
            f"DB_SYNC_PORT={CONTAINER_PORT}",
            f"DB_SYNC_BASE_URL={deployment.public_url}",
            "DB_SYNC_DATA_DIR=/app/data",
            "DB_SYNC_STORAGE_DRIVER=sqlite",
            "DB_SYNC_ASSETS_DRIVER=filesystem",
            "DB_SYNC_LOG_LEVEL=info",
            f"COGNITO_ISSUER={LOGSEQ_COGNITO_ISSUER}",
            f"COGNITO_CLIENT_ID={LOGSEQ_COGNITO_CLIENT_ID}",
            f"COGNITO_JWKS_URL={LOGSEQ_COGNITO_JWKS_URL}",
            f"DB_SYNC_ADMIN_TOKEN={admin_token}",
            "",
        ]
    )


def compose_file(deployment: Deployment) -> str:
    return f"""services:
  sync:
    image: {deployment.image}
    container_name: {CONTAINER_NAME}
    user: "{deployment.run_as}"
    env_file: [.env]
    ports: ["127.0.0.1:{CONTAINER_PORT}:{CONTAINER_PORT}"]
    volumes: ["{deployment.data_dir}:/app/data"]
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    restart: unless-stopped
"""


def backup_service_unit(deployment: Deployment) -> str:
    return f"""[Unit]
Description=Restic backup of Logseq graph data
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User={deployment.run_as.split(":")[0]}
Environment="RESTIC_PASSWORD_FILE={deployment.restic_password_file}"
Environment="RESTIC_REPOSITORY={deployment.restic_repository}"
Environment="RCLONE_CONFIG={deployment.rclone_config}"
ExecStart=restic backup {deployment.data_dir}
ExecStartPost=restic forget --prune --keep-hourly 24 --keep-daily 30 --keep-monthly 12
"""


def backup_timer_unit() -> str:
    return """[Unit]
Description=Hourly restic backup of Logseq graph data

[Timer]
OnCalendar=*:30
Persistent=true

[Install]
WantedBy=timers.target
"""


def restic_environment(deployment: Deployment) -> str:
    return (
        f"export RESTIC_PASSWORD_FILE={deployment.restic_password_file} "
        f"RESTIC_REPOSITORY={deployment.restic_repository} "
        f"RCLONE_CONFIG={deployment.rclone_config}"
    )


@app.command(help="Print the resolved deployment settings and the files that would be written.")
def config(ctx: typer.Context, show_files: Annotated[bool, typer.Option(help="Also render the compose and unit files.")] = False) -> None:
    deployment: Deployment = ctx.obj
    table = Table(show_header=False, box=None)
    for field, value in vars(deployment).items():
        table.add_row(field.replace("_", " "), str(value))
    table.add_row("public url", deployment.public_url)
    console.print(Panel(table, title="deployment", border_style="cyan"))
    if show_files:
        console.print(Panel(Syntax(compose_file(deployment), "yaml"), title="docker-compose.yml"))
        console.print(Panel(Syntax(backup_service_unit(deployment), "ini"), title=f"{deployment.backup_unit}.service"))


@app.command(help="Create the data directory, write compose and env files, then start the sync server.")
def deploy(ctx: typer.Context, regenerate_admin_token: Annotated[bool, typer.Option(help="Overwrite an existing .env with a fresh admin token.")] = False) -> None:
    deployment: Deployment = ctx.obj
    admin_token = secrets.token_hex(32)
    overwrite = "true" if regenerate_admin_token else "false"
    script = f"""set -euo pipefail
mkdir -p {deployment.data_dir}
chown -R {deployment.run_as} {deployment.base_dir}
if [ ! -f {deployment.base_dir}/.env ] || [ {overwrite} = true ]; then
  cat > {deployment.base_dir}/.env <<'ENVEOF'
{environment_file(deployment, admin_token)}ENVEOF
  chmod 600 {deployment.base_dir}/.env
  chown {deployment.run_as} {deployment.base_dir}/.env
  echo "wrote .env"
else
  echo "kept existing .env"
fi
cat > {deployment.base_dir}/docker-compose.yml <<'COMPOSEEOF'
{compose_file(deployment)}COMPOSEEOF
chown {deployment.run_as} {deployment.base_dir}/docker-compose.yml
cd {deployment.base_dir}
docker compose pull
docker compose up -d
"""
    console.print(run_remote(deployment, script))
    console.print(f"[green]deployed[/green] {deployment.image}")


@app.command(help="Publish the sync API on the tailnet over HTTPS with a real certificate.")
def expose(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    if deployment.published_port not in TAILSCALE_HTTPS_PORTS:
        console.print(f"[red]tailscale serve accepts only {TAILSCALE_HTTPS_PORTS}[/red]")
        raise typer.Exit(1)
    run_remote(deployment, f"sudo tailscale serve --bg --https={deployment.published_port} {CONTAINER_PORT}")
    console.print(run_remote(deployment, "tailscale serve status"))


@app.command(help="Withdraw the tailnet listener without touching the container.")
def unexpose(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    run_remote(deployment, f"sudo tailscale serve --https={deployment.published_port} off")
    console.print(run_remote(deployment, "tailscale serve status"))


@app.command(help="Show container state, tailnet routing and API health.")
def status(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    container = run_remote(deployment, f'docker ps -a --filter name={CONTAINER_NAME} --format "{{{{.Status}}}}"').strip()
    serve = run_remote(deployment, "tailscale serve status").strip()
    table = Table(show_header=False, box=None)
    table.add_row("container", container or "absent")
    table.add_row("image", deployment.image)
    table.add_row("public url", deployment.public_url)
    table.add_row("health", probe_health(deployment))
    console.print(Panel(table, title="status", border_style="cyan"))
    console.print(Panel(serve or "no serve configuration", title="tailscale serve"))


def probe_health(deployment: Deployment) -> str:
    try:
        response = httpx.get(f"{deployment.public_url}/health", timeout=10.0)
        return f"{response.status_code} {response.text.strip()}"
    except httpx.HTTPError as error:
        return f"unreachable ({type(error).__name__})"


@app.command(help="Follow the sync server log.")
def logs(ctx: typer.Context, tail: Annotated[int, typer.Option(help="Number of trailing lines.")] = 100, follow: Annotated[bool, typer.Option(help="Stream new output.")] = False) -> None:
    deployment: Deployment = ctx.obj
    flag = "--follow" if follow else ""
    raise typer.Exit(stream_remote(deployment, f"docker logs --tail {tail} {flag} {CONTAINER_NAME}"))


@app.command(help="Restart the sync server, picking up env file changes.")
def restart(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    run_remote(deployment, f"cd {deployment.base_dir} && docker compose up -d --force-recreate")
    console.print("[green]restarted[/green]")


@app.command(help="Repin the image tag, pull it and recreate the container.")
def update(ctx: typer.Context, tag: Annotated[str, typer.Argument(help="Dated image tag published by the upstream build, such as 20260829-39b4311.")]) -> None:
    deployment: Deployment = ctx.obj
    repository = deployment.image.split(":")[0]
    script = f"""set -euo pipefail
cd {deployment.base_dir}
sed -i "s|image: .*|image: {repository}:{tag}|" docker-compose.yml
docker compose pull
docker compose up -d
grep 'image:' docker-compose.yml
"""
    console.print(run_remote(deployment, script))


@app.command(help="List every graph registered on the server with its encryption flag.")
def graphs(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    rows = query_sqlite(
        deployment,
        deployment.index_database,
        "select graph_id, graph_name, graph_e2ee, datetime(created_at/1000,'unixepoch') as created from graphs order by created",
    )
    table = Table(title="registered graphs")
    table.add_column("graph id")
    table.add_column("name")
    table.add_column("encrypted")
    table.add_column("created")
    for row in rows:
        table.add_row(row["graph_id"], row["graph_name"], "yes" if row["graph_e2ee"] else "no", row["created"])
    console.print(table)


@app.command(help="Report stored transaction count, sync checksum and on-disk size for one graph.")
def graph(ctx: typer.Context, graph_id: Annotated[str, typer.Argument(help="Graph uuid as listed by the graphs command.")]) -> None:
    deployment: Deployment = ctx.obj
    database = f"graphs/{graph_id}/db.sqlite"
    counts = query_sqlite(deployment, database, "select (select count(*) from kvs) as kvs, (select count(*) from tx_log) as transactions")
    meta = query_sqlite(deployment, database, "select key, value from sync_meta")
    size = run_remote(deployment, f"du -h {deployment.graphs_dir}/{graph_id}/db.sqlite | cut -f1").strip()
    table = Table(show_header=False, box=None)
    table.add_row("size", size)
    for key, value in (counts[0] if counts else {}).items():
        table.add_row(key, str(value))
    for row in meta:
        table.add_row(row["key"], str(row["value"]))
    console.print(Panel(table, title=graph_id, border_style="cyan"))


@backup_app.command("install", help="Install and enable the systemd service and timer backing up graph data only.")
def backup_install(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    unit = deployment.backup_unit
    script = f"""set -euo pipefail
sudo tee /etc/systemd/system/{unit}.service >/dev/null <<'SERVICEEOF'
{backup_service_unit(deployment)}SERVICEEOF
sudo tee /etc/systemd/system/{unit}.timer >/dev/null <<'TIMEREOF'
{backup_timer_unit()}TIMEREOF
sudo systemctl daemon-reload
{restic_environment(deployment)}
restic snapshots >/dev/null 2>&1 || restic init
sudo systemctl enable --now {unit}.timer
systemctl list-timers {unit}.timer --no-pager
"""
    console.print(run_remote(deployment, script))


@backup_app.command("run", help="Trigger a backup immediately instead of waiting for the timer.")
def backup_run(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    unit = deployment.backup_unit
    console.print(run_remote(deployment, f"sudo systemctl start {unit}.service && systemctl show -p Result --value {unit}.service"))


@backup_app.command("snapshots", help="List snapshots held in the Logseq restic repository.")
def backup_snapshots(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    console.print(run_remote(deployment, f"{restic_environment(deployment)}\nrestic snapshots"))


@app.command(help="Verify every moving part end to end and summarise what passed.")
def doctor(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    container = run_remote(deployment, f'docker ps --filter name={CONTAINER_NAME} --format "{{{{.Status}}}}"', check=False).strip()
    serve = run_remote(deployment, "tailscale serve status", check=False)
    timer = run_remote(deployment, f"systemctl is-enabled {deployment.backup_unit}.timer", check=False).strip()
    health = probe_health(deployment)
    unauthenticated = httpx_status(f"{deployment.public_url}/graphs")
    checks = [
        ("container running", "Up" in container, container or "absent"),
        ("tailnet listener", str(deployment.published_port) in serve, deployment.public_url),
        ("health endpoint", health.startswith("200"), health),
        ("authentication enforced", unauthenticated == 401, f"/graphs returned {unauthenticated}"),
        ("backup timer enabled", timer == "enabled", timer or "absent"),
    ]
    table = Table(title="doctor")
    table.add_column("check")
    table.add_column("result")
    table.add_column("detail")
    for name, passed, detail in checks:
        table.add_row(name, "[green]pass[/green]" if passed else "[red]fail[/red]", detail)
    console.print(table)
    raise typer.Exit(0 if all(passed for _, passed, _ in checks) else 1)


def httpx_status(url: str) -> int:
    try:
        return httpx.get(url, timeout=10.0).status_code
    except httpx.HTTPError:
        return 0


if __name__ == "__main__":
    app()
