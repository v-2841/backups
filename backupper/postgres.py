from __future__ import annotations

from pathlib import Path
import json
import shlex
import shutil
import subprocess

from backupper.errors import BackupError
from backupper.manifest import write_manifest
from backupper.models import BackupConfig, RemoteSpec
from backupper.remote import (
    remote_bash_command,
    run_capture,
    stream_remote_stdout_to_file,
)
from backupper.utils import now_iso, sanitize_path_part


def copy_postgres_project(
    config: BackupConfig,
    raw_project: str,
    snapshot_dir: Path,
    manifest: dict,
) -> None:
    spec = RemoteSpec.parse(raw_project)
    project_rel = spec.relative_path
    entry = {
        'type': 'postgres_project',
        'source': raw_project,
        'started_at': now_iso(),
        'status': 'running',
        'services': [],
    }
    manifest['items'].append(entry)
    write_manifest(snapshot_dir, manifest)

    print(f'==> Dumping Postgres project {raw_project}')
    try:
        services = find_postgres_services(config, spec)
        if not services:
            raise BackupError(
                'No running postgres service found in compose project: '
                f'{raw_project}'
            )

        for service in services:
            service_dir = (
                snapshot_dir
                / spec.host_dir
                / 'postgres'
                / project_rel
                / sanitize_path_part(service)
            )
            service_entry = {
                'service': service,
                'destination': str(service_dir.relative_to(snapshot_dir)),
                'databases': [],
            }
            entry['services'].append(service_entry)

            globals_path = service_dir / 'globals.sql'
            dump_postgres_globals(
                config,
                spec,
                service,
                globals_path,
            )
            service_entry['globals'] = {
                'path': str(globals_path.relative_to(snapshot_dir)),
                'size_bytes': globals_path.stat().st_size,
            }

            for database in postgres_databases(config, spec, service):
                dump_path = (
                    service_dir / f'{sanitize_path_part(database)}.dump'
                )
                dump_postgres_database(
                    config,
                    spec,
                    service,
                    database,
                    dump_path,
                )
                dump_check = validate_pg_dump(config, dump_path)
                service_entry['databases'].append(
                    {
                        'name': database,
                        'path': str(dump_path.relative_to(snapshot_dir)),
                        'size_bytes': dump_path.stat().st_size,
                        'dump_check': dump_check,
                    }
                )

        entry.update({'status': 'ok', 'finished_at': now_iso()})
    except Exception as error:
        entry.update({
            'status': 'failed',
            'finished_at': now_iso(),
            'error': str(error),
        })
        write_manifest(snapshot_dir, manifest)
        raise

    write_manifest(snapshot_dir, manifest)


def find_postgres_services(
    config: BackupConfig,
    spec: RemoteSpec,
) -> list[str]:
    script = f'''
set -euo pipefail
cd {shlex.quote(spec.path)}
docker compose ps --format json
'''.strip()
    command = remote_bash_command(config, spec.ssh_target, script)
    output = run_capture(config, command)
    services = []

    for row in parse_compose_ps_json(output):
        service = row.get('Service') or row.get('Name')
        image = row.get('Image') or ''
        if service and 'postgres' in str(image).lower():
            services.append(str(service))

    return sorted(set(services))


def postgres_databases(
    config: BackupConfig,
    spec: RemoteSpec,
    service: str,
) -> list[str]:
    query = (
        'select datname from pg_database '
        'where datallowconn and not datistemplate '
        'order by datname;'
    )
    inner = f'''
PGUSER="${{POSTGRES_USER:-postgres}}"
export PGPASSWORD="${{POSTGRES_PASSWORD:-}}"
psql -U "$PGUSER" -d postgres -At -c {shlex.quote(query)}
'''.strip()
    script = f'''
set -euo pipefail
cd {shlex.quote(spec.path)}
docker compose exec -T {shlex.quote(service)} sh -lc {shlex.quote(inner)}
'''.strip()
    command = remote_bash_command(config, spec.ssh_target, script)
    output = run_capture(config, command)
    return [line.strip() for line in output.splitlines() if line.strip()]


def dump_postgres_globals(
    config: BackupConfig,
    spec: RemoteSpec,
    service: str,
    destination: Path,
) -> None:
    inner = '''
PGUSER="${POSTGRES_USER:-postgres}"
export PGPASSWORD="${POSTGRES_PASSWORD:-}"
pg_dumpall -U "$PGUSER" --globals-only
'''.strip()
    script = f'''
set -euo pipefail
cd {shlex.quote(spec.path)}
docker compose exec -T {shlex.quote(service)} sh -lc {shlex.quote(inner)}
'''.strip()
    stream_remote_stdout_to_file(config, spec.ssh_target, script, destination)


def dump_postgres_database(
    config: BackupConfig,
    spec: RemoteSpec,
    service: str,
    database: str,
    destination: Path,
) -> None:
    inner = f'''
PGUSER="${{POSTGRES_USER:-postgres}}"
export PGPASSWORD="${{POSTGRES_PASSWORD:-}}"
DB_NAME={shlex.quote(database)}
pg_dump -U "$PGUSER" -d "$DB_NAME" -Fc
'''.strip()
    script = f'''
set -euo pipefail
cd {shlex.quote(spec.path)}
docker compose exec -T {shlex.quote(service)} sh -lc {shlex.quote(inner)}
'''.strip()
    stream_remote_stdout_to_file(config, spec.ssh_target, script, destination)


def validate_pg_dump(config: BackupConfig, path: Path) -> str:
    '''Read the TOC of a custom-format dump to confirm it is not corrupt.'''
    pg_restore = shutil.which('pg_restore')
    if pg_restore is None:
        return 'skipped: pg_restore not found'

    try:
        result = subprocess.run(
            [pg_restore, '-l', str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=config.command_timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise BackupError(
            f'pg_restore -l timed out after '
            f'{config.command_timeout_seconds}s for {path}'
        ) from error

    if result.returncode != 0:
        raise BackupError(
            f'pg_restore -l failed for {path} ({result.returncode}):\n'
            f'{result.stderr}'
        )
    return 'ok'


def parse_compose_ps_json(output: str) -> list[dict]:
    text = output.strip()
    if not text:
        return []

    if text.startswith('['):
        data = json.loads(text)
        if not isinstance(data, list):
            raise BackupError(
                'docker compose ps --format json did not return a list'
            )
        return [row for row in data if isinstance(row, dict)]

    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows
