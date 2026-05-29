from __future__ import annotations

from pathlib import Path
from typing import Callable
import shutil
import sys
import time

from backupper.manifest import write_manifest
from backupper.models import BackupConfig
from backupper.postgres import copy_postgres_project
from backupper.sources import copy_path_source, copy_sqlite_source
from backupper.utils import now_iso, timestamp_name

BackupHandler = Callable[[BackupConfig, str, Path, dict], None]


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

    manifest = create_manifest(config, snapshot_name, partial_dir, final_dir)
    manifest['pruned_partials'] = pruned_partials
    write_manifest(partial_dir, manifest)

    for source in config.path_sources:
        run_item(
            config,
            'path',
            source,
            copy_path_source,
            partial_dir,
            manifest,
        )

    for source in config.sqlite_sources:
        run_item(
            config,
            'sqlite',
            source,
            copy_sqlite_source,
            partial_dir,
            manifest,
        )

    for project in config.postgres_projects:
        run_item(
            config,
            'postgres_project',
            project,
            copy_postgres_project,
            partial_dir,
            manifest,
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

        removed = rotate_old_backups(
            config.backup_root,
            config.keep_backups,
        )
        if removed:
            manifest['rotated'] = removed
            write_manifest(final_dir, manifest)

    except Exception as error:
        manifest['status'] = 'failed'
        manifest['finished_at'] = now_iso()
        manifest['errors'].append(str(error))
        write_manifest(partial_dir, manifest)
        print(
            f'Backup failed, partial snapshot kept at: {partial_dir}',
            file=sys.stderr,
        )
        print(str(error), file=sys.stderr)
        return 1

    if has_errors:
        print(f'Backup finished with errors: {final_dir}', file=sys.stderr)
        return 1

    print(f'Backup finished: {final_dir}')
    return 0


def run_item(
    config: BackupConfig,
    kind: str,
    source: str,
    handler: BackupHandler,
    partial_dir: Path,
    manifest: dict,
) -> None:
    try:
        handler(config, source, partial_dir, manifest)
    except Exception as error:
        error_text = f'{kind} {source}: {error}'
        manifest['errors'].append(error_text)
        write_manifest(partial_dir, manifest)
        print(f'ERROR: {error_text}', file=sys.stderr)


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
        'keep_backups': config.keep_backups,
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


def rotate_old_backups(root: Path, keep: int) -> list[str]:
    backups = sorted(
        [
            item
            for item in root.iterdir()
            if (
                item.is_dir()
                and item.name.startswith('backup_')
                and not item.name.endswith('.partial')
            )
        ],
        key=lambda item: item.name,
        reverse=True,
    )
    removed = []
    for old_backup in backups[keep:]:
        shutil.rmtree(old_backup)
        removed.append(str(old_backup))
    return removed


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
