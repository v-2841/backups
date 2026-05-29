from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from backupper.config import load_config, resolve_config_path
from backupper.runner import run_backup

DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name('config.toml')


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Create VPS backup snapshots.',
    )
    parser.add_argument(
        '-c',
        '--config',
        default=os.environ.get('BACKUPS_CONFIG', str(DEFAULT_CONFIG_PATH)),
        help=(
            'Path to TOML config file. Defaults to BACKUPS_CONFIG '
            'or ./config.toml.'
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        config = load_config(resolve_config_path(args.config))
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 2

    return run_backup(config)


if __name__ == '__main__':
    raise SystemExit(main())
