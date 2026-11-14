"""Single-process authenticated identity registry; no anonymous store adoption.

Composition injects a fresh service factory taking only an internal user's Path.
It MUST place all mutable stores/vault/checkpoints and extension/scheduler state
there (or in that service instance). The manager never imports bootstrap or dotenv.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import fcntl
import inspect
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any
from uuid import UUID, uuid4

from .auth import Principal

_MARKER = b"companion-auth-root-v1\n"


class AccountError(Exception):
    """Safe 503: never report identities, paths, or underlying storage exceptions."""

    status_code = 503
    retryable = True

    def __init__(self, code: str = "accounts_unavailable"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class UserWorkContext:
    """Capture before suspension; service/coordinator must recheck before effects."""

    account_id: str
    data_epoch: str
    generation: int


@dataclass(frozen=True, slots=True)
class UserServiceHandle:
    account_id: str
    data_dir: Path
    service: Any


def _private_dir(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise AccountError()
    path.chmod(0o700)


def _private_file(path: Path) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(fd)
        raise AccountError()
    os.fchmod(fd, 0o600)
    return fd


def _secure_tree(path: Path) -> None:
    """Reject symlinks/nonregular files; make initialized factory artifacts private.

    Runtime files must also be created 0600 by the injected store implementations;
    the 0700 enclosing root is the boundary even during their creation.
    """
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            _private_dir(Path(base) / name)
        for name in files:
            fd = _private_file(Path(base) / name)
            os.close(fd)


class AccountManager:
    """Lazy serialized startup, no implicit eviction or current-user variable.

    Hold one manager for the lifespan, one worker only. Capacity counts resident
    handles; admission refusal does not discard scheduler/background state. Use
    handles() for server-owned housekeeping, never the last HTTP caller. Explicit
    close_account stops an account; parent must quiesce requests/work first.
    """

    def __init__(self, auth_root: Path, service_factory: Callable[[Path], Any | Awaitable[Any]],
                 *, max_accounts: int = 128):
        if type(max_accounts) is not int or not 1 <= max_accounts <= 10000:
            raise ValueError("max_accounts must be in [1, 10000]")
        path = Path(auth_root)
        if not path.is_absolute() or not path.name or path.name in {".", ".."}:
            raise ValueError("auth_root must be absolute and dedicated")
        # Resolve parents only; a symlink at the root itself must not be followed.
        self.root = path.parent.resolve(strict=True) / path.name
        self._factory, self._maximum = service_factory, max_accounts
        self._lock = asyncio.Lock()
        self._handles: dict[str, UserServiceHandle] = {}
        self._pending: set[asyncio.Task] = set()
        self._closing = False
        self._db: sqlite3.Connection | None = None
        self._lock_fd: int | None = None
        self._shutdown_task: asyncio.Task | None = None

    def _initialize(self) -> None:
        if self._db is not None:
            return
        fd = None
        db = None
        try:
            marker = self.root / ".auth-root"
            # Refuse foreign roots BEFORE chmod or creating any artifact there.
            # lstat also refuses a symlink root without following it.
            if self.root.exists() or self.root.is_symlink():
                info = self.root.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                    raise AccountError()
                if not marker.exists() and any(self.root.iterdir()):
                    raise AccountError()
                if marker.exists() and (marker.is_symlink()
                        or not stat.S_ISREG(marker.stat().st_mode)
                        or marker.stat().st_size != len(_MARKER) or marker.read_bytes() != _MARKER):
                    raise AccountError()
            _private_dir(self.root)
            marker_fd = _private_file(marker)
            try:
                content = os.read(marker_fd, 128)
                if not content:
                    os.write(marker_fd, _MARKER)
                    os.fsync(marker_fd)
                elif content != _MARKER:
                    raise AccountError()
            finally:
                os.close(marker_fd)
            fd = _private_file(self.root / ".registry.lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            db_path = self.root / "identities.sqlite"
            db_fd = _private_file(db_path)
            os.close(db_fd)
            db = sqlite3.connect(db_path, isolation_level=None, timeout=5)
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("""CREATE TABLE IF NOT EXISTS identities (
                issuer TEXT NOT NULL, subject TEXT NOT NULL,
                account_id TEXT NOT NULL PRIMARY KEY,
                UNIQUE(issuer, subject))""")
            _private_dir(self.root / "users")
            _secure_tree(self.root)
            self._db, self._lock_fd = db, fd
        except Exception:
            if db is not None:
                db.close()
            if fd is not None:
                os.close(fd)
            raise AccountError() from None

    @staticmethod
    def _require_principal(principal: Principal) -> None:
        if not isinstance(principal, Principal):
            raise TypeError("A verified Principal is required")

    async def service_for(self, principal: Principal) -> UserServiceHandle:
        self._require_principal(principal)
        if self._closing:
            raise AccountError()
        # Request cancellation cannot strand partially started services. Shutdown
        # waits for all shielded admissions before draining the resident handles.
        task = asyncio.create_task(self._service_for(principal))
        self._pending.add(task)
        task.add_done_callback(self._admission_done)
        return await asyncio.shield(task)

    def _admission_done(self, task: asyncio.Task) -> None:
        self._pending.discard(task)
        if not task.cancelled():
            task.exception()  # Retrieve failures even if the requesting HTTP task left.

    async def _service_for(self, principal: Principal) -> UserServiceHandle:
        async with self._lock:
            self._initialize()
            row = self._db.execute("SELECT account_id FROM identities WHERE issuer=? AND subject=?",
                                   (principal.issuer, principal.subject)).fetchone()
            if row is not None and row[0] in self._handles:
                return self._handles[row[0]]
            if len(self._handles) >= self._maximum:
                raise AccountError("account_capacity")
            account_id = row[0] if row else str(uuid4())
            try:
                if str(UUID(account_id)) != account_id:
                    raise ValueError
                directory = self.root / "users" / account_id
                _private_dir(directory)
                if row is None:
                    self._db.execute("INSERT INTO identities(issuer,subject,account_id) VALUES(?,?,?)",
                                     (principal.issuer, principal.subject, account_id))
                service = None
                try:
                    service = self._factory(directory)
                    if inspect.isawaitable(service):
                        service = await service
                    if any(h.service is service for h in self._handles.values()):
                        # Do not close another user's active service on bad composition.
                        service = None
                        raise ValueError("factory reused a service")
                    await service.start()
                    _secure_tree(directory)
                except BaseException:
                    if service is not None:
                        await service.close()
                    raise
                handle = UserServiceHandle(account_id, directory, service)
                self._handles[account_id] = handle
                return handle
            except Exception:
                raise AccountError() from None

    async def handles(self) -> tuple[UserServiceHandle, ...]:
        """Immutable snapshot of initialized handles for per-user housekeeping."""
        async with self._lock:
            return tuple(self._handles.values())

    async def close_account(self, principal: Principal) -> None:
        """Explicit stop, not deletion or idle eviction. Caller must quiesce work."""
        self._require_principal(principal)
        if self._closing:
            raise AccountError()
        async with self._lock:
            if self._db is None:
                return
            row = self._db.execute("SELECT account_id FROM identities WHERE issuer=? AND subject=?",
                                   (principal.issuer, principal.subject)).fetchone()
            handle = self._handles.get(row[0]) if row else None
            if handle is not None:
                # Keep the handle if close fails; never silently evict live work.
                task = asyncio.create_task(handle.service.close())
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    try:
                        await task
                    except Exception:
                        raise AccountError() from None
                    del self._handles[handle.account_id]
                    raise
                except Exception:
                    raise AccountError() from None
                del self._handles[handle.account_id]

    async def aclose(self) -> None:
        """Idempotent shielded drain; failed closes retain lock/handle for retry."""
        self._closing = True
        if self._shutdown_task is None or (self._shutdown_task.done() and self._shutdown_task.exception()):
            self._shutdown_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self) -> None:
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)
        async with self._lock:
            failures = False
            for account_id, handle in tuple(self._handles.items()):
                try:
                    await handle.service.close()
                except Exception:
                    failures = True
                else:
                    del self._handles[account_id]
            if failures:
                raise AccountError()
            if self._db is not None:
                self._db.close()
                self._db = None
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
