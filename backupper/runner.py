from __future__ import annotations

from pathlib import Path
from typing import Callable
import shutil
import sys
import time

from backupper.manifest import write_manifest
from backupper.models import BackupConfig, RemoteSpec
from backupper.postgres import copy_postgres_project
from backupper.progress import Progress
from backupper.reuse import previous_backup_dirs, read_manifest
from backupper.sources import copy_path_source, copy_sqlite_source
from backupper.utils import now_iso, timestamp_name

BackupHandler = Callable[[BackupConfig, str, Path, dict], None]

KIND_LABELS = {
    'path': 'path',
    'sqlite': 'sqlite',
    'postgres_project': 'postgres',
}


def run_backup(config: BackupConfig) -> int:
    config.backup_root.mkdir(parents=True, exist_ok=True)
    pruned_partials = prune_old_partial_backups(
        config.backup_root,
        config.keep_partial_days,
    )
    snapshot_name, partial_dir, final_dir = unique_snapshot_paths(
        config.backup_root,
    )
    partial_dir.mkdir(parents=True)

    work: list[tuple[str, str, BackupHandler]] = []
    for source in config.path_sources:
        work.append(('path', source, copy_path_source))
    for source in config.sqlite_sources:
        work.append(('sqlite', source, copy_sqlite_source))
    for project in config.postgres_projects:
        work.append(('postgres_project', project, copy_postgres_project))

    progress = Progress(
        [item_label(kind, source) for kind, source, _ in work],
    )
    progress.print_header(snapshot_name, final_dir)
    if pruned_partials:
        progress.note(
            f'Pruned {len(pruned_partials)} stale partial snapshot(s)'
        )

    manifest = create_manifest(config, snapshot_name, partial_dir, final_dir)
    manifest['pruned_partials'] = pruned_partials
    write_manifest(partial_dir, manifest)

    for kind, source, handler in work:
        run_item(
            config,
            kind,
            source,
            handler,
            partial_dir,
            manifest,
            progress,
        )

    # A run that reaches this point has finished. Promote it to a final
    # snapshot even if some sources failed; manifest['status'] records
    # whether everything succeeded. A *.partial directory only survives
    # when the process is interrupted before it gets here.
    has_errors = bool(manifest['errors'])
    manifest['status'] = (
        'completed_with_errors' if has_errors else 'ok'
    )
    manifest['finished_at'] = now_iso()

    try:
        write_manifest(partial_dir, manifest)

        partial_dir.rename(final_dir)
        manifest['final_dir'] = str(final_dir)
        write_manifest(final_dir, manifest)

        removed = prune_old_successful_backups(
            config.backup_root,
            config.keep_backups_days,
            config.keep_min_backups,
        )
        if removed:
            manifest['rotated'] = removed
            write_manifest(final_dir, manifest)

    except Exception as error:
        manifest['status'] = 'failed'
        manifest['finished_at'] = now_iso()
        manifest['errors'].append(str(error))
        write_manifest(partial_dir, manifest)
        progress.print_summary(partial_dir, 'failed')
        print(
            f'Backup failed, partial snapshot kept at: {partial_dir}',
            file=sys.stderr,
        )
        print(str(error), file=sys.stderr)
        return 1

    if removed:
        progress.note(f'Rotated out {len(removed)} old snapshot(s)')
    progress.print_summary(final_dir, manifest['status'])

    if has_errors:
        print(f'Backup finished with errors: {final_dir}', file=sys.stderr)
        return 1
    return 0


def run_item(
    config: BackupConfig,
    kind: str,
    source: str,
    handler: BackupHandler,
    partial_dir: Path,
    manifest: dict,
    progress: Progress,
) -> None:
    progress.start_item()
    started = time.monotonic()
    try:
        handler(config, source, partial_dir, manifest)
    except Exception as error:
        duration = time.monotonic() - started
        error_text = f'{kind} {source}: {error}'
        manifest['errors'].append(error_text)
        write_manifest(partial_dir, manifest)
        progress.finish_item(
            'FAILED',
            None,
            duration,
            failed=True,
            detail=short_error(error),
        )
        print(f'ERROR: {error_text}', file=sys.stderr)
    else:
        duration = time.monotonic() - started
        result, size_bytes, reused, detail = describe_entry(
            manifest['items'][-1],
        )
        progress.finish_item(
            result,
            size_bytes,
            duration,
            reused=reused,
            detail=detail,
        )


def item_label(kind: str, source: str) -> tuple[str, str, str]:
    kind_text = KIND_LABELS.get(kind, kind)
    try:
        spec = RemoteSpec.parse(source)
    except Exception:
        return kind_text, '', source

    path = spec.path
    home_prefix = f'/home/{spec.user}/'
    if path.startswith(home_prefix):
        path = path[len(home_prefix):]
    return kind_text, spec.host, path


def describe_entry(entry: dict) -> tuple[str, int | None, bool, str]:
    if entry.get('type') == 'path':
        reused = bool(entry.get('reused_from'))
        result = 'reused' if reused else 'copied'
        return result, entry.get('size_bytes'), reused, ''

    if entry.get('type') == 'sqlite':
        return 'dumped', entry.get('size_bytes'), False, ''

    databases = 0
    size_bytes = 0
    for service in entry.get('services', []):
        service_databases = service.get('databases', [])
        databases += len(service_databases)
        for database in service_databases:
            size_bytes += database.get('size_bytes', 0)
        size_bytes += service.get('globals', {}).get('size_bytes', 0)
    noun = 'db' if databases == 1 else 'dbs'
    return 'dumped', size_bytes, False, f'{databases} {noun} + globals'


def short_error(error: Exception) -> str:
    text = str(error).strip()
    if not text:
        return error.__class__.__name__
    return text.splitlines()[0]


def create_manifest(
    config: BackupConfig,
    snapshot_name: str,
    partial_dir: Path,
    final_dir: Path,
) -> dict:
    return {
        'status': 'running',
        'started_at': now_iso(),
        'finished_at': None,
        'backup_name': snapshot_name,
        'partial_dir': str(partial_dir),
        'final_dir': str(final_dir),
        'backup_root': str(config.backup_root),
        'config_path': str(config.config_path),
        'keep_backups_days': config.keep_backups_days,
        'keep_min_backups': config.keep_min_backups,
        'keep_partial_days': config.keep_partial_days,
        'command_timeout_seconds': config.command_timeout_seconds,
        'ssh': {
            'batch_mode': True,
            'connect_timeout': config.ssh.connect_timeout,
            'server_alive_interval': config.ssh.server_alive_interval,
            'server_alive_count_max': config.ssh.server_alive_count_max,
        },
        'items': [],
        'errors': [],
        'pruned_partials': [],
        'rotated': [],
    }


def unique_snapshot_paths(root: Path) -> tuple[str, Path, Path]:
    base_name = f'backup_{timestamp_name()}'
    for index in range(100):
        name = base_name if index == 0 else f'{base_name}_{index}'
        final_dir = root / name
        partial_dir = root / f'{name}.partial'
        if not final_dir.exists() and not partial_dir.exists():
            return name, partial_dir, final_dir
    raise RuntimeError(
        f'Could not allocate a unique backup directory under {root}'
    )


def prune_old_successful_backups(
    root: Path,
    keep_days: int,
    keep_min_ok: int,
) -> list[str]:
    cutoff = time.time() - (keep_days * 24 * 60 * 60)
    protected = newest_ok_backup_dirs(root, keep_min_ok)
    removed = []
    for backup_dir in sorted(root.glob('backup_*')):
        if not backup_dir.is_dir() or backup_dir.name.endswith('.partial'):
            continue
        if backup_dir in protected:
            continue
        if backup_dir.stat().st_mtime > cutoff:
            continue
        shutil.rmtree(backup_dir)
        removed.append(str(backup_dir))
    return removed


def newest_ok_backup_dirs(root: Path, count: int) -> set[Path]:
    '''The newest `count` snapshots whose manifest status is ok.

    These are never pruned by age, so a streak of failing runs cannot
    rotate out the last known-good backups.
    '''
    if count < 1:
        return set()

    ok_dirs: set[Path] = set()
    for backup_dir in previous_backup_dirs(root):
        manifest = read_manifest(backup_dir)
        if manifest is None or manifest.get('status') != 'ok':
            continue
        ok_dirs.add(backup_dir)
        if len(ok_dirs) == count:
            break
    return ok_dirs


def prune_old_partial_backups(root: Path, keep_days: int) -> list[str]:
    if keep_days < 0:
        return []

    cutoff = time.time() - (keep_days * 24 * 60 * 60)
    removed = []
    for partial_dir in sorted(root.glob('backup_*.partial')):
        if not partial_dir.is_dir():
            continue
        if partial_dir.stat().st_mtime > cutoff:
            continue
        shutil.rmtree(partial_dir)
        removed.append(str(partial_dir))
    return removed
