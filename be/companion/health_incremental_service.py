"""Readiness and independent raw-history retention; no startup DDL or providers."""
import asyncio,calendar,os
from datetime import datetime,timedelta,timezone
from typing import Literal
from .patterns import Strict
from .contracts import DomainError
from .health_incremental import HKType,prune_health_incremental_history
from .health_incremental_migrations import TABLES

TYPES=('stepCount','sleepAnalysis','heartRate','restingHeartRate','workout')
class HealthStreamStatus(Strict):
    hk_type: HKType
    stream_id: str | None
    next_sequence: int
    bootstrapped: bool
class HealthIncrementalStatus(Strict):
    schema_version: Literal['1']='1'
    data_epoch: str
    enabled: bool
    schema_ready: bool
    source_allowed: bool
    authorization_requested: bool
    snapshot_allowed: bool
    baseline_supported: bool
    baseline_start: str
    message: str
    streams: list[HealthStreamStatus]

async def ready(conn):
    row=await (await conn.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='companion_private' AND c.relname=ANY(%s) AND c.relrowsecurity AND c.relforcerowsecurity",(list(TABLES),))).fetchone()
    return row[0]==len(TABLES)

class HealthIncrementalService:
    def __init__(self,runtime,clock=None,enabled=None):
        mode=os.environ.get('COMPANION_HEALTH_INCREMENTAL','disabled')
        if mode not in ('enabled','disabled'):raise ValueError('Invalid Health incremental configuration')
        self.enabled=mode=='enabled' if enabled is None else enabled
        self.runtime=runtime;self.clock=clock or (lambda:datetime.now(timezone.utc));self.task=None
    async def status(self,service):
        async with service.lock:
            doc=service.document;epoch=doc['state']['data_epoch']
            source=next(s for s in doc['state']['sources'] if s['id']=='healthkit')
            now=self.clock().astimezone(timezone.utc);year=now.year-(now.month<=6);month=(now.month-7)%12+1
            start=now.replace(year=year,month=month,day=min(now.day,calendar.monthrange(year,month)[1]),hour=0,minute=0,second=0,microsecond=0)
            streams=[];supported=True;schema=False;pending=False
            coordinator=getattr(service,'pattern_coordinator',None)
            if coordinator:
                async with coordinator.transaction() as c:
                    schema=await ready(c)
                    if schema:
                        account=coordinator.store.account_id
                        streams=await (await c.execute('SELECT hk_type,stream_id,next_sequence,bootstrapped FROM companion_private.health_incremental_streams WHERE account_id=%s AND epoch=%s',(account,epoch))).fetchall()
                        pending=bool(await (await c.execute('SELECT 1 FROM companion_private.health_import_pages WHERE account_id=%s LIMIT 1',(account,))).fetchone())
                        supported=not bool(await (await c.execute("""SELECT 1 FROM companion_private.health_samples h JOIN companion_private.pattern_batches b USING(account_id,batch_id)
                            WHERE h.account_id=%s AND h.hk_type='stepCount' AND NOT h.deleted AND b.provenance='realHealthKit' AND b.origin='healthkit'
                            AND NOT EXISTS(SELECT 1 FROM companion_private.health_incremental_days d WHERE d.account_id=h.account_id AND d.epoch=%s AND d.day=(h.start_at AT TIME ZONE 'UTC')::date) LIMIT 1""",(account,epoch))).fetchone())
                        oldest=await (await c.execute("SELECT min(start_at) FROM companion_private.health_samples WHERE account_id=%s AND NOT deleted AND end_at>=%s",(account,now-timedelta(days=190)))).fetchone()
                        if oldest[0]:start=max(now.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=190),min(start,oldest[0]))
            allowed=source['enabled'] and source['consent']=='granted'
            message='Incremental Health is disabled. Existing records are preserved.'
            if self.enabled:
                message='Incremental Health is ready. Empty reads do not establish permission or erase records.'
                if not schema:message='Incremental Health requires the reviewed database migration. Collection is paused.'
                elif not supported:message='Existing real daily step totals need a reviewed UUID baseline migration. No account reset or data replacement was performed.'
                elif pending:message='Finish or explicitly discard the pending complete-window import before incremental cutover.'
                elif not allowed:message='Enable the Health source explicitly before collecting incremental records.'
            known={r[0]:r for r in streams}
            return HealthIncrementalStatus(data_epoch=epoch,enabled=self.enabled,schema_ready=schema,source_allowed=allowed,
                authorization_requested=source.get('authorization_requested',False),snapshot_allowed=not streams and not self.enabled,
                baseline_supported=supported and not pending,baseline_start=start.isoformat(),message=message,
                streams=[HealthStreamStatus(hk_type=t,stream_id=str(known[t][1]) if t in known else None,next_sequence=known[t][2] if t in known else 0,bootstrapped=known[t][3] if t in known else False) for t in TYPES])
    async def require(self,service):
        status=await self.status(service)
        if not status.enabled or not status.schema_ready:raise DomainError('not_configured',409,'Incremental Health is not enabled and ready.')
    async def retain(self,account):
        async with self.runtime.pool.connection() as c,c.transaction():
            await c.execute("SELECT set_config('companion.account_id',%s,true)",(str(account),))
            row=await (await c.execute('SELECT body FROM companion_private.snapshots WHERE account_id=%s',(account,))).fetchone()
            if row:await prune_health_incremental_history(c,str(account),row[0]['state']['data_epoch'],now=self.clock())
    async def start(self,worker=True):
        await self.runtime.open()
        async with self.runtime.pool.connection() as c:
            if not worker or not await ready(c):return
        async def loop():
            cursor='00000000-0000-0000-0000-000000000000'
            while True:
                try:
                    async with self.runtime.pool.connection() as c:
                        rows=await (await c.execute('SELECT account_id FROM companion_private.accounts WHERE account_id>%s ORDER BY account_id LIMIT 100',(cursor,))).fetchall()
                    for (account,) in rows:
                        try:await self.retain(account)
                        except asyncio.CancelledError:raise
                        except Exception:pass
                    cursor=str(rows[-1][0]) if len(rows)==100 else '00000000-0000-0000-0000-000000000000'
                except asyncio.CancelledError:raise
                except Exception:pass  # Retry bounded maintenance; never print account data.
                await asyncio.sleep(1 if cursor!='00000000-0000-0000-0000-000000000000' else 60)
        self.task=asyncio.create_task(loop())
    async def close(self):
        if self.task:self.task.cancel();await asyncio.gather(self.task,return_exceptions=True);self.task=None
