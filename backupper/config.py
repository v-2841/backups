from __future__ import annotations

from pathlib import Path
from typing import Any
import tomllib

from backupper.errors import BackupError
from backupper.models import BackupConfig, SSHSettings


def resolve_config_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def load_config(path: Path) -> BackupConfig:
    if not path.is_file():
        raise BackupError(f'Config file not found: {path}')

    with path.open('rb') as file:
        config = tomllib.load(file)

    if not isinstance(config, dict):
        raise BackupError(f'Config file must contain a TOML table: {path}')

    config_dir = path.parent
    backup_root_raw = require_string(
        config.get('backup_root', 'backups'),
        'backup_root',
    )
    backup_root = Path(backup_root_raw).expanduser()
    if not backup_root.is_absolute():
        backup_root = config_dir / backup_root

    keep_backups = require_int(config.get('keep_backups', 5), 'keep_backups')
    if keep_backups < 1:
        raise BackupError(
            'Config value keep_backups must be greater than zero'
        )

    keep_partial_days = require_int(
        config.get('keep_partial_days', 3),
        'keep_partial_days',
    )
    if keep_partial_days < 0:
        raise BackupError(
            'Config value keep_partial_days must be zero or greater'
        )

    ssh_connect_timeout = require_positive_int(
        config,
        'ssh_connect_timeout',
        10,
    )
    ssh_server_alive_interval = require_positive_int(
        config,
        'ssh_server_alive_interval',
        30,
    )
    ssh_server_alive_count_max = require_positive_int(
        config,
        'ssh_server_alive_count_max',
        3,
    )
    command_timeout_seconds = require_positive_int(
        config,
        'command_timeout_seconds',
        7200,
    )

    result = BackupConfig(
        config_path=path,
        backup_root=backup_root.resolve(),
        keep_backups=keep_backups,
        keep_partial_days=keep_partial_days,
        command_timeout_seconds=command_timeout_seconds,
        ssh=SSHSettings(
            connect_timeout=ssh_connect_timeout,
            server_alive_interval=ssh_server_alive_interval,
            server_alive_count_max=ssh_server_alive_count_max,
        ),
        path_sources=config_items(config, 'path_sources'),
        sqlite_sources=config_items(config, 'sqlite_sources'),
        postgres_projects=config_items(config, 'postgres_projects'),
    )

    if (
        not result.path_sources
        and not result.sqlite_sources
        and not result.postgres_projects
    ):
        raise BackupError('Config must contain at least one source')

    return result


def require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise BackupError(f'Config value {name} must be a non-empty string')
    return value


def require_int(value: Any, name: str) -> int:
    if not isinstance(value, int):
        raise BackupError(f'Config value {name} must be an integer')
    return value


def require_positive_int(
    config: dict[str, Any],
    name: str,
    default: int,
) -> int:
    value = require_int(config.get(name, default), name)
    if value < 1:
        raise BackupError(f'Config value {name} must be greater than zero')
    return value


def config_items(config: dict[str, Any], section_name: str) -> list[str]:
    section = config.get(section_name, {})
    if not isinstance(section, dict):
        raise BackupError(f'Config section [{section_name}] must be a table')

    items = section.get('items', [])
    valid_items = all(isinstance(item, str) and item for item in items)
    if not isinstance(items, list) or not valid_items:
        raise BackupError(
            f'Config section [{section_name}] must contain '
            'items = ["user@host:/path", ...]'
        )

    return items
