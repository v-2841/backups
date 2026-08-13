from __future__ import annotations

from pathlib import Path
import os
import shutil
import sys
import time

MAX_PATH_WIDTH = 44
RESULT_WIDTH = 6
SIZE_WIDTH = 9
DURATION_WIDTH = 7
FALLBACK_DETAIL_WIDTH = 160
SUMMARY_RULE_WIDTH = 60


def human_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return ''
    size = float(size_bytes)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024:
            if unit == 'B':
                return f'{int(size)} B'
            return f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} TB'


def human_duration(seconds: float) -> str:
    if seconds < 60:
        return f'{seconds:.1f}s'
    total_seconds = int(round(seconds))
    minutes, secs = divmod(total_seconds, 60)
    if minutes < 60:
        return f'{minutes}m {secs:02d}s'
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h {minutes:02d}m'


def truncate_text(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return '…'
    return text[:width - 1] + '…'


def truncate_path(path: str, width: int) -> str:
    if len(path) <= width:
        return path
    return '…' + path[-(width - 1):]


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def color_enabled(stream) -> bool:
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('TERM') == 'dumb':
        return False
    isatty = getattr(stream, 'isatty', None)
    return bool(isatty and isatty())


class Style:
    '''Optional ANSI coloring; plain pass-through when disabled.'''

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def wrap(self, text: str, code: str) -> str:
        if not self.enabled or not text:
            return text
        return f'\x1b[{code}m{text}\x1b[0m'

    def green(self, text: str) -> str:
        return self.wrap(text, '32')

    def yellow(self, text: str) -> str:
        return self.wrap(text, '33')

    def red(self, text: str) -> str:
        return self.wrap(text, '31;1')

    def cyan(self, text: str) -> str:
        return self.wrap(text, '36')

    def dim(self, text: str) -> str:
        return self.wrap(text, '2')

    def bold(self, text: str) -> str:
        return self.wrap(text, '1')


class Progress:
    '''Per-item progress lines plus a final run summary.

    In a TTY the line for the current item is drawn immediately and
    rewritten in place once the item finishes. Without a TTY (systemd
    journal, redirects) plain start/finish lines are printed instead.
    '''

    def __init__(
        self,
        labels: list[tuple[str, str, str]],
        stream=None,
    ):
        self.stream = stream if stream is not None else sys.stdout
        isatty = getattr(self.stream, 'isatty', None)
        self.is_tty = bool(isatty and isatty())
        self.style = Style(color_enabled(self.stream))
        self.labels = labels
        self.total = len(labels)
        self.counter_width = len(str(self.total)) if self.total else 1
        self.kind_width = max((len(k) for k, _, _ in labels), default=4)
        self.host_width = max((len(h) for _, h, _ in labels), default=0)
        self.path_width = min(
            max((len(p) for _, _, p in labels), default=0),
            MAX_PATH_WIDTH,
        )
        self.index = 0
        self.current_prefix = ''
        self.ok_count = 0
        self.failed_count = 0
        self.reused_count = 0
        self.fresh_count = 0
        self.total_bytes = 0
        self.run_started = time.monotonic()

    def print_header(self, snapshot_name: str, target: Path) -> None:
        name = self.style.bold(snapshot_name)
        self.println(f'Backup {name} → {display_path(target)}')

    def note(self, text: str) -> None:
        self.println(self.style.dim(text))

    def start_item(self) -> None:
        kind, host, path = self.labels[self.index]
        self.index += 1
        counter = f'[{self.index:>{self.counter_width}}/{self.total}]'
        path_text = truncate_path(path, self.path_width)
        self.current_prefix = (
            f'{counter} '
            f'{kind:<{self.kind_width}}  '
            f'{host:<{self.host_width}}  '
            f'{path_text:<{self.path_width}}'
        )
        if self.is_tty:
            self.stream.write(f'{self.current_prefix}  ...')
            self.stream.flush()
        else:
            self.println(f'{self.current_prefix}  ...')

    def finish_item(
        self,
        result: str,
        size_bytes: int | None,
        duration: float,
        *,
        failed: bool = False,
        reused: bool = False,
        detail: str = '',
    ) -> None:
        if failed:
            self.failed_count += 1
        else:
            self.ok_count += 1
            self.total_bytes += size_bytes or 0
            if reused:
                self.reused_count += 1
            else:
                self.fresh_count += 1

        result_cell = f'{result:<{RESULT_WIDTH}}'
        if failed:
            result_cell = self.style.red(result_cell)
        elif reused:
            result_cell = self.style.cyan(result_cell)

        size_cell = f'{human_size(size_bytes):>{SIZE_WIDTH}}'
        duration_cell = f'{human_duration(duration):>{DURATION_WIDTH}}'
        line = (
            f'{self.current_prefix}  {result_cell}'
            f'  {size_cell}  {duration_cell}'
        )
        if detail:
            detail_text = truncate_text(detail, self.detail_width())
            line += f'  {self.style.dim(detail_text)}'

        if self.is_tty:
            self.stream.write(f'\r\x1b[2K{line}\n')
            self.stream.flush()
        else:
            self.println(line)

    def print_summary(self, snapshot_dir: Path, status: str) -> None:
        elapsed = time.monotonic() - self.run_started
        ok_text = f'✔ {self.ok_count} ok'
        if self.reused_count:
            ok_text += (
                f' ({self.reused_count} reused, {self.fresh_count} fresh)'
            )
        parts = [self.style.green(ok_text)]
        if self.failed_count:
            parts.append(self.style.red(f'✘ {self.failed_count} failed'))
        parts.append(human_size(self.total_bytes))
        parts.append(human_duration(elapsed))

        if status == 'ok':
            status_text = self.style.green(status)
        elif status == 'failed':
            status_text = self.style.red(status)
        else:
            status_text = self.style.yellow(status)

        self.println(self.style.dim('─' * SUMMARY_RULE_WIDTH))
        self.println('   '.join(parts))
        self.println(
            f'Snapshot: {display_path(snapshot_dir)}  ({status_text})'
        )

    def detail_width(self) -> int:
        fixed = (
            len(self.current_prefix)
            + RESULT_WIDTH + SIZE_WIDTH + DURATION_WIDTH + 8
        )
        if not self.is_tty:
            return FALLBACK_DETAIL_WIDTH
        columns = shutil.get_terminal_size((120, 24)).columns
        return max(16, columns - fixed - 1)

    def println(self, text: str) -> None:
        print(text, file=self.stream, flush=True)
