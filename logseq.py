#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer>=0.12", "rich>=13.7", "httpx>=0.27"]
# ///

from __future__ import annotations

import os
import secrets
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

SYNC_IMAGE = "ghcr.io/yshalsager/logseq-selfhost-sync:20260829-39b4311"
CONTAINER_NAME = "logseq-selfhost-sync"
CONTAINER_PORT = 8787
TAILSCALE_HTTPS_PORTS = (443, 8443, 10000)
BACKUP_UNIT = "restic-backup-logseq"

LOGSEQ_COGNITO_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_dtagLnju8"
LOGSEQ_COGNITO_CLIENT_ID = "69cs1lgme7p8kbgld8n5kseii6"
LOGSEQ_COGNITO_JWKS_URL = f"{LOGSEQ_COGNITO_ISSUER}/.well-known/jwks.json"

console = Console()


@dataclass(frozen=True)
class Deployment:
    base_dir: Path
    tailnet_hostname: str
    image: str
    run_as: str
    published_port: int
    restic_repository: str
    restic_password_file: str
    rclone_config: str

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def graphs_dir(self) -> Path:
        return self.data_dir / "graphs"

    @property
    def index_database(self) -> Path:
        return self.data_dir / "index.sqlite"

    @property
    def environment_file(self) -> Path:
        return self.base_dir / ".env"

    @property
    def compose_file(self) -> Path:
        return self.base_dir / "docker-compose.yml"

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{CONTAINER_PORT}"

    @property
    def public_url(self) -> str:
        return f"https://{self.tailnet_hostname}:{self.published_port}"


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Operate a self-hosted Logseq DB sync server, run directly on the machine hosting it.",
)
backup_app = typer.Typer(no_args_is_help=True, help="Graph-data backups into a dedicated restic repository.")
app.add_typer(backup_app, name="backup")


@app.callback()
def configure(
    ctx: typer.Context,
    base_dir: Annotated[Path, typer.Option(envvar="LOGSEQ_BASE_DIR", help="Directory holding the compose file, env file and graph data.")],
    tailnet_hostname: Annotated[str, typer.Option(envvar="LOGSEQ_TAILNET_HOSTNAME", help="MagicDNS name clients connect to.")],
    image: Annotated[str, typer.Option(envvar="LOGSEQ_IMAGE", help="Pinned sync server image; upstream tracks master, so never ride latest.")] = SYNC_IMAGE,
    run_as: Annotated[str, typer.Option(envvar="LOGSEQ_RUN_AS", help="uid:gid the container runs as; defaults to the invoking user so the bind mount stays readable.")] = f"{os.getuid()}:{os.getgid()}",
    published_port: Annotated[int, typer.Option(envvar="LOGSEQ_PUBLISHED_PORT", help="Tailscale HTTPS port serving the sync API.")] = 10000,
    restic_repository: Annotated[str, typer.Option(envvar="LOGSEQ_RESTIC_REPOSITORY", help="Restic repository dedicated to Logseq graph data.")] = "rclone:remote:backups/logseq",
    restic_password_file: Annotated[str, typer.Option(envvar="LOGSEQ_RESTIC_PASSWORD_FILE", help="Path to the restic password file.")] = "/etc/restic/password",
    rclone_config: Annotated[str, typer.Option(envvar="LOGSEQ_RCLONE_CONFIG", help="Path to rclone.conf used by the backup unit.")] = str(Path.home() / ".config/rclone/rclone.conf"),
) -> None:
    ctx.obj = Deployment(
        base_dir=base_dir,
        tailnet_hostname=tailnet_hostname,
        image=image,
        run_as=run_as,
        published_port=published_port,
        restic_repository=restic_repository,
        restic_password_file=restic_password_file,
        rclone_config=rclone_config,
    )


def run(command: list[str], cwd: Path | None = None, check: bool = True, capture: bool = True) -> str:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=capture)
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip() if capture else ""
        console.print(Panel(detail or " ".join(command), title="command failed", border_style="red"))
        raise typer.Exit(completed.returncode)
    return completed.stdout if capture else ""


def write_privileged_file(path: str, content: str) -> None:
    subprocess.run(["sudo", "tee", path], input=content, text=True, stdout=subprocess.DEVNULL, check=True)


def query(database: Path, sql: str) -> list[dict]:
    if not database.exists():
        console.print(f"[red]missing database[/red] {database}")
        raise typer.Exit(1)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql)]


def human_size(path: Path) -> str:
    size = float(path.stat().st_size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def environment_contents(deployment: Deployment, admin_token: str) -> str:
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


def compose_contents(deployment: Deployment) -> str:
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


def backup_service_contents(deployment: Deployment) -> str:
    return f"""[Unit]
Description=Restic backup of Logseq graph data
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User={os.getlogin()}
Environment="RESTIC_PASSWORD_FILE={deployment.restic_password_file}"
Environment="RESTIC_REPOSITORY={deployment.restic_repository}"
Environment="RCLONE_CONFIG={deployment.rclone_config}"
ExecStart=restic backup {deployment.data_dir}
ExecStartPost=restic forget --prune --keep-hourly 24 --keep-daily 30 --keep-monthly 12
"""


BACKUP_TIMER_CONTENTS = """[Unit]
Description=Hourly restic backup of Logseq graph data

[Timer]
OnCalendar=*:30
Persistent=true

[Install]
WantedBy=timers.target
"""


def restic_environment(deployment: Deployment) -> dict[str, str]:
    return os.environ | {
        "RESTIC_PASSWORD_FILE": deployment.restic_password_file,
        "RESTIC_REPOSITORY": deployment.restic_repository,
        "RCLONE_CONFIG": deployment.rclone_config,
    }


@app.command(help="Print resolved settings and optionally the files that deploy would write.")
def config(ctx: typer.Context, show_files: Annotated[bool, typer.Option(help="Also render the compose file and systemd units.")] = False) -> None:
    deployment: Deployment = ctx.obj
    table = Table(show_header=False, box=None)
    for field, value in vars(deployment).items():
        table.add_row(field.replace("_", " "), str(value))
    table.add_row("public url", deployment.public_url)
    console.print(Panel(table, title="deployment", border_style="cyan"))
    if show_files:
        console.print(Panel(Syntax(compose_contents(deployment), "yaml"), title=str(deployment.compose_file)))
        console.print(Panel(Syntax(backup_service_contents(deployment), "ini"), title=f"{BACKUP_UNIT}.service"))
        console.print(Panel(Syntax(BACKUP_TIMER_CONTENTS, "ini"), title=f"{BACKUP_UNIT}.timer"))


@app.command(help="Create the data directory, write the compose and env files, then start the sync server.")
def deploy(ctx: typer.Context, regenerate_admin_token: Annotated[bool, typer.Option(help="Overwrite an existing env file with a freshly generated admin token.")] = False) -> None:
    deployment: Deployment = ctx.obj
    deployment.data_dir.mkdir(parents=True, exist_ok=True)
    if deployment.environment_file.exists() and not regenerate_admin_token:
        console.print("kept existing env file")
    else:
        deployment.environment_file.write_text(environment_contents(deployment, secrets.token_hex(32)))
        deployment.environment_file.chmod(0o600)
        console.print("wrote env file with a generated admin token")
    deployment.compose_file.write_text(compose_contents(deployment))
    run(["docker", "compose", "pull"], cwd=deployment.base_dir, capture=False)
    run(["docker", "compose", "up", "-d"], cwd=deployment.base_dir, capture=False)
    console.print(f"[green]deployed[/green] {deployment.image}")


@app.command(help="Publish the sync API on the tailnet over HTTPS with a real certificate.")
def expose(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    if deployment.published_port not in TAILSCALE_HTTPS_PORTS:
        console.print(f"[red]tailscale serve accepts only {TAILSCALE_HTTPS_PORTS}[/red]")
        raise typer.Exit(1)
    run(["sudo", "tailscale", "serve", "--bg", f"--https={deployment.published_port}", str(CONTAINER_PORT)])
    console.print(run(["tailscale", "serve", "status"]))


@app.command(help="Withdraw the tailnet listener without stopping the container.")
def unexpose(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    run(["sudo", "tailscale", "serve", f"--https={deployment.published_port}", "off"])
    console.print(run(["tailscale", "serve", "status"]))


def probe(url: str) -> tuple[int, str]:
    try:
        response = httpx.get(url, timeout=10.0)
        return response.status_code, response.text.strip()
    except httpx.HTTPError as error:
        return 0, f"unreachable ({type(error).__name__})"


@app.command(help="Show container state, tailnet routing and API health.")
def status(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    container = run(["docker", "ps", "-a", "--filter", f"name={CONTAINER_NAME}", "--format", "{{.Status}}"], check=False).strip()
    code, body = probe(f"{deployment.local_url}/health")
    table = Table(show_header=False, box=None)
    table.add_row("container", container or "absent")
    table.add_row("image", deployment.image)
    table.add_row("data", str(deployment.data_dir))
    table.add_row("public url", deployment.public_url)
    table.add_row("health", f"{code} {body}")
    console.print(Panel(table, title="status", border_style="cyan"))
    console.print(Panel(run(["tailscale", "serve", "status"], check=False).strip() or "no serve configuration", title="tailscale serve"))


@app.command(help="Show the sync server log.")
def logs(ctx: typer.Context, tail: Annotated[int, typer.Option(help="Number of trailing lines.")] = 100, follow: Annotated[bool, typer.Option(help="Stream new output until interrupted.")] = False) -> None:
    command = ["docker", "logs", "--tail", str(tail), CONTAINER_NAME]
    if follow:
        command.insert(2, "--follow")
    run(command, check=False, capture=False)


@app.command(help="Recreate the container, picking up env file changes.")
def restart(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    run(["docker", "compose", "up", "-d", "--force-recreate"], cwd=deployment.base_dir, capture=False)
    console.print("[green]restarted[/green]")


@app.command(help="Repin the image tag, pull it and recreate the container.")
def update(ctx: typer.Context, tag: Annotated[str, typer.Argument(help="Dated tag published by the upstream build, such as 20260829-39b4311.")]) -> None:
    deployment: Deployment = ctx.obj
    repository = deployment.image.split(":")[0]
    updated = "\n".join(
        f"    image: {repository}:{tag}" if line.strip().startswith("image:") else line
        for line in deployment.compose_file.read_text().splitlines()
    )
    deployment.compose_file.write_text(updated + "\n")
    run(["docker", "compose", "pull"], cwd=deployment.base_dir, capture=False)
    run(["docker", "compose", "up", "-d"], cwd=deployment.base_dir, capture=False)
    console.print(f"[green]now running[/green] {repository}:{tag}")


@app.command(help="List every graph registered on the server with its encryption flag.")
def graphs(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    rows = query(
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


@app.command(help="Report stored transactions, sync checksum and on-disk size for one graph.")
def graph(ctx: typer.Context, graph_id: Annotated[str, typer.Argument(help="Graph uuid as listed by the graphs command.")]) -> None:
    deployment: Deployment = ctx.obj
    database = deployment.graphs_dir / graph_id / "db.sqlite"
    counts = query(database, "select (select count(*) from kvs) as kvs, (select count(*) from tx_log) as transactions")
    table = Table(show_header=False, box=None)
    table.add_row("size", human_size(database))
    for key, value in counts[0].items():
        table.add_row(key, str(value))
    for row in query(database, "select key, value from sync_meta"):
        table.add_row(row["key"], str(row["value"]))
    console.print(Panel(table, title=graph_id, border_style="cyan"))


@backup_app.command("install", help="Install and enable the systemd service and timer backing up graph data only.")
def backup_install(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    write_privileged_file(f"/etc/systemd/system/{BACKUP_UNIT}.service", backup_service_contents(deployment))
    write_privileged_file(f"/etc/systemd/system/{BACKUP_UNIT}.timer", BACKUP_TIMER_CONTENTS)
    run(["sudo", "systemctl", "daemon-reload"])
    if subprocess.run(["restic", "snapshots"], env=restic_environment(deployment), capture_output=True).returncode != 0:
        run(["restic", "init"], capture=False)
    run(["sudo", "systemctl", "enable", "--now", f"{BACKUP_UNIT}.timer"])
    console.print(run(["systemctl", "list-timers", f"{BACKUP_UNIT}.timer", "--no-pager"]))


@backup_app.command("run", help="Trigger a backup immediately instead of waiting for the timer.")
def backup_run(ctx: typer.Context) -> None:
    run(["sudo", "systemctl", "start", f"{BACKUP_UNIT}.service"])
    console.print(run(["systemctl", "show", "-p", "Result", "--value", f"{BACKUP_UNIT}.service"]).strip())


@backup_app.command("snapshots", help="List snapshots held in the Logseq restic repository.")
def backup_snapshots(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    subprocess.run(["restic", "snapshots"], env=restic_environment(deployment))


@app.command(help="Verify every moving part and summarise what passed.")
def doctor(ctx: typer.Context) -> None:
    deployment: Deployment = ctx.obj
    container = run(["docker", "ps", "--filter", f"name={CONTAINER_NAME}", "--format", "{{.Status}}"], check=False).strip()
    serve = run(["tailscale", "serve", "status"], check=False)
    timer = run(["systemctl", "is-enabled", f"{BACKUP_UNIT}.timer"], check=False).strip()
    local_code, local_body = probe(f"{deployment.local_url}/health")
    public_code, _ = probe(f"{deployment.public_url}/health")
    guarded_code, _ = probe(f"{deployment.local_url}/graphs")
    checks = [
        ("container running", "Up" in container, container or "absent"),
        ("data directory", deployment.data_dir.is_dir(), str(deployment.data_dir)),
        ("health endpoint", local_code == 200, f"{local_code} {local_body}"),
        ("authentication enforced", guarded_code == 401, f"/graphs returned {guarded_code}"),
        ("tailnet listener", str(deployment.published_port) in serve, deployment.public_url),
        ("reachable over tailnet", public_code == 200, f"{public_code}"),
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


if __name__ == "__main__":
    app()
