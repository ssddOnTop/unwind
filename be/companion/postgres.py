"""Explicit PostgreSQL runtime. Secrets never appear in repr/errors; no implicit DDL."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
import hashlib
import os
from uuid import UUID, uuid4

from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import AsyncConnectionPool

from .accounts import AccountError


class PostgresUnavailable(AccountError):
    pass


@dataclass(frozen=True, repr=False)
class PostgresConfig:
    dsn: str = field(repr=False)
    max_connections: int = 16
    local_test: bool = False

    def __post_init__(self):
        try:
            values = conninfo_to_dict(self.dsn)
            if not 2 <= self.max_connections <= 32:
                raise ValueError
            if self.local_test:
                if values.get("host") not in {"127.0.0.1", "localhost"}:
                    raise ValueError
            elif values.get("sslmode", "verify-full") != "verify-full":
                raise ValueError
            elif not values.get("host") or values.get("hostaddr") or str(values.get('port',5432))=='6543':
                raise ValueError
        except Exception:
            raise ValueError("Invalid PostgreSQL configuration; verified TLS is required.") from None

    @classmethod
    def from_env(cls):
        dsn = os.environ.get("DATABASE_URL", "")
        if not dsn:
            raise ValueError("PostgreSQL requires DATABASE_URL.")
        return cls(dsn)


def lock_key(account_id):
    return int.from_bytes(hashlib.sha256(("companion-account:" + str(UUID(account_id))).encode()).digest()[:8], "big", signed=True)


class PostgresRuntime:
    def __init__(self, config: PostgresConfig):
        self.config = config
        self.pool = None
        self._lock = asyncio.Lock()

    async def open(self):
        async with self._lock:
            if self.pool is None:
                # Disable pool background error logging: libpq exceptions can include DSNs.
                import logging
                logging.getLogger("psycopg.pool").setLevel(logging.CRITICAL)
                kwargs = {"autocommit": True, "connect_timeout": 3,
                          "options": "-c statement_timeout=5000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=10000"}
                if not self.config.local_test:
                    kwargs["sslmode"] = "verify-full"
                    kwargs['sslrootcert'] = conninfo_to_dict(self.config.dsn).get('sslrootcert','system')
                self.pool = AsyncConnectionPool(self.config.dsn, kwargs=kwargs, min_size=0,
                    max_size=self.config.max_connections, timeout=3, max_waiting=32,
                    reconnect_timeout=3, open=False)
                await self.pool.open()

    async def check(self):
        await self.open()
        try:
            async with self.pool.connection() as conn:
                versions = await (await conn.execute('SELECT version FROM companion_private.schema_version')).fetchall()
                row = await (await conn.execute("""SELECT to_regclass('companion_private.snapshots') IS NOT NULL
                    AND to_regclass('companion_private.receipts') IS NOT NULL
                    AND to_regclass('companion_private.accounts') IS NOT NULL
                    AND to_regclass('companion_private.plaid_owners') IS NOT NULL
                    AND to_regclass('companion_private.webhook_jobs') IS NOT NULL
                    AND to_regclass('companion_private.webhook_receipts') IS NOT NULL""")).fetchone()
                if versions not in ([(1,)], [(2,)]) or not row[0]:
                    raise ValueError
        except Exception:
            raise PostgresUnavailable() from None

    async def identity(self, principal=None, *, account_id=None):
        await self.open()
        try:
            async with self.pool.connection() as conn:
                if account_id is not None:
                    row = await (await conn.execute("SELECT account_id, issuer, subject FROM companion_private.accounts WHERE account_id=%s", (str(UUID(account_id)),))).fetchone()
                    return row
                row = await (await conn.execute("""INSERT INTO companion_private.accounts(account_id,issuer,subject)
                    VALUES(%s,%s,%s) ON CONFLICT(issuer,subject) DO UPDATE SET issuer=EXCLUDED.issuer
                    RETURNING account_id""", (str(uuid4()), principal.issuer, principal.subject))).fetchone()
                return str(row[0])
        except Exception:
            raise PostgresUnavailable() from None

    async def may_remove_item(self, account_id, item_id):
        """Cleanup authority survives revocation via immutable owner tombstones.

        Never remove an uncommitted/unknown Item or another account's Item merely
        because a local pending exchange file contains a provider token.
        """
        if not isinstance(item_id,str) or not item_id:return False
        await self.open()
        try:
            async with self.pool.connection() as conn:
                row=await (await conn.execute("SELECT 1 FROM companion_private.plaid_owners WHERE environment='sandbox' AND item_id=%s AND account_id=%s",(item_id,str(UUID(account_id))))).fetchone()
                return bool(row)
        except Exception:return False

    async def reserve_item(self,account_id,item_id,epoch):
        """Inactive immutable reservation before any Item-specific provider request.

        Snapshot commit alone activates it. Abandoned reservations cannot route
        webhooks or be adopted by a different user.
        """
        if not isinstance(item_id,str) or not 1<=len(item_id)<=256:raise PostgresUnavailable()
        account_id=str(UUID(account_id));await self.open()
        try:
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    await conn.execute("INSERT INTO companion_private.plaid_owners(environment,item_id,account_id,epoch,generation,active) VALUES('sandbox',%s,%s,%s,0,false) ON CONFLICT(environment,item_id) DO NOTHING",(item_id,account_id,epoch))
                    row=await (await conn.execute("SELECT 1 FROM companion_private.plaid_owners WHERE environment='sandbox' AND item_id=%s AND account_id=%s",(item_id,account_id))).fetchone()
                    return bool(row)
        except Exception:raise PostgresUnavailable() from None

    async def close(self):
        if self.pool is not None:
            await self.pool.close(timeout=5)
            self.pool = None
