"""Account-scoped snapshot/receipt store; lifetime advisory ownership, short transactions."""
import asyncio
import fcntl
import os
from uuid import UUID
from psycopg.types.json import Jsonb
from ..postgres import PostgresUnavailable, lock_key


class PostgresPersistenceExtension:
    id, version, status = "persistence", "1", "available"

    def __init__(self, runtime, account_id, residual_dir=None):
        self.runtime, self.account_id = runtime, str(UUID(account_id))
        self.residual_dir, self._file_fd = residual_dir, None
        self.conn = None
        self.lock = asyncio.Lock()

    async def _connect(self):
        if self.conn is not None:
            if self.conn.closed or self.conn.broken:
                raise PostgresUnavailable()
            return
        await self.runtime.open()
        conn = None
        try:
            if self.residual_dir is not None:
                from ..accounts import _private_file
                self._file_fd = _private_file(self.residual_dir / '.postgres-service.lock')
                fcntl.flock(self._file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            conn = await self.runtime.pool.getconn(timeout=3)
            row = await (await conn.execute("SELECT pg_try_advisory_lock(%s)", (lock_key(self.account_id),))).fetchone()
            if not row[0]:
                raise PostgresUnavailable()
            self.conn = conn
        except BaseException:
            if conn is not None: await self.runtime.pool.putconn(conn)
            if self._file_fd is not None:
                os.close(self._file_fd)
                self._file_fd = None
            raise

    async def handle(self, operation, payload):
        async with self.lock:
            try:
                if operation == "close":
                    if self.conn is not None:
                        conn, self.conn = self.conn, None
                        # Never put a session lock back into the pool, even on cancellation.
                        await conn.close()
                        await self.runtime.pool.putconn(conn)
                    if self._file_fd is not None:
                        os.close(self._file_fd)
                        self._file_fd = None
                    return {"closed": True}
                await self._connect()
                async with self.conn.transaction():
                    await self.conn.execute("SELECT set_config('companion.account_id',%s,true)", (self.account_id,))
                    if operation == "load":
                        row = await (await self.conn.execute("SELECT body FROM companion_private.snapshots WHERE account_id=%s", (self.account_id,))).fetchone()
                        return row[0] if row else {}
                    if operation == "receipt":
                        row = await (await self.conn.execute("SELECT digest,response FROM companion_private.receipts WHERE account_id=%s AND epoch=%s AND route=%s AND request_id=%s",
                            (self.account_id,payload['epoch'],payload['route'],payload['request_id']))).fetchone()
                        return {"digest":row[0],"response":row[1]} if row else {}
                    if operation != "commit":
                        raise ValueError("Unsupported persistence operation")
                    await self._ownership(payload['document'])
                    await self._retain_patterns()
                    if payload.get('health_ingest'):
                        from ..health_ingestion import stage_health
                        await stage_health(self.conn,self.account_id,payload['health_ingest'])
                    from ..pattern_banking import persist_bank_patterns
                    await persist_bank_patterns(self.conn,self.account_id,payload['document'],payload.get('pattern_bank_changes') or [])
                    if payload.get('erase_receipts'):
                        await self.conn.execute("DELETE FROM companion_private.receipts WHERE account_id=%s", (self.account_id,))
                    for route in payload.get('erase_receipt_routes',[]):
                        await self.conn.execute("DELETE FROM companion_private.receipts WHERE account_id=%s AND route=%s", (self.account_id,route))
                    if payload.get('receipt_cutoff'):
                        await self.conn.execute("DELETE FROM companion_private.receipts WHERE account_id=%s AND created_at < %s", (self.account_id,payload['receipt_cutoff']))
                    await self.conn.execute("INSERT INTO companion_private.snapshots(account_id,body) VALUES(%s,%s) ON CONFLICT(account_id) DO UPDATE SET body=EXCLUDED.body WHERE companion_private.snapshots.account_id=%s",
                        (self.account_id,Jsonb(payload['document']),self.account_id))
                    r=payload.get('receipt')
                    if r:
                        await self.conn.execute("""INSERT INTO companion_private.receipts(account_id,epoch,route,request_id,digest,response,created_at)
                            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(account_id,epoch,route,request_id)
                            DO UPDATE SET digest=EXCLUDED.digest,response=EXCLUDED.response,created_at=EXCLUDED.created_at
                            WHERE companion_private.receipts.account_id=%s""",
                            (self.account_id,r['epoch'],r['route'],r['request_id'],r['digest'],Jsonb(r['response']) if r['response'] is not None else None,r['created_at'],self.account_id))
                    if payload.get('recover_pending'):
                        await self.conn.execute("UPDATE companion_private.receipts SET response=%s WHERE account_id=%s AND response IS NULL", (Jsonb(payload['document']['state']),self.account_id))
                    return {"committed":True}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                from ..contracts import DomainError
                if isinstance(exc,DomainError): raise
                raise PostgresUnavailable() from None

    async def _retain_patterns(self):
        ready=await (await self.conn.execute("SELECT to_regclass('companion_private.pattern_batches') IS NOT NULL")).fetchone()
        if not ready[0]: return
        pages=await (await self.conn.execute("SELECT to_regclass('companion_private.health_import_pages') IS NOT NULL")).fetchone()
        if pages[0]:await self.conn.execute("DELETE FROM companion_private.health_import_pages WHERE account_id=%s AND received_at<now()-interval '1 day'",(self.account_id,))
        for table,field in [('health_samples','end_at'),('pattern_transactions','event_at'),('pattern_batches','end_at')]:
            cursor=await self.conn.execute('DELETE FROM companion_private.'+table+' WHERE account_id=%s AND '+field+" < now()-interval '190 days'",(self.account_id,))
        # Review history/answers/request bindings have their OWN retention clock. Raw expiry
        # cannot erase an unrelated current review or allow its request to invoke the provider again.
        await self.conn.execute("DELETE FROM companion_private.pattern_runs WHERE account_id=%s AND analyzed_at < now()-interval '190 days'",(self.account_id,))

    @staticmethod
    def contexts(doc):
        meta=doc.get('meta',{})
        if 'banking_item_contexts' in meta:
            result={}
            for item in meta['banking_item_contexts']:
                identifier,generation=item.get('item_id'),item.get('generation')
                if not isinstance(identifier,str) or not 1<=len(identifier)<=256 or identifier in result or type(generation) is not int or generation<0:
                    raise PostgresUnavailable()
                result[identifier]=generation
            return result
        item=meta.get('plaid_item_id') if meta.get('plaid_connected') else None
        if meta.get('plaid_connected') and not item:raise PostgresUnavailable()
        if item is not None and (not isinstance(item,str) or not 1<=len(item)<=256):raise PostgresUnavailable()
        return {item:0} if item else {}

    async def _ownership(self, doc):
        account=await (await self.conn.execute('SELECT generation FROM companion_private.accounts WHERE account_id=%s FOR NO KEY UPDATE',(self.account_id,))).fetchone()
        if account is None:raise PostgresUnavailable()
        previous=await (await self.conn.execute('SELECT body FROM companion_private.snapshots WHERE account_id=%s',(self.account_id,))).fetchone()
        old=previous[0] if previous else {}
        before,after=self.contexts(old),self.contexts(doc)
        epoch=doc['state']['data_epoch'];epoch_changed=bool(old) and old['state']['data_epoch']!=epoch
        rows=await (await self.conn.execute("SELECT item_id,generation,active,epoch FROM companion_private.plaid_owners WHERE account_id=%s",(self.account_id,))).fetchall()
        owned={r[0]:r for r in rows};changed_any=False
        for identifier,row in owned.items():
            if (row[2] or epoch_changed) and identifier not in after:
                changed_any=True
                await self.conn.execute("UPDATE companion_private.plaid_owners SET active=false,generation=generation+1 WHERE account_id=%s AND environment='sandbox' AND item_id=%s",(self.account_id,identifier))
                await self._erase_item_jobs(identifier,include_unknown=True)
        for identifier,selection_version in after.items():
            row=owned.get(identifier)
            changed=row is None or not row[2] or epoch_changed or before.get(identifier)!=selection_version
            generation=(row[1]+1 if row else 1) if changed else row[1]
            owner=await (await self.conn.execute("""INSERT INTO companion_private.plaid_owners(environment,item_id,account_id,epoch,generation,active)
                VALUES('sandbox',%s,%s,%s,%s,true) ON CONFLICT(environment,item_id)
                DO UPDATE SET epoch=EXCLUDED.epoch,generation=EXCLUDED.generation,active=true
                WHERE companion_private.plaid_owners.account_id=%s RETURNING account_id""",(identifier,self.account_id,epoch,generation,self.account_id))).fetchone()
            if owner is None:raise PostgresUnavailable()
            if changed:
                changed_any=True
                # Preserve an unassigned exchange-race quarantine, but fence old leases.
                await self._erase_item_jobs(identifier)
            await self.conn.execute("""UPDATE companion_private.webhook_jobs SET account_id=%s,epoch=%s,owner_generation=%s
                WHERE environment='sandbox' AND item_id=%s AND account_id IS NULL AND expires_at>now()""",(self.account_id,epoch,generation,identifier))
        if epoch_changed:
            ready=await (await self.conn.execute("SELECT to_regclass('companion_private.pattern_batches') IS NOT NULL")).fetchone()
            if ready[0]:
                pages=await (await self.conn.execute("SELECT to_regclass('companion_private.health_import_pages') IS NOT NULL")).fetchone()
                if pages[0]:await self.conn.execute('DELETE FROM companion_private.health_import_pages WHERE account_id=%s',(self.account_id,))
                await self.conn.execute('DELETE FROM companion_private.pattern_runs WHERE account_id=%s',(self.account_id,))
                await self.conn.execute('DELETE FROM companion_private.pattern_batches WHERE account_id=%s',(self.account_id,))
        if changed_any or epoch_changed:
            await self.conn.execute('UPDATE companion_private.accounts SET generation=generation+1 WHERE account_id=%s',(self.account_id,))

    async def _erase_item_jobs(self,identifier,include_unknown=False):
        if include_unknown:
            await self.conn.execute("DELETE FROM companion_private.webhook_jobs j USING companion_private.plaid_owners o WHERE o.account_id=%s AND o.environment='sandbox' AND o.item_id=%s AND j.environment=o.environment AND j.item_id=o.item_id AND (j.account_id=%s OR j.account_id IS NULL)",(self.account_id,identifier,self.account_id))
        else:
            await self.conn.execute("DELETE FROM companion_private.webhook_jobs WHERE account_id=%s AND environment='sandbox' AND item_id=%s",(self.account_id,identifier))
        await self.conn.execute("DELETE FROM companion_private.webhook_receipts r USING companion_private.plaid_owners o WHERE o.account_id=%s AND o.environment='sandbox' AND o.item_id=%s AND r.environment=o.environment AND r.item_id=o.item_id",(self.account_id,identifier))
