from __future__ import annotations

from pathlib import Path
import json
import os
import shutil


def find_reusable_path_source(
    backup_root: Path,
    raw_source: str,
    fingerprint: str,
) -> tuple[Path, dict] | None:
    for backup_dir in previous_backup_dirs(backup_root):
        manifest = read_manifest(backup_dir)
        if manifest is None:
            continue

        for item in manifest.get('items', []):
            if item.get('type') != 'path':
                continue
            if item.get('status') != 'ok':
                continue
            if item.get('source') != raw_source:
                continue
            if item.get('fingerprint') != fingerprint:
                continue
            if not item.get('destination'):
                continue

            source_path = backup_dir / item['destination']
            if source_path.exists() or source_path.is_symlink():
                return source_path, item

    return None


def copy_reused_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)

    if source.is_symlink():
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        os.symlink(os.readlink(source), destination)
        return

    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            symlinks=True,
            dirs_exist_ok=True,
        )
        return

    shutil.copy2(source, destination, follow_symlinks=False)


def previous_backup_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []

    return sorted(
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


def read_manifest(backup_dir: Path) -> dict | None:
    manifest_path = backup_dir / 'manifest.json'
    if not manifest_path.is_file():
        return None

    try:
        return json.loads(manifest_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None
