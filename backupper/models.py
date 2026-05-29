from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backupper.errors import BackupError
from backupper.utils import sanitize_path_part


@dataclass(frozen=True)
class SSHSettings:
    connect_timeout: int
    server_alive_interval: int
    server_alive_count_max: int


@dataclass(frozen=True)
class BackupConfig:
    config_path: Path
    backup_root: Path
    keep_backups: int
    keep_partial_days: int
    command_timeout_seconds: int
    ssh: SSHSettings
    path_sources: list[str]
    sqlite_sources: list[str]
    postgres_projects: list[str]


@dataclass(frozen=True)
class RemoteSpec:
    user: str
    host: str
    path: str

    @property
    def ssh_target(self) -> str:
        return f'{self.user}@{self.host}'

    @property
    def host_dir(self) -> str:
        return sanitize_path_part(self.host)

    @property
    def relative_path(self) -> str:
        return self.path.lstrip('/')

    @classmethod
    def parse(cls, raw: str) -> 'RemoteSpec':
        if ':' not in raw:
            raise BackupError(
                'Remote source must be user@host:/full/path, '
                f'got: {raw}'
            )

        host_part, path = raw.split(':', 1)
        if not host_part:
            raise BackupError(f'Remote source has empty host: {raw}')
        if not path.startswith('/'):
            raise BackupError(f'Remote source path must be absolute: {raw}')
        if '@' not in host_part:
            raise BackupError(
                'Remote source must include SSH user as '
                f'user@host:/full/path, got: {raw}'
            )

        user, host = host_part.split('@', 1)
        if not user or not host:
            raise BackupError(f'Remote source has invalid user/host: {raw}')

        return cls(user=user, host=host, path=path)
