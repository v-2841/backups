from __future__ import annotations

from pathlib import Path
import json


def write_manifest(snapshot_dir: Path, manifest: dict) -> None:
    manifest_path = snapshot_dir / 'manifest.json'
    manifest_path.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + '\n',
        encoding='utf-8',
    )
