"""Global PG identity mapping, private-volume per-user residuals; no SQLite registry."""
import asyncio
import inspect
from pathlib import Path
from .accounts import AccountManager, AccountError, UserServiceHandle, _private_dir, _secure_tree
from .auth import Principal


class PostgresAccountManager(AccountManager):
    def __init__(self, auth_root, service_factory, runtime, *, max_accounts=8):
        super().__init__(auth_root, service_factory, max_accounts=max_accounts)
        self.runtime = runtime

    def _initialize(self):
        marker=self.root/'.postgres-root'
        if self.root.exists() and not marker.exists() and any(self.root.iterdir()):
            raise AccountError()
        _private_dir(self.root)
        if marker.is_symlink():
            raise AccountError()
        if not marker.exists():
            with marker.open('xb') as f: f.write(b'companion-postgres-residual-v1\n')
            marker.chmod(0o600)
        if marker.read_bytes()!=b'companion-postgres-residual-v1\n':
            raise AccountError()
        _private_dir(self.root/'users')

    async def _service_for(self, principal):
        self._require_principal(principal)
        account_id=await self.runtime.identity(principal)
        return await self._open_account(account_id)

    async def _open_account(self, account_id):
        if self._closing: raise AccountError()
        async with self._lock:
            self._initialize()
            if account_id in self._handles:
                return self._handles[account_id]
            if len(self._handles)>=self._maximum:
                raise AccountError('account_capacity')
            directory=self.root/'users'/account_id
            _private_dir(directory)
            service=None
            try:
                service=self._factory(directory)
                if inspect.isawaitable(service): service=await service
                if any(h.service is service for h in self._handles.values()):
                    service=None
                    raise AccountError()
                await service.start()
                _secure_tree(directory)
                handle=UserServiceHandle(account_id,directory,service)
                self._handles[account_id]=handle
                return handle
            except BaseException:
                if service is not None: await service.close()
                raise AccountError() from None

    async def service_for_account(self, account_id):
        # Internal verified-ownership worker only. Never expose account selectors.
        row=await self.runtime.identity(account_id=account_id)
        if row is None: raise AccountError()
        return await self._open_account(str(row[0]))

    async def close_account(self, principal):
        self._require_principal(principal)
        async with self._lock:
            row=await self.runtime.identity(principal)
            handle=self._handles.get(row)
            if handle:
                await handle.service.close()
                del self._handles[row]
