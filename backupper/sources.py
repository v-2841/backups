from __future__ import annotations

from pathlib import Path
import shlex
import sqlite3

from backupper.errors import BackupError
from backupper.manifest import write_manifest
from backupper.models import BackupConfig, RemoteSpec
from backupper.remote import (
    copy_remote_path,
    remote_path_fingerprint,
    stream_remote_stdout_to_file,
)
from backupper.reuse import copy_reused_path, find_reusable_path_source
from backupper.utils import now_iso

SQLITE_SUFFIXES = ('.sqlite', '.sqlite3', '.db')


def copy_path_source(
    config: BackupConfig,
    raw_source: str,
    snapshot_dir: Path,
    manifest: dict,
) -> None:
    spec = RemoteSpec.parse(raw_source)
    entry = {
        'type': 'path',
        'source': raw_source,
        'started_at': now_iso(),
        'status': 'running',
    }
    manifest['items'].append(entry)
    write_manifest(snapshot_dir, manifest)

    print(f'==> Checking {raw_source}')
    try:
        files_root = snapshot_dir / spec.host_dir / 'files'
        copied_path = files_root / spec.relative_path
        fingerprint_info = remote_path_fingerprint(config, spec)
        fingerprint = fingerprint_info['fingerprint']
        reusable = find_reusable_path_source(
            config.backup_root,
            raw_source,
            fingerprint,
        )

        if reusable:
            reusable_path, reusable_item = reusable
            copy_reused_path(reusable_path, copied_path)
            entry['reused_from'] = str(reusable_path)
            entry['reused_from_source'] = reusable_item.get('source')
            print(f'==> Reused unchanged {raw_source}')
        else:
            print(f'==> Copying {raw_source}')
            copied_path = copy_remote_path(config, spec, files_root)

        sqlite_check = maybe_check_sqlite(copied_path)
        entry.update(
            {
                'status': 'ok',
                'finished_at': now_iso(),
                'destination': str(copied_path.relative_to(snapshot_dir)),
                'size_bytes': fingerprint_info['size_bytes'],
                'fingerprint': fingerprint,
                'fingerprint_method': fingerprint_info['method'],
                'source_kind': fingerprint_info['kind'],
                'file_count': fingerprint_info['file_count'],
                'dir_count': fingerprint_info['dir_count'],
                'symlink_count': fingerprint_info['symlink_count'],
            }
        )
        if sqlite_check:
            entry['sqlite_integrity_check'] = sqlite_check
    except Exception as error:
        entry.update({
            'status': 'failed',
            'finished_at': now_iso(),
            'error': str(error),
        })
        write_manifest(snapshot_dir, manifest)
        raise

    write_manifest(snapshot_dir, manifest)


def copy_sqlite_source(
    config: BackupConfig,
    raw_source: str,
    snapshot_dir: Path,
    manifest: dict,
) -> None:
    spec = RemoteSpec.parse(raw_source)
    entry = {
        'type': 'sqlite',
        'source': raw_source,
        'started_at': now_iso(),
        'status': 'running',
    }
    manifest['items'].append(entry)
    write_manifest(snapshot_dir, manifest)

    print(f'==> Backing up SQLite {raw_source}')
    try:
        sqlite_root = snapshot_dir / spec.host_dir / 'sqlite'
        copied_path = copy_remote_sqlite(config, spec, sqlite_root)
        sqlite_check = maybe_check_sqlite(copied_path)
        entry.update(
            {
                'status': 'ok',
                'finished_at': now_iso(),
                'destination': str(copied_path.relative_to(snapshot_dir)),
                'size_bytes': copied_path.stat().st_size,
                'sqlite_integrity_check': sqlite_check,
            }
        )
    except Exception as error:
        entry.update({
            'status': 'failed',
            'finished_at': now_iso(),
            'error': str(error),
        })
        write_manifest(snapshot_dir, manifest)
        raise

    write_manifest(snapshot_dir, manifest)


def copy_remote_sqlite(
    config: BackupConfig,
    spec: RemoteSpec,
    destination_root: Path,
) -> Path:
    if not spec.relative_path:
        raise BackupError(
            'Refusing to copy remote root path: '
            f'{spec.ssh_target}:{spec.path}'
        )

    destination = destination_root / spec.relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)

    remote_script = f'''
set -euo pipefail
SRC_PATH={shlex.quote(spec.path)}
test -f "$SRC_PATH"
TMP_PATH="$(mktemp /tmp/backups_sqlite_XXXXXX.sqlite3)"
trap 'rm -f "$TMP_PATH"' EXIT
python3 - "$SRC_PATH" "$TMP_PATH" <<'PY'
import os
import sqlite3
import sys

src_path, dst_path = sys.argv[1], sys.argv[2]

if os.path.exists(dst_path):
    os.remove(dst_path)

src = sqlite3.connect(f'file:{{src_path}}?mode=ro', uri=True)
dst = sqlite3.connect(dst_path)
try:
    with dst:
        src.backup(dst)
finally:
    dst.close()
    src.close()

check = sqlite3.connect(dst_path)
try:
    result = check.execute('PRAGMA integrity_check').fetchone()[0]
finally:
    check.close()

if result != 'ok':
    raise SystemExit(f'SQLite integrity_check failed: {{result}}')
PY
test -s "$TMP_PATH"
cat "$TMP_PATH"
'''.strip()

    stream_remote_stdout_to_file(
        config,
        spec.ssh_target,
        remote_script,
        destination,
    )
    return destination


def maybe_check_sqlite(path: Path) -> str | None:
    if not path.is_file():
        return None
    if path.name.endswith(('-journal', '-wal', '-shm')):
        return None
    if path.suffix.lower() not in SQLITE_SUFFIXES:
        return None

    uri_path = path.resolve().as_posix()
    connection = sqlite3.connect(
        f'file:{uri_path}?mode=ro&immutable=1',
        uri=True,
    )
    try:
        result = connection.execute('PRAGMA integrity_check').fetchone()[0]
    finally:
        connection.close()

    if result != 'ok':
        raise BackupError(
            f'SQLite integrity_check failed for {path}: {result}'
        )
    return result
