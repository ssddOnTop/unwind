"""PostgreSQL-only pattern repository, sharing the account persistence session and lock."""
from contextlib import asynccontextmanager
from copy import deepcopy
from . import banking_schedule as schedule
from uuid import uuid4
from psycopg.types.json import Jsonb
from .patterns import Aggregate, PatternOverview, aggregate, concerns, findings, empty_overview, model_review, VERSION
from .contracts import DomainError
import asyncio
import hashlib
import json
import os


def source_label(provenances):
    if 'synthetic' in provenances and 'providerSandbox' in provenances:
        return 'Synthetic Health records + Plaid Sandbox transactions'
    if 'synthetic' in provenances:
        return 'Operator demo — synthetic records, not Apple or Plaid provider data'
    if 'providerSandbox' in provenances:
        return 'Plaid Sandbox records — not live finances'
    return 'Normalized connected records — coverage is limited'


class PatternCoordinator:
    def __init__(self, service, persistence, model_factory=None):
        self.service,self.store=service,persistence
        if not hasattr(persistence,'pattern_active_runs'):persistence.pattern_active_runs=set()
        if not hasattr(persistence,'pattern_request_lock'):persistence.pattern_request_lock=asyncio.Lock()
        if model_factory is None:
            from .connections.llm import create_chat_model
            model_factory=create_chat_model
        self.model_factory=model_factory

    @asynccontextmanager
    async def transaction(self):
        p=self.store
        async with p.lock:
            await p._connect()
            async with p.conn.transaction():
                await p.conn.execute("SELECT set_config('companion.account_id',%s,true)",(p.account_id,))
                yield p.conn

    async def _recover_orphans(self, conn):
        # transaction() first acquires/verifies the lifetime exclusive account advisory lock.
        # A second process cannot enter; a sibling coordinator sharing this session must also
        # observe the in-memory active set. Never recover a currently executing invocation.
        if self.store.pattern_active_runs:return
        if conn is not self.store.conn or conn.closed or conn.broken:raise RuntimeError('Exclusive session unavailable')
        await conn.execute("UPDATE companion_private.pattern_runs SET status='unavailable',engine='deterministic' WHERE account_id=%s AND status='running'",(self.store.account_id,))

    async def _view(self, conn, row):
        if not row: return empty_overview()
        run_id,status,engine,at,evidence,is_demo=row
        ids=await (await conn.execute('SELECT concern_id,attribution FROM companion_private.pattern_insights WHERE account_id=%s AND run_id=%s ORDER BY rank', (self.store.account_id,run_id))).fetchall()
        dto=Aggregate.model_validate(evidence)
        cards=findings([r[0] for r in ids],dto)
        for card,entry in zip(cards,ids):
            card.attribution=entry[1]
            if entry[1]=='not_for_me':
                card.explanation='You reported these records were not mostly about you. No personal interpretation is made; the recorded pattern is retained for transparency.'
        brief=('SYNTHETIC HEALTH DEMO — not medical evidence.\n' if is_demo else '')+'Patterns worth reviewing\n'
        brief+='\n'.join(c.title+': '+c.explanation+' '+c.evidence_summary for c in cards if c.attribution!='not_for_me')
        brief+='\nOptional question for a clinician: Could changes in my routine or recording coverage help explain these observations? These records do not establish a diagnosis, diet quality or causality.'
        label=await (await conn.execute('SELECT source_label,brief_override FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s',(self.store.account_id,run_id))).fetchone()
        if label[1] is not None:
            brief=('SYNTHETIC HEALTH DEMO — not medical evidence.\n' if is_demo else '')+'User-edited notes (not AI conclusions):\n'+label[1]
        eligible=await (await conn.execute("SELECT EXISTS(SELECT 1 FROM companion_private.pattern_batches b WHERE b.account_id=%s AND b.active AND (EXISTS(SELECT 1 FROM companion_private.health_samples h WHERE h.account_id=b.account_id AND h.batch_id=b.batch_id AND NOT h.deleted) OR EXISTS(SELECT 1 FROM companion_private.pattern_transactions t WHERE t.account_id=b.account_id AND t.batch_id=b.batch_id AND NOT t.deleted)))",(self.store.account_id,))).fetchone()
        return PatternOverview(bank_refresh={'status':'not_requested','last_updated':schedule.last_full_success(self.service.document) if hasattr(self.service,'document') else None,'message':'Using saved bank data; no bank fetch requested.'},doctor_brief=brief,status=status,run_id=str(run_id),analyzed_at=at.isoformat(),engine=engine,is_demo=is_demo,can_analyze=eligible[0],
            source_label=label[0],
            evidence=dto,findings=cards).model_dump()

    async def get(self):
        async with self.service.lock:
            async with self.transaction() as c:
                await self._recover_orphans(c)
                row=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s ORDER BY analyzed_at DESC LIMIT 1',(self.store.account_id,))).fetchone()
                if row: return await self._view(c,row)
                value=empty_overview()
                batches=await (await c.execute('SELECT provenance,origin FROM companion_private.pattern_batches WHERE account_id=%s AND active',(self.store.account_id,))).fetchall()
                if batches:
                    value['can_analyze']=True
                    value['is_demo']=any(r[0]=='synthetic' for r in batches)
                    value['source_label']=source_label({r[0] for r in batches})
                    if value['is_demo'] and any(r[1]=='healthkit' for r in batches):value['source_label']='Synthetic HealthKit helper records — read through this app’s Health permissions' + (' + Plaid Sandbox transactions' if any(r[0]=='providerSandbox' for r in batches) else '')
                return value

    async def clarify(self, body):
        async with self.service.lock:
            async with self.transaction() as c:
                await self._recover_orphans(c)
                row=await (await c.execute('UPDATE companion_private.pattern_insights SET attribution=%s WHERE account_id=%s AND run_id=%s AND concern_id=%s RETURNING run_id',(body.attribution,self.store.account_id,body.run_id,body.concern_id))).fetchone()
                if not row: raise DomainError('invalid_request',409,'This pattern is no longer available.')
                await c.execute('UPDATE companion_private.pattern_runs SET brief_override=NULL WHERE account_id=%s AND run_id=%s',(self.store.account_id,body.run_id))
                run=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s',(self.store.account_id,body.run_id))).fetchone()
                return await self._view(c,run)

    async def save_brief(self, body):
        async with self.service.lock:
            async with self.transaction() as c:
                row=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s',(self.store.account_id,body.run_id))).fetchone()
                if not row:raise DomainError('invalid_request',409,'This review is no longer available.')
                view=await self._view(c,row)
                existing=await (await c.execute('SELECT brief_override FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s',(self.store.account_id,body.run_id))).fetchone()
                if body.expected_brief!=view['doctor_brief'] and body.text!=existing[0]:raise DomainError('stale_experience',409,'The brief changed. Refresh before editing.')
                await c.execute('UPDATE companion_private.pattern_runs SET brief_override=%s WHERE account_id=%s AND run_id=%s',(body.text,self.store.account_id,body.run_id))
                return await self._view(c,row)

    async def execute(self, body):
        """One durable user action: optional explicit bank refresh, then analysis.

        A reservation precedes every external effect. Interrupted actions are
        terminalized without replaying bank or model calls. A fresh request is
        needed for a genuinely new user-authorized refresh attempt.
        """
        service=self.service;lock=self.store.pattern_request_lock
        if lock.locked():raise DomainError('busy',409,'A pattern action is already running.')
        async with lock:
            payload=body.model_dump();epoch=body.data_epoch
            async with service.lock:
                if service.document['state']['data_epoch']!=epoch:raise DomainError('epoch_mismatch',409)
                receipt=service.receipt('/patterns/v1/analyze',payload)
                cached=await service.registry.dispatch('persistence','receipt',receipt)
                if cached and cached['digest']!=receipt['digest']:raise DomainError('idempotency_conflict',409)
                if cached and cached['response'] is not None:return cached['response']
                interrupted=bool(cached)
                if not interrupted:
                    receipt['preserve_response']=True
                    await service.commit(deepcopy(service.document),receipt,bump=False)
            refresh={'status':'not_requested','last_updated':schedule.last_full_success(service.document),'message':'Using saved bank data; no bank fetch requested.'}
            should_analyze=not interrupted
            if interrupted:
                refresh.update(status='failed',message='The previous action response expired or was interrupted. Bank and AI calls were not repeated; start a new action to retry.')
            elif body.fetch_latest_bank_data:
                bank=service.banking_coordinator
                refresh.update(status='unavailable',message='No eligible linked bank is available. Analysis was not run.')
                should_analyze=False
                if bank is not None:
                    try:
                        async with service.lock:
                            if service.document['state']['data_epoch']!=epoch:raise DomainError('epoch_mismatch',409)
                            doc=service.document;items=(await bank.call('describe',bank.payload(doc)))['items']
                            eligible=any(i['connected'] and i['eligible'] for i in items.values())
                            request={'schema_version':'1','request_id':'pattern-bank:'+body.request_id,'data_epoch':epoch,
                                     'expected_revision':doc['state']['revision'],'action':'sync','connection_id':None}
                        if eligible:
                            response=await bank.action(request,eligible_only=True)
                            should_analyze=response['result']['status']=='complete'
                            refresh.update(status='complete' if should_analyze else 'partial',
                                message='Bank refresh committed before this analysis.' if should_analyze else 'Bank refresh was incomplete. Analysis was not run; saved findings may be older.')
                    except asyncio.CancelledError:raise
                    except Exception:
                        refresh.update(status='failed',message='Bank refresh failed. Analysis was not run; saved findings may be older.')
                refresh['last_updated']=schedule.last_full_success(service.document)
            value=await self.analyze(body.request_id,expected_epoch=epoch) if should_analyze else await self.get()
            value['bank_refresh']=refresh
            async with service.lock:
                if service.document['state']['data_epoch']!=epoch:raise DomainError('epoch_mismatch',409)
                receipt.update(response=None if interrupted else value,preserve_response=True)
                await service.commit(deepcopy(service.document),receipt,bump=False)
            return value

    async def analyze(self, request_id, expected_epoch=None):
        # Serializes admission with account erasure, source changes and native double taps.
        async with self.service.lock:
            if expected_epoch is not None and self.service.document['state']['data_epoch']!=expected_epoch:raise DomainError('epoch_mismatch',409)
            account=self.store.account_id
            async with self.transaction() as c:
                await self._recover_orphans(c)
                row=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s AND run_id IN (SELECT run_id FROM companion_private.pattern_requests WHERE account_id=%s AND request_id=%s)',(account,account,request_id))).fetchone()
                if row: return await self._view(c,row)
                rows=await (await c.execute("SELECT h.hk_type,h.start_at,h.end_at,h.timezone,h.value,h.category FROM companion_private.health_samples h JOIN companion_private.pattern_batches b USING(account_id,batch_id) WHERE h.account_id=%s AND b.active AND NOT h.deleted AND h.end_at>=now()-interval '190 days' AND (b.provenance='synthetic' OR NOT EXISTS(SELECT 1 FROM companion_private.pattern_batches sb WHERE sb.account_id=h.account_id AND sb.active AND sb.provenance='synthetic' AND EXISTS(SELECT 1 FROM companion_private.health_samples sh WHERE sh.account_id=sb.account_id AND sh.batch_id=sb.batch_id AND NOT sh.deleted)))",(account,))).fetchall()
                samples=[dict(zip(('hk_type','start_at','end_at','timezone','value','category'),r)) for r in rows]
                rows=await (await c.execute('SELECT t.event_at,t.category,t.amount FROM companion_private.pattern_transactions t JOIN companion_private.pattern_batches b USING(account_id,batch_id) WHERE t.account_id=%s AND b.active AND NOT t.deleted',(account,))).fetchall()
                tx=[dict(zip(('event_at','category','amount'),r)) for r in rows]
                if not samples and not tx: raise DomainError('invalid_request',409,'No eligible records are available. Refresh permitted Health records and sync connected bank accounts first.')
                dto=aggregate(samples,tx,self.service.clock())
                batches=await (await c.execute('SELECT provenance FROM companion_private.pattern_batches WHERE account_id=%s AND active',(account,))).fetchall()
                demo=any(r[0]=='synthetic' for r in batches)
                # Model choice participates in the local cache key but never the model prompt as user identity.
                version=VERSION+':'+os.environ.get('COMPANION_LLM_MODEL','unconfigured')[:200]
                digest=hashlib.sha256(json.dumps({'aggregate':dto.model_dump(),'provenance':sorted(r[0] for r in batches)},sort_keys=True).encode()).hexdigest()
                row=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s AND input_digest=%s AND input_version=%s',(account,digest,version))).fetchone()
                if row:
                    await c.execute('INSERT INTO companion_private.pattern_requests(account_id,request_id,run_id) VALUES(%s,%s,%s)',(account,request_id,row[0]))
                    return await self._view(c,row)
                run=str(uuid4()); ids=concerns(dto)
                origins=await (await c.execute('SELECT origin FROM companion_private.pattern_batches WHERE account_id=%s AND active',(account,))).fetchall()
                label=source_label({r[0] for r in batches})
                if demo and any(r[0]=='healthkit' for r in origins):label='Synthetic HealthKit helper records (real Health excluded from this demo comparison)' + (' + Plaid Sandbox transactions' if any(r[0]=='providerSandbox' for r in batches) else '')
                await c.execute("INSERT INTO companion_private.pattern_runs(account_id,run_id,request_id,input_digest,input_version,status,engine,evidence,is_demo,source_label) VALUES(%s,%s,%s,%s,%s,'running','deterministic',%s,%s,%s)",(account,run,request_id,digest,version,Jsonb(dto.model_dump()),demo,label))
                await c.execute('INSERT INTO companion_private.pattern_requests(account_id,request_id,run_id) VALUES(%s,%s,%s)',(account,request_id,run))
                for rank,i in enumerate(ids):
                    await c.execute('INSERT INTO companion_private.pattern_insights(account_id,run_id,concern_id,rank) VALUES(%s,%s,%s,%s)',(account,run,i,rank))
            # No transaction is held across network I/O. The durable reservation forbids transport retry calls.
            self.store.pattern_active_runs.add(run)
            try:
                status,engine='unavailable','deterministic'
                cancelled=False
                try:
                    from langsmith import tracing_context
                    with tracing_context(enabled=False):
                        ids=await model_review(dto,ids,self.model_factory)
                    status,engine='complete','LLMassisted' if ids else 'deterministic'
                except asyncio.CancelledError:
                    cancelled=True
                except Exception:
                    pass  # Never log private provider payloads or prompts.
                async with self.transaction() as c:
                    await c.execute('UPDATE companion_private.pattern_runs SET status=%s,engine=%s WHERE account_id=%s AND run_id=%s',(status,engine,account,run))
                    # Remove old ranks before inserting the validated permutation.
                    await c.execute('DELETE FROM companion_private.pattern_insights WHERE account_id=%s AND run_id=%s',(account,run))
                    for rank,i in enumerate(ids):
                        await c.execute('INSERT INTO companion_private.pattern_insights(account_id,run_id,concern_id,rank) VALUES(%s,%s,%s,%s)',(account,run,i,rank))
                    row=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s',(account,run))).fetchone()
                    result=await self._view(c,row)
                if cancelled: raise asyncio.CancelledError
                return result
            finally:
                self.store.pattern_active_runs.discard(run)
