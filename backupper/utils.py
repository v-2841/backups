from __future__ import annotations

from datetime import datetime
from pathlib import Path


def sanitize_path_part(value: str) -> str:
    safe = []
    for char in value:
        if char.isalnum() or char in '.-_':
            safe.append(char)
        else:
            safe.append('_')
    return ''.join(safe).strip('._') or 'unknown'


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def timestamp_name() -> str:
    return datetime.now().strftime('%Y-%m-%d_%H-%M-%S')


def path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        total = 0
        for item in path.rglob('*'):
            if item.is_file():
                total += item.stat().st_size
        return total
    return 0
