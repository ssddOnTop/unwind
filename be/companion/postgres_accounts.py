"""Global PG identity mapping, private-volume per-user residuals; no SQLite registry."""
import asyncio
import inspect
from contextlib import asynccontextmanager
from pathlib import Path
from .accounts import AccountManager, AccountError, UserServiceHandle, _private_dir, _secure_tree
from .auth import Principal


class PostgresAccountManager(AccountManager):
    def __init__(self, auth_root, service_factory, runtime, *, max_accounts=8):
        super().__init__(auth_root, service_factory, max_accounts=max_accounts)
        self.runtime = runtime
        self._borrowed={}
        self._temporary=set()

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
                candidate=self._factory(directory)
                service=await candidate if inspect.isawaitable(candidate) else candidate
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

    @asynccontextmanager
    async def borrow(self,principal):
        self._require_principal(principal)
        account=await self.runtime.identity(principal)
        async with self.borrow_account(account) as handle:yield handle

    @asynccontextmanager
    async def borrow_account(self,account_id,*,temporary=False):
        """Pin HTTP/worker use; close worker-only unloaded accounts after their last borrower.

        Two bounded overflow slots avoid filling the interactive cache with nightly users.
        HTTP may share a worker's handle safely without it closing during the request.
        """
        row=await self.runtime.identity(account_id=account_id)
        if row is None or self._closing:raise AccountError()
        account_id=str(row[0])
        async with self._lock:
            self._initialize()
            handle=self._handles.get(account_id)
            if handle is None:
                if len(self._handles)>=self._maximum+(2 if temporary else 0):raise AccountError('account_capacity')
                directory=self.root/'users'/account_id;_private_dir(directory)
                service=None
                try:
                    candidate=self._factory(directory)
                    service=await candidate if inspect.isawaitable(candidate) else candidate
                    await service.start();_secure_tree(directory)
                    handle=UserServiceHandle(account_id,directory,service)
                    self._handles[account_id]=handle
                    if temporary or len(self._handles)>self._maximum:self._temporary.add(account_id)
                except BaseException:
                    if service is not None:await service.close()
                    raise AccountError() from None
            self._borrowed[account_id]=self._borrowed.get(account_id,0)+1
        try:yield handle
        finally:
            async with self._lock:
                self._borrowed[account_id]-=1
                if not self._borrowed[account_id]:
                    del self._borrowed[account_id]
                    if account_id in self._temporary:
                        await handle.service.close();self._handles.pop(account_id,None);self._temporary.discard(account_id)

    async def close_account(self, principal):
        self._require_principal(principal)
        async with self._lock:
            row=await self.runtime.identity(principal)
            handle=self._handles.get(row)
            if handle:
                await handle.service.close()
                del self._handles[row]
