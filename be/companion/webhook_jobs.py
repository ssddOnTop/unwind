"""Private durable verified-event inbox and bounded in-monolith sync worker."""
import asyncio
from datetime import datetime, timezone
from .domain import instant
import re
from uuid import uuid4
from .postgres import PostgresUnavailable


class WebhookInbox:
    def __init__(self, runtime): self.runtime=runtime

    async def enqueue_verified(self,event):
        # The HTTP boundary must call the verifier before this internal callback.
        if (event.environment!='sandbox' or not isinstance(event.item_id,str) or not 1<=len(event.item_id)<=256
                or re.fullmatch('[0-9a-f]{64}',event.body_digest) is None):
            raise ValueError('Unsupported verified event')
        await self.runtime.open()
        try:
            async with self.runtime.pool.connection() as conn:
                async with conn.transaction():
                    # The inbox is global privileged scheduler metadata, never a client-selected account read.
                    # Serialize capacity admission; transaction ends before any provider operation.
                    await conn.execute('SELECT pg_advisory_xact_lock(686459532117)')
                    # Dedupe horizon 5m; unknown mappings are quarantined for 10m.
                    await conn.execute("DELETE FROM companion_private.webhook_receipts WHERE expires_at<now()")
                    await conn.execute("DELETE FROM companion_private.webhook_jobs j WHERE j.expires_at<now() AND (j.account_id IS NULL OR j.completed>=j.requested OR NOT EXISTS(SELECT 1 FROM companion_private.plaid_owners o WHERE o.environment=j.environment AND o.item_id=j.item_id AND o.account_id=j.account_id AND o.epoch=j.epoch AND o.generation=j.owner_generation AND o.active))")
                    owner=await (await conn.execute("SELECT account_id,epoch,generation,active FROM companion_private.plaid_owners WHERE environment=%s AND item_id=%s",(event.environment,event.item_id))).fetchone()
                    inserted=await (await conn.execute("""INSERT INTO companion_private.webhook_receipts(digest,received_at,environment,item_id,expires_at)
                        VALUES(%s,now(),%s,%s,now()+interval '5 minutes') ON CONFLICT(digest) DO NOTHING RETURNING digest""",(event.body_digest,event.environment,event.item_id))).fetchone()
                    if not inserted:
                        if owner and owner[3]:
                            # Repeated equal bodies can also mean a new notification during a running sync.
                            # An extra pass is safe; silently dropping a wakeup is not.
                            await conn.execute("UPDATE companion_private.webhook_jobs SET requested=requested+1 WHERE environment=%s AND item_id=%s AND account_id=%s AND owner_generation=%s AND lease_until>now()", (event.environment,event.item_id,owner[0],owner[2]))
                        return {'accepted':True,'duplicate':True}
                    count=await (await conn.execute('SELECT count(*) FROM companion_private.webhook_receipts')).fetchone()
                    if count[0]>10000: raise PostgresUnavailable()
                    if owner and not owner[3]:
                        if owner[2]!=0:return {'accepted':True,'duplicate':False}
                        # An inactive generation-zero reservation has not published
                        # consent yet. Quarantine, never target an account early.
                        owner=None
                    # Only sync notifications cause jobs. Valid other events are acknowledged, not interpreted as data.
                    if event.webhook_type!='TRANSACTIONS' or event.webhook_code not in {'SYNC_UPDATES_AVAILABLE','INITIAL_UPDATE','HISTORICAL_UPDATE','DEFAULT_UPDATE'}:
                        return {'accepted':True,'duplicate':False}
                    count=await (await conn.execute('SELECT count(*) FROM companion_private.webhook_jobs')).fetchone()
                    if count[0]>=10000: raise PostgresUnavailable()
                    await conn.execute("""INSERT INTO companion_private.webhook_jobs(environment,item_id,account_id,epoch,owner_generation,expires_at)
                        VALUES(%s,%s,%s,%s,%s,now()+CASE WHEN %s THEN interval '24 hours' ELSE interval '10 minutes' END)
                        ON CONFLICT(environment,item_id) DO UPDATE SET requested=companion_private.webhook_jobs.requested+1,
                        expires_at=GREATEST(companion_private.webhook_jobs.expires_at,EXCLUDED.expires_at),account_id=EXCLUDED.account_id,epoch=EXCLUDED.epoch,owner_generation=EXCLUDED.owner_generation,
                        attempts=CASE WHEN companion_private.webhook_jobs.lease_until IS NULL THEN 0 ELSE companion_private.webhook_jobs.attempts END,
                        available_at=CASE WHEN companion_private.webhook_jobs.completed<companion_private.webhook_jobs.requested THEN companion_private.webhook_jobs.available_at ELSE now() END""",
                        (event.environment,event.item_id,str(owner[0]) if owner else None,owner[1] if owner else None,owner[2] if owner else None,bool(owner)))
                    return {'accepted':True,'duplicate':False}
        except Exception:
            raise PostgresUnavailable() from None

    async def schedule_quiet_items(self):
        """One coalesced pending wakeup per active owner, including after process restart.

        The account snapshot is the authoritative due-time gate, not this wakeup.
        Ownership generation/epoch changes replace stale scheduling metadata only.
        """
        await self.runtime.open()
        async with self.runtime.pool.connection() as conn:
            await conn.execute("""INSERT INTO companion_private.webhook_jobs(environment,item_id,account_id,epoch,owner_generation,expires_at)
                SELECT environment,item_id,account_id,epoch,generation,now()+interval '24 hours' FROM companion_private.plaid_owners WHERE active
                ON CONFLICT(environment,item_id) DO UPDATE SET
                account_id=EXCLUDED.account_id,epoch=EXCLUDED.epoch,owner_generation=EXCLUDED.owner_generation,
                attempts=CASE WHEN companion_private.webhook_jobs.attempts>=12 AND companion_private.webhook_jobs.available_at<=now() THEN 0 ELSE companion_private.webhook_jobs.attempts END,
                requested=CASE WHEN companion_private.webhook_jobs.completed>=companion_private.webhook_jobs.requested THEN companion_private.webhook_jobs.requested+1 ELSE companion_private.webhook_jobs.requested END,
                available_at=CASE WHEN companion_private.webhook_jobs.completed>=companion_private.webhook_jobs.requested THEN now() ELSE companion_private.webhook_jobs.available_at END,
                expires_at=GREATEST(companion_private.webhook_jobs.expires_at,EXCLUDED.expires_at)
                WHERE companion_private.webhook_jobs.lease_until IS NULL OR companion_private.webhook_jobs.lease_until<now()""")

    async def defer(self,job,until):
        due=instant(until)
        async with self.runtime.pool.connection() as conn:
            await conn.execute("""UPDATE companion_private.webhook_jobs SET available_at=GREATEST(%s,now()+interval '5 seconds'),
                expires_at=GREATEST(expires_at,%s+interval '24 hours'),lease_token=NULL,lease_until=NULL,attempts=0
                WHERE environment=%s AND item_id=%s AND account_id=%s AND epoch=%s AND owner_generation=%s AND lease_token=%s""",
                (due,due,job['environment'],job['item_id'],job['account_id'],job['epoch'],job['generation'],job['token']))

    async def claim(self):
        await self.runtime.open()
        try:
            async with self.runtime.pool.connection() as conn:
                async with conn.transaction():
                    # Resolve only already verified, unexpired quarantine from immutable backend ownership.
                    # Covers an event transaction becoming visible just after exchange committed.
                    await conn.execute("""UPDATE companion_private.webhook_jobs j SET account_id=o.account_id,epoch=o.epoch,owner_generation=o.generation
                        FROM companion_private.plaid_owners o WHERE j.account_id IS NULL AND j.environment=o.environment
                        AND j.item_id=o.item_id AND o.active AND j.expires_at>now()""")
                    row=await (await conn.execute("""SELECT j.environment,j.item_id,j.account_id,j.epoch,j.owner_generation,j.requested
                        FROM companion_private.webhook_jobs j JOIN companion_private.plaid_owners o
                        ON o.environment=j.environment AND o.item_id=j.item_id AND o.account_id=j.account_id
                        AND o.epoch=j.epoch AND o.generation=j.owner_generation AND o.active
                        WHERE j.account_id IS NOT NULL AND j.completed<j.requested AND j.attempts<12
                        AND j.available_at<=now() AND (j.lease_until IS NULL OR j.lease_until<now())
                        ORDER BY j.available_at FOR UPDATE OF j SKIP LOCKED LIMIT 1""")).fetchone()
                    if not row:return None
                    token=str(uuid4())
                    await conn.execute("""UPDATE companion_private.webhook_jobs SET lease_token=%s,lease_until=now()+interval '360 seconds',attempts=attempts+1
                        WHERE environment=%s AND item_id=%s AND account_id=%s""",(token,row[0],row[1],row[2]))
                    return dict(environment=row[0],item_id=row[1],account_id=str(row[2]),epoch=row[3],generation=row[4],requested=row[5],token=token)
        except Exception: raise PostgresUnavailable() from None

    async def current(self,job):
        async with self.runtime.pool.connection() as conn:
            row=await (await conn.execute("SELECT 1 FROM companion_private.plaid_owners WHERE environment=%s AND item_id=%s AND account_id=%s AND epoch=%s AND generation=%s AND active",
                (job['environment'],job['item_id'],job['account_id'],job['epoch'],job['generation']))).fetchone()
            return bool(row)

    async def finish(self,job,success):
        try:
            async with self.runtime.pool.connection() as conn:
                # requested remains monotonic: arrivals during this lease force another pass.
                await conn.execute("""UPDATE companion_private.webhook_jobs SET completed=CASE WHEN %s THEN GREATEST(completed,%s) ELSE completed END,
                    available_at=now()+CASE WHEN %s THEN interval '0 seconds' WHEN attempts>=12 THEN interval '24 hours' ELSE LEAST(3600,power(2,attempts)::integer)*interval '1 second' END,
                    lease_token=NULL,lease_until=NULL,attempts=CASE WHEN %s THEN 0 ELSE attempts END
                    WHERE environment=%s AND item_id=%s AND account_id=%s AND epoch=%s AND owner_generation=%s AND lease_token=%s""",
                    (success,job['requested'],success,success,job['environment'],job['item_id'],job['account_id'],job['epoch'],job['generation'],job['token']))
        except Exception: raise PostgresUnavailable() from None


class WebhookWorker:
    def __init__(self,inbox,callback, *, concurrency=2, interval=2, periodic=False):
        if not 1<=concurrency<=4:raise ValueError('Invalid concurrency')
        self.inbox,self.callback,self.concurrency,self.interval=inbox,callback,concurrency,interval
        self.tasks=[]
        self.stopping=False
        self.periodic=periodic;self.next_periodic=0

    async def tick(self):
        now=asyncio.get_running_loop().time()
        if self.periodic and now>=self.next_periodic:
            self.next_periodic=now+60
            await self.inbox.schedule_quiet_items()
        job=await self.inbox.claim()
        if not job:return False
        success=False;deferred=None
        try:
            if await self.inbox.current(job):
                async with asyncio.timeout(300): result=await self.callback(job)
                if isinstance(result,dict):deferred=result.get('deferred_until')
            success=True
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            if deferred:await asyncio.shield(self.inbox.defer(job,deferred))
            else:await asyncio.shield(self.inbox.finish(job,success))
        return True

    async def _run(self):
        while not self.stopping:
            try: await self.tick()
            except Exception: pass
            if not self.stopping: await asyncio.sleep(self.interval)

    async def start(self):
        self.tasks=[asyncio.create_task(self._run()) for _ in range(self.concurrency)]

    async def close(self):
        self.stopping=True
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        self.tasks=[]
