from __future__ import annotations

from pathlib import Path
import json
import shlex
import subprocess
import tempfile

from backupper.errors import BackupError
from backupper.models import BackupConfig, RemoteSpec


def remote_bash_command(
    config: BackupConfig,
    target: str,
    script: str,
) -> list[str]:
    return [
        'ssh',
        '-o',
        'BatchMode=yes',
        '-o',
        f'ConnectTimeout={config.ssh.connect_timeout}',
        '-o',
        f'ServerAliveInterval={config.ssh.server_alive_interval}',
        '-o',
        f'ServerAliveCountMax={config.ssh.server_alive_count_max}',
        target,
        'bash -lc ' + shlex.quote(script),
    ]


def run_capture(config: BackupConfig, command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=config.command_timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise BackupError(
            f'Command timed out after {config.command_timeout_seconds}s: '
            f'{format_command(command)}'
        ) from error

    if result.returncode != 0:
        command_text = format_command(command)
        raise BackupError(
            f'Command failed ({result.returncode}): {command_text}\n'
            f'stdout:\n{result.stdout}\n'
            f'stderr:\n{result.stderr}'
        )
    return result.stdout


def copy_remote_path(
    config: BackupConfig,
    spec: RemoteSpec,
    destination_root: Path,
) -> Path:
    if not spec.relative_path:
        raise BackupError(
            'Refusing to copy remote root path: '
            f'{spec.ssh_target}:{spec.path}'
        )

    destination_root.mkdir(parents=True, exist_ok=True)
    remote_rel = spec.relative_path

    remote_script = f'''
set -euo pipefail
test -e {shlex.quote(spec.path)}
tar -C / -cf - -- {shlex.quote(remote_rel)}
'''.strip()

    with (
        tempfile.TemporaryFile() as ssh_stderr_file,
        tempfile.TemporaryFile() as tar_stderr_file,
    ):
        ssh_proc = subprocess.Popen(
            remote_bash_command(config, spec.ssh_target, remote_script),
            stdout=subprocess.PIPE,
            stderr=ssh_stderr_file,
        )
        assert ssh_proc.stdout is not None

        tar_proc = subprocess.Popen(
            ['tar', '-C', str(destination_root), '-xf', '-'],
            stdin=ssh_proc.stdout,
            stdout=subprocess.DEVNULL,
            stderr=tar_stderr_file,
        )
        ssh_proc.stdout.close()

        try:
            tar_proc.wait(timeout=config.command_timeout_seconds)
            ssh_returncode = ssh_proc.wait(
                timeout=max(1, config.command_timeout_seconds),
            )
        except subprocess.TimeoutExpired as error:
            kill_processes(tar_proc, ssh_proc)
            raise BackupError(
                f'Timed out after {config.command_timeout_seconds}s '
                f'while copying {spec.ssh_target}:{spec.path}'
            ) from error

        ssh_stderr_file.seek(0)
        tar_stderr_file.seek(0)
        ssh_stderr = ssh_stderr_file.read()
        tar_stderr = tar_stderr_file.read()
        ssh_stderr_text = ssh_stderr.decode(errors='replace')
        tar_stderr_text = tar_stderr.decode(errors='replace')

    if ssh_returncode != 0 or tar_proc.returncode != 0:
        raise BackupError(
            f'Failed to copy {spec.ssh_target}:{spec.path}\n'
            f'ssh exit code: {ssh_returncode}\n'
            f'tar exit code: {tar_proc.returncode}\n'
            f'ssh stderr:\n{ssh_stderr_text}\n'
            f'tar stderr:\n{tar_stderr_text}'
        )

    copied_path = destination_root / remote_rel
    if not copied_path.exists():
        raise BackupError(
            'Copy finished but local path was not created: '
            f'{copied_path}'
        )

    return copied_path


def remote_path_fingerprint(
    config: BackupConfig,
    spec: RemoteSpec,
) -> dict:
    script = f'''
set -euo pipefail
python3 - {shlex.quote(spec.path)} <<'PY'
from pathlib import Path
import hashlib
import json
import os
import stat
import sys

root = Path(sys.argv[1])
root_stat = os.lstat(root)
digest = hashlib.sha256()


def add_text(value):
    digest.update(str(value).encode('utf-8', 'surrogateescape'))
    digest.update(b'\\0')


def file_digest(path):
    file_hash = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            file_hash.update(chunk)
    return file_hash.hexdigest()


def add_regular_file(path, relative_path, info):
    add_text('file')
    add_text(relative_path)
    add_text(stat.S_IMODE(info.st_mode))
    add_text(info.st_size)
    add_text(file_digest(path))


def add_symlink(path, relative_path, info):
    add_text('symlink')
    add_text(relative_path)
    add_text(stat.S_IMODE(info.st_mode))
    add_text(os.readlink(path))


def add_other(path, relative_path, info):
    add_text('other')
    add_text(relative_path)
    add_text(stat.S_IFMT(info.st_mode))
    add_text(stat.S_IMODE(info.st_mode))


if stat.S_ISLNK(root_stat.st_mode):
    kind = 'symlink'
    total_size = 0
    file_count = 0
    dir_count = 0
    symlink_count = 1
    add_symlink(root, '.', root_stat)
elif root.is_file():
    kind = 'file'
    total_size = root_stat.st_size
    file_count = 1
    dir_count = 0
    symlink_count = 0
    add_regular_file(root, '.', root_stat)
elif root.is_dir():
    kind = 'directory'
    total_size = 0
    file_count = 0
    dir_count = 0
    symlink_count = 0

    for current, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        current_path = Path(current)
        current_relative = current_path.relative_to(root).as_posix()
        if current_relative == '.':
            current_relative = ''

        current_stat = os.lstat(current_path)
        dir_count += 1
        add_text('dir')
        add_text(current_relative)
        add_text(stat.S_IMODE(current_stat.st_mode))

        for name in list(dirnames):
            path = current_path / name
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                relative_path = path.relative_to(root).as_posix()
                symlink_count += 1
                add_symlink(path, relative_path, info)
                dirnames.remove(name)

        for name in filenames:
            path = current_path / name
            relative_path = path.relative_to(root).as_posix()
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode):
                file_count += 1
                total_size += info.st_size
                add_regular_file(path, relative_path, info)
            elif stat.S_ISLNK(info.st_mode):
                symlink_count += 1
                add_symlink(path, relative_path, info)
            else:
                add_other(path, relative_path, info)
else:
    raise SystemExit(f'Unsupported source type: {{root}}')

print(json.dumps(
    {{
        'method': 'sha256-content-v1',
        'kind': kind,
        'fingerprint': digest.hexdigest(),
        'size_bytes': total_size,
        'file_count': file_count,
        'dir_count': dir_count,
        'symlink_count': symlink_count,
    }},
    sort_keys=True,
))
PY
'''.strip()

    output = run_capture(
        config,
        remote_bash_command(config, spec.ssh_target, script),
    )
    fingerprint = json.loads(output)
    if not isinstance(fingerprint, dict):
        raise BackupError(
            f'Invalid fingerprint response for {spec.ssh_target}:{spec.path}'
        )
    return fingerprint


def stream_remote_stdout_to_file(
    config: BackupConfig,
    target: str,
    script: str,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        destination.open('wb') as file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        proc = subprocess.Popen(
            remote_bash_command(config, target, script),
            stdout=file,
            stderr=stderr_file,
        )
        try:
            returncode = proc.wait(timeout=config.command_timeout_seconds)
        except subprocess.TimeoutExpired as error:
            kill_processes(proc)
            destination.unlink(missing_ok=True)
            raise BackupError(
                f'Remote stream timed out after '
                f'{config.command_timeout_seconds}s for {destination}'
            ) from error

        stderr_file.seek(0)
        stderr = stderr_file.read()
        stderr_text = stderr.decode(errors='replace')

    if returncode != 0:
        destination.unlink(missing_ok=True)
        raise BackupError(
            f'Remote stream failed ({returncode}) for {destination}\n'
            f'stderr:\n{stderr_text}'
        )

    if destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise BackupError(
            f'Remote stream created an empty file: {destination}'
        )


def format_command(command: list[str]) -> str:
    return ' '.join(shlex.quote(part) for part in command)


def kill_processes(*processes: subprocess.Popen[bytes]) -> None:
    for proc in processes:
        if proc.poll() is None:
            proc.kill()
    for proc in processes:
        proc.wait()
