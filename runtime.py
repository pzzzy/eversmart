"""Private filesystem, publication and retry helpers for the collector."""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterator


RETRYABLE_HTTP = {429, 500, 502, 503, 504}


class LocalError(RuntimeError):
    """An application-generated message safe to display, without external input."""


class LockBusy(LocalError):
    pass


class UpstreamError(RuntimeError):
    """Keep parser detail in memory; publication only uses structured status."""

    def __init__(self, detail: str, *, status: int | None = None, retry_after: float = 0, network: bool = False):
        super().__init__(detail)
        self.status = status
        self.retry_after = retry_after
        self.network = network


def state_directory(override: str | Path | None = None) -> Path:
    configured = override or os.environ.get("EVERSMART_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".local" / "state") / "eversmart"


def make_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def require_private_directory(path: Path) -> None:
    make_private_directory(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise LocalError("Use an owned, non-symlink state directory with permissions 0700.")


@contextmanager
def atomic_destination(path: Path, *, replace: bool = True) -> Iterator[Path]:
    """Stage in the destination directory so replacement is a single rename."""
    fd, name = tempfile.mkstemp(prefix=".eversmart-", suffix=path.suffix, dir=path.parent)
    os.close(fd)
    staged = Path(name)
    try:
        yield staged
        staged.chmod(0o600)
        with staged.open("rb") as stream:
            os.fsync(stream.fileno())
        if replace:
            os.replace(staged, path)
        else:
            try:
                os.link(staged, path, follow_symlinks=False)
            except FileExistsError:
                raise LocalError("The export destination already exists; choose a new file.") from None
    finally:
        staged.unlink(missing_ok=True)


def atomic_json(path: Path, value: object) -> None:
    with atomic_destination(path) as staged:
        staged.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


@contextmanager
def process_lock(path: Path) -> Iterator[None]:
    # Never unlink a lock file: two processes could otherwise lock different inodes.
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockBusy("Another Eversmart operation already holds the process lock.") from None
        yield
    finally:
        os.close(fd)


def retry_after_header(value: str | None) -> float:
    if not value:
        return 0
    value = value.strip()
    if value.isascii() and value.isdecimal():
        # Ignore out-of-range headers rather than raising in error recovery.
        return int(value) if len(value) <= 9 else 0
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0, (target - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, OverflowError, TypeError):
        return 0


def retry_delay(exc: Exception) -> float:
    if isinstance(exc, UpstreamError):
        return exc.retry_after
    if isinstance(exc, urllib.error.HTTPError):
        return retry_after_header(exc.headers.get("Retry-After") if exc.headers else None)
    return 0


def _http_status(exc: Exception) -> int | None:
    if isinstance(exc, UpstreamError):
        return exc.status
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code
    # Compatibility with older clients. Never include the body in status text.
    match = re.match(r"^Opower GraphQL HTTP ([1-5][0-9]{2}):", str(exc))
    return int(match[1]) if match else None


def is_transient(exc: Exception) -> bool:
    return (_http_status(exc) in RETRYABLE_HTTP
            or (isinstance(exc, UpstreamError) and exc.network)
            or (isinstance(exc, (urllib.error.URLError, TimeoutError))
                and not isinstance(exc, urllib.error.HTTPError)))


def safe_error(exc: Exception, fallback: str) -> str:
    if isinstance(exc, LocalError):
        return str(exc)
    status = _http_status(exc)
    if status is not None:
        if isinstance(exc, urllib.error.HTTPError):
            # Unclosed error responses can log their repr during cleanup.
            exc.close()
        return f"Upstream HTTP {status}."
    if is_transient(exc):
        return "Temporary network failure."
    return fallback
