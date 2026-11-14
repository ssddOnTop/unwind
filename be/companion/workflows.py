"""Private UTC queue: one nightly job per account/epoch/date, bounded backend worker."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime,timezone,timedelta
import hashlib,json
from uuid import UUID,uuid4,uuid5,NAMESPACE_URL
from .contracts import DomainError
from .workflow_contracts import eligible,consent_key,WorkflowJob,overview

MESSAGES={'queued':'Review queued.','running':'Review in progress; bank data respects its daily limit.',
 'complete':'Review finished. Unchanged summaries may reuse a saved review.',
 'unavailable':'AI review unavailable or execution interrupted. Saved deterministic findings remain available.',
 'cancelled':'Review cancelled because account or source consent changed.',
 'failed':'Review could not start. No automatic external-call retry was made.'}

class WorkflowQueue:
    def __init__(self,runtime,config,clock=None):
        self.runtime,self.config=runtime,config
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.cursor=None

    @asynccontextmanager
    async def scoped(self,account):
        await self.runtime.open()
        async with self.runtime.pool.connection() as c:
            async with c.transaction():
                await c.execute("SELECT set_config('companion.account_id',%s,true)",(str(UUID(account)),))
                yield c

    async def check(self):
        await self.runtime.open()
        async with self.runtime.pool.connection() as c:
            row=await (await c.execute('SELECT version FROM companion_private.schema_version')).fetchone()
            if row!=(4,):raise RuntimeError('Explicit workflow migration required')

    async def accounts(self,cursor_name="cursor"):
        await self.runtime.open()
        async with self.runtime.pool.connection() as c:
            rows=await (await c.execute('SELECT account_id FROM companion_private.accounts WHERE account_id>%s ORDER BY account_id LIMIT 100',(getattr(self,cursor_name,None) or UUID(int=0),))).fetchall()
        setattr(self,cursor_name,rows[-1][0] if len(rows)==100 else None)
        return [str(r[0]) for r in rows]

    async def enqueue(self,account,request_id,epoch,origin='self'):
        if not self.config.enabled:raise DomainError('not_configured',409)
        if origin=='self' and account not in self.config.manual_accounts:raise DomainError('consent_required',403,'Manual review is not available for this account.')
        if origin not in {'self','nightly','operator'}:raise ValueError('Invalid trusted origin')
        request_id=str(UUID(request_id));now=self.clock().astimezone(timezone.utc)
        digest=hashlib.sha256(json.dumps({'epoch':epoch,'request_id':request_id,'origin':origin},sort_keys=True).encode()).hexdigest()
        async with self.scoped(account) as c:
            await c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",('pattern-enqueue:'+account,))
            row=await (await c.execute('SELECT body FROM companion_private.snapshots WHERE account_id=%s FOR SHARE',(account,))).fetchone()
            if not row or row[0]['state']['data_epoch']!=epoch:raise DomainError('epoch_mismatch',409)
            if not eligible(row[0]):raise DomainError('consent_required',403,'Nightly review requires cloud consent and a permitted source.')
            existing=await (await c.execute('SELECT digest FROM companion_private.pattern_jobs WHERE account_id=%s AND epoch=%s AND request_id=%s',(account,epoch,request_id))).fetchone()
            if existing:
                if existing[0]!=digest:raise DomainError('idempotency_conflict',409)
                return
            if origin!='nightly':
                pending=await (await c.execute("SELECT count(*) FROM companion_private.pattern_jobs WHERE account_id=%s AND epoch=%s AND status IN ('queued','running')",(account,epoch))).fetchone()
                if pending[0]>=8:raise DomainError('busy',409,'Pending review queue is full.')
            await c.execute('''INSERT INTO companion_private.pattern_jobs(account_id,epoch,job_id,request_id,utc_date,origin,digest,available_at,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (account,epoch,str(uuid4()),request_id,now.date(),origin,digest,now,now,now))

    async def scan(self):
        """Catch up CURRENT UTC date only; never enqueue a backlog of missed dates."""
        if not self.config.enabled:return []
        accounts=await self.accounts();day=self.clock().astimezone(timezone.utc).date().isoformat()
        for account in accounts:
            async with self.scoped(account) as c:
                row=await (await c.execute('SELECT body FROM companion_private.snapshots WHERE account_id=%s',(account,))).fetchone()
            if row and eligible(row[0]):
                epoch=row[0]['state']['data_epoch'];request=str(uuid5(NAMESPACE_URL,'nightly:'+account+':'+epoch+':'+day))
                try:await self.enqueue(account,request,epoch,'nightly')
                except DomainError:pass  # consent/epoch changed between scoped reads
        return accounts

    async def latest(self,account,epoch):
        if not self.config.enabled:return None
        async with self.scoped(account) as c:
            row=await (await c.execute("SELECT job_id,utc_date,status,bank_status,updated_at,reason FROM companion_private.pattern_jobs WHERE account_id=%s AND epoch=%s ORDER BY (status IN ('queued','running')) DESC,updated_at DESC,created_at DESC,job_id DESC LIMIT 1",(account,epoch))).fetchone()
        return WorkflowJob(id=str(row[0]),utc_date=row[1].isoformat(),status=row[2],message={'superseded':'A newer UTC day superseded this queued review.','expired':'This queued review expired before execution.'}.get(row[5],MESSAGES[row[2]]),bank_status=row[3],updated_at=row[4].isoformat(),bank_message={'not_checked':'Bank refresh has not been checked.','saved':'Using saved bank data; a refresh was not due or was already in progress.','refreshed':'Due bank data was refreshed before this review.','partial':'Bank refresh was incomplete; available data may be older.','unavailable':'Bank data could not be refreshed.'}[row[3]]) if row else None

    async def claim(self,account):
        now=self.clock()
        async with self.scoped(account) as c:
            # Serialize claims for an account independently from its long-lived persistence session.
            locked=await (await c.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",('pattern-workflow:'+account,))).fetchone()
            if not locked[0]:return None
            await c.execute("UPDATE companion_private.pattern_jobs SET status='unavailable',reason='interrupted',updated_at=%s,lease_token=NULL,lease_until=NULL WHERE account_id=%s AND status='running' AND lease_until<=%s",(now,account,now))
            state=await (await c.execute('SELECT body FROM companion_private.snapshots WHERE account_id=%s FOR SHARE',(account,))).fetchone()
            if not state:return None
            epoch=state[0]['state']['data_epoch']
            await c.execute("UPDATE companion_private.pattern_jobs SET status='cancelled',reason='superseded',updated_at=%s WHERE account_id=%s AND status='queued' AND origin='nightly' AND utc_date<%s",(now,account,now.astimezone(timezone.utc).date()))
            await c.execute("UPDATE companion_private.pattern_jobs SET status='cancelled',reason='expired',updated_at=%s WHERE account_id=%s AND status='queued' AND origin<>'nightly' AND created_at<%s",(now,account,now-timedelta(days=1)))
            if not eligible(state[0]):
                await c.execute("UPDATE companion_private.pattern_jobs SET status='cancelled',reason='consent',updated_at=%s WHERE account_id=%s AND status='queued'",(now,account));return None
            active=await (await c.execute("SELECT 1 FROM companion_private.pattern_jobs WHERE account_id=%s AND status='running'",(account,))).fetchone()
            if active:return None
            row=await (await c.execute("SELECT job_id,attempts FROM companion_private.pattern_jobs WHERE account_id=%s AND epoch=%s AND status='queued' AND available_at<=%s AND attempts<3 ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1",(account,epoch,now))).fetchone()
            if not row:return None
            token=str(uuid4())
            await c.execute("UPDATE companion_private.pattern_jobs SET status='running',reason='running',attempts=attempts+1,lease_token=%s,lease_until=%s,updated_at=%s WHERE account_id=%s AND job_id=%s",(token,now+timedelta(minutes=5),now,account,row[0]))
            return {'account_id':account,'epoch':epoch,'job_id':str(row[0]),'token':token,'attempts':row[1]+1,'consent':consent_key(state[0])}

    async def current(self,job):
        async with self.scoped(job['account_id']) as c:
            row=await (await c.execute("SELECT s.body FROM companion_private.snapshots s JOIN companion_private.pattern_jobs j ON j.account_id=s.account_id WHERE s.account_id=%s AND j.epoch=%s AND j.job_id=%s AND j.status='running' AND j.lease_token=%s AND j.lease_until>%s",(job['account_id'],job['epoch'],job['job_id'],job['token'],self.clock()))).fetchone()
            return bool(row and row[0]['state']['data_epoch']==job['epoch'] and eligible(row[0]) and consent_key(row[0])==job['consent'])

    async def finish(self,job,status,bank='not_checked',run_id=None,retry=False):
        now=self.clock()
        if retry:status='queued' if job['attempts']<3 else 'failed'
        async with self.scoped(job['account_id']) as c:
            await c.execute('''UPDATE companion_private.pattern_jobs SET status=%s,reason=%s,bank_status=%s,run_id=%s,updated_at=%s,
                available_at=%s,lease_token=NULL,lease_until=NULL WHERE account_id=%s AND epoch=%s AND job_id=%s AND lease_token=%s''',
                (status,status,bank,run_id,now,now+timedelta(seconds=30*job['attempts']) if retry else now,job['account_id'],job['epoch'],job['job_id'],job['token']))

    async def view(self,account,service):
        report=await service.pattern_coordinator.get()
        return overview(self.config,account,service.document,self.clock(),report,await self.latest(account,service.document['state']['data_epoch']))

class WorkflowWorker:
    def __init__(self,queue,accounts):self.queue,self.accounts=queue,accounts;self.tasks=[]
    async def run(self,job):
        from .accounts import AccountError
        started=False;bank_status='not_checked'
        try:
            async with self.accounts.borrow_account(job['account_id'],temporary=True) as handle:
                service=handle.service.app.state.service
                if not await self.queue.current(job):await self.queue.finish(job,'cancelled');return
                started=True
                async def review():
                    nonlocal bank_status
                    if service.banking_coordinator:
                        bank=service.banking_coordinator
                        while True:
                            while bank.busy:await asyncio.sleep(0.1)
                            response=await bank.daily_sync()
                            if not bank.busy:break
                        bank_status='saved' if not response or response.get('deferred_until') else 'refreshed' if response.get('result',{}).get('status')=='complete' else 'partial'
                        if response is None and any(v.get('failures',0) for v in service.document.get('meta',{}).get('banking_fetch_schedule',{}).values()):bank_status='unavailable'
                    else:bank_status='saved'
                    if not await self.queue.current(job):raise DomainError('consent_required',403)
                    value=await service.pattern_coordinator.analyze(job['job_id'],expected_epoch=job['epoch'],require_consent=True,expected_consent=job['consent'])
                    return value
                pending=asyncio.create_task(review())
                try:
                    async with asyncio.timeout(180):
                        while not pending.done():
                            await asyncio.wait({pending},timeout=1)
                            if not await self.queue.current(job):pending.cancel();raise DomainError('consent_required',403)
                        value=await pending
                    await self.queue.finish(job,'complete' if value['status']=='complete' else 'unavailable',bank_status,value.get('run_id'))
                finally:
                    if not pending.done():pending.cancel()
                    await asyncio.gather(pending,return_exceptions=True)
        except asyncio.CancelledError:
            await asyncio.shield(self.queue.finish(job,'unavailable',bank_status));raise
        except AccountError:
            await self.queue.finish(job,'unavailable',bank_status,retry=not started)
        except DomainError:
            await self.queue.finish(job,'cancelled' if not await self.queue.current(job) else 'unavailable',bank_status)
        except Exception:
            await self.queue.finish(job,'unavailable',bank_status)  # no error/prompt/provider logging

    async def tick(self):
        accounts=await self.queue.scan()
        for account in accounts:
            job=await self.queue.claim(account)
            if job:await self.run(job)
    async def start(self):
        await self.queue.check()
        async def schedule_loop():
            while True:
                try:await self.queue.scan()
                except asyncio.CancelledError:raise
                except Exception:pass
                await asyncio.sleep(0 if self.queue.cursor is not None else 10)
        async def work_loop():
            while True:
                try:
                    for account in await self.queue.accounts('_worker_cursor'):
                        job=await self.queue.claim(account)
                        if job:await self.run(job)
                except asyncio.CancelledError:raise
                except Exception:pass
                await asyncio.sleep(2)
        self.tasks=[asyncio.create_task(schedule_loop()),asyncio.create_task(work_loop())]
    async def close(self):
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True);self.tasks=[]
