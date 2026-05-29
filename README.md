# backups

Small VPS backup orchestrator.

## Configure

Copy the example config and edit local values:

```bash
cp config.example.toml config.toml
```

`config.toml` is ignored by git.

Config format:

```toml
backup_root = 'backups'
keep_backups = 5
keep_partial_days = 3
ssh_connect_timeout = 10
ssh_server_alive_interval = 30
ssh_server_alive_count_max = 3
command_timeout_seconds = 7200

[path_sources]
items = [
  'user@example-vps:/home/user/app/.env',
  'user@example-vps:/home/user/app/media',
]

[sqlite_sources]
items = [
  'user@example-vps:/home/user/app/db.sqlite3',
]

[postgres_projects]
items = [
  'user@example-vps:/home/user/app',
]
```

All remote sources must include the SSH user as `user@host:/full/path`.
`command_timeout_seconds` is a per-command ceiling; it does not limit normal transfers unless they exceed that duration.
SSH runs in batch mode with connect/server-alive settings from the config, so systemd runs fail instead of waiting for interactive input.

## Run

```bash
python3 backup.py --config config.toml
```

Snapshots are written to `./backups/backup_YYYY-MM-DD_HH-MM-SS`.
If one source fails, the script still tries the remaining sources and records every item in `manifest.json`. A run that finishes is promoted to a final snapshot regardless: `manifest.status` is `ok` when every source succeeded, or `completed_with_errors` when some failed (the process still exits with code `1` so systemd flags the failure).
A `*.partial` directory only remains when the run is interrupted before it finishes (crash, kill, timeout); these are pruned using `keep_partial_days`.
SQLite copies are verified with `PRAGMA integrity_check`; Postgres custom-format dumps are verified locally with `pg_restore -l`.

## Install systemd timer

Preview generated units:

```bash
DRY_RUN=1 ./install_systemd.sh
```

Install and enable the daily timer:

```bash
./install_systemd.sh
```
