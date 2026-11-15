"""PostgreSQL-only pattern repository, sharing the account persistence session and lock."""
from contextlib import asynccontextmanager
from copy import deepcopy
from . import banking_schedule as schedule
from uuid import uuid4
from psycopg.types.json import Jsonb
from .patterns import Aggregate, Finding, PatternOverview, area_results, aggregate, concerns, findings, empty_overview, model_review, VERSION
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


async def current_evidence(conn, account, now):
    """Shared eligible-source aggregation; no model or runtime effects."""
    rows=await (await conn.execute("SELECT h.hk_type,h.start_at,h.end_at,h.timezone,h.value,h.category FROM companion_private.health_samples h JOIN companion_private.pattern_batches b USING(account_id,batch_id) WHERE h.account_id=%s AND b.active AND NOT h.deleted AND h.end_at>=now()-interval '190 days' AND (b.provenance='synthetic' OR NOT EXISTS(SELECT 1 FROM companion_private.pattern_batches sb WHERE sb.account_id=h.account_id AND sb.active AND sb.provenance='synthetic' AND EXISTS(SELECT 1 FROM companion_private.health_samples sh WHERE sh.account_id=sb.account_id AND sh.batch_id=sb.batch_id AND NOT sh.deleted)))",(account,))).fetchall()
    samples=[dict(zip(('hk_type','start_at','end_at','timezone','value','category'),r)) for r in rows]
    rows=await (await conn.execute('SELECT t.event_at,t.category,t.amount FROM companion_private.pattern_transactions t JOIN companion_private.pattern_batches b USING(account_id,batch_id) WHERE t.account_id=%s AND b.active AND NOT t.deleted',(account,))).fetchall()
    tx=[dict(zip(('event_at','category','amount'),r)) for r in rows]
    dto=aggregate(samples,tx,now)
    return dto


class PatternCoordinator:
    def __init__(self, service, persistence, model_factory=None):
        self.service,self.store=service,persistence
        if not hasattr(persistence,'pattern_active_runs'):persistence.pattern_active_runs=set()
        if not hasattr(persistence,'pattern_request_lock'):persistence.pattern_request_lock=asyncio.Lock()
        if not hasattr(persistence,'pattern_analysis_lock'):persistence.pattern_analysis_lock=asyncio.Lock()
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
        saved=await (await conn.execute('SELECT assessment,source_label,brief_override FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s',(self.store.account_id,run_id))).fetchone()
        dto=Aggregate.model_validate(evidence)
        snapshot=saved[0]
        cards=[Finding.model_validate(v) for v in snapshot['findings']]
        revisions=await (await conn.execute('SELECT revision,kind,concern_id,value FROM companion_private.pattern_revisions WHERE account_id=%s AND run_id=%s ORDER BY revision',(self.store.account_id,run_id))).fetchall()
        notes=saved[2]
        for _,kind,area,value in revisions:
            if kind=='notes': notes=value
            else:
                notes=None
                for card in cards:
                    if card.concern_id==area: card.attribution=value
        cards=[card for card in cards if card.attribution!='not_for_me']
        areas=deepcopy(snapshot['areas'])
        for area in areas:
            if area['state']=='concern' and not any(c.concern_id==area['area'] for c in cards):
                area.update(state='insufficient_data',summary='You said these records were not about you. No personal assessment is made.')
        brief=('SYNTHETIC HEALTH DEMO — not medical evidence.\n' if is_demo else '')+'Current assessment\n'
        brief+='\n'.join(a['summary'] for a in areas)
        if notes is not None: brief=('SYNTHETIC HEALTH DEMO — not medical evidence.\n' if is_demo else '')+'User-edited notes (not AI conclusions):\n'+notes
        latest=await (await conn.execute("SELECT run_id FROM companion_private.pattern_runs WHERE account_id=%s ORDER BY analyzed_at DESC,sequence DESC LIMIT 1",(self.store.account_id,))).fetchone()
        is_current=bool(latest and latest[0]==run_id and status!='running')
        actionable=sorted(c.concern_id for c in cards) if is_current else []
        key=hashlib.sha256(json.dumps(actionable).encode()).hexdigest() if actionable else None
        eligible=await (await conn.execute("SELECT EXISTS(SELECT 1 FROM companion_private.pattern_batches b WHERE b.account_id=%s AND b.active AND (EXISTS(SELECT 1 FROM companion_private.health_samples h WHERE h.account_id=b.account_id AND h.batch_id=b.batch_id AND NOT h.deleted) OR EXISTS(SELECT 1 FROM companion_private.pattern_transactions t WHERE t.account_id=b.account_id AND t.batch_id=b.batch_id AND NOT t.deleted)))",(self.store.account_id,))).fetchone()
        return PatternOverview(bank_refresh={'status':'not_requested','last_updated':schedule.last_full_success(self.service.document) if hasattr(self.service,'document') else None,'message':'Using saved bank data; no bank fetch requested.'},doctor_brief=brief,status=status,run_id=str(run_id),analyzed_at=at.isoformat(),engine=engine,is_demo=is_demo,can_analyze=eligible[0],
            source_label=saved[1], areas=areas, current_run_id=str(latest[0]) if latest else None, is_current=is_current,
            actionable_area_ids=actionable, actionability_key=key, revision=revisions[-1][0] if revisions else 0,
            evidence=dto,findings=cards if status!='running' else []).model_dump()

    async def get(self):
        async with self.service.lock:
            async with self.transaction() as c:
                await self._recover_orphans(c)
                row=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s ORDER BY analyzed_at DESC,sequence DESC LIMIT 1',(self.store.account_id,))).fetchone()
                if row: return await self._view(c,row)
                value=empty_overview()
                batches=await (await c.execute('SELECT provenance,origin FROM companion_private.pattern_batches WHERE account_id=%s AND active',(self.store.account_id,))).fetchall()
                if batches:
                    value['can_analyze']=True
                    value['is_demo']=any(r[0]=='synthetic' for r in batches)
                    value['source_label']=source_label({r[0] for r in batches})
                    if value['is_demo'] and any(r[1]=='healthkit' for r in batches):value['source_label']='Synthetic HealthKit helper records — read through this app’s Health permissions' + (' + Plaid Sandbox transactions' if any(r[0]=='providerSandbox' for r in batches) else '')
                return value

    async def notification_candidate(self, expected_run_id, expected_actionability_key):
        """Read-only send-time fence for the notification owner; never sends anything.

        Compare both fields immediately before delivery. A newer run invalidates
        pending work even if it has the same concerns; the stable key supports
        unchanged-repeat suppression in the owner's durable delivery history.
        Corrections can change the key without changing the assessment run ID.
        """
        current = await self.get()
        return {
            'current_run_id': current['current_run_id'],
            'revision': current['revision'],
            'actionability_key': current['actionability_key'],
            'actionable_area_ids': current['actionable_area_ids'],
            'eligible': bool(current['is_current'] and current['actionable_area_ids']
                and current['run_id'] == expected_run_id
                and current['actionability_key'] == expected_actionability_key),
        }

    async def clarify(self, body):
        async with self.service.lock:
            async with self.transaction() as c:
                await self._recover_orphans(c)
                row=await (await c.execute("SELECT 1 FROM companion_private.pattern_insights i JOIN companion_private.pattern_runs r USING(account_id,run_id) WHERE i.account_id=%s AND i.run_id=%s AND i.concern_id=%s AND r.status<>'running'",(self.store.account_id,body.run_id,body.concern_id))).fetchone()
                if not row: raise DomainError('invalid_request',409,'This pattern is no longer available.')
                await c.execute("INSERT INTO companion_private.pattern_revisions(account_id,run_id,kind,concern_id,value) VALUES(%s,%s,'attribution',%s,%s)",(self.store.account_id,body.run_id,body.concern_id,body.attribution))
                run=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s',(self.store.account_id,body.run_id))).fetchone()
                return await self._view(c,run)

    async def save_brief(self, body):
        async with self.service.lock:
            async with self.transaction() as c:
                row=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s',(self.store.account_id,body.run_id))).fetchone()
                if not row or row[1]=='running':raise DomainError('invalid_request',409,'This review is no longer available.')
                view=await self._view(c,row)
                if body.expected_brief!=view['doctor_brief']:raise DomainError('stale_experience',409,'The brief changed. Refresh before editing.')
                await c.execute("INSERT INTO companion_private.pattern_revisions(account_id,run_id,kind,value) VALUES(%s,%s,'notes',%s)",(self.store.account_id,body.run_id,body.text))
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

    async def analyze(self, request_id, expected_epoch=None, require_consent=False, expected_consent=None):
        # Serialize reviews without blocking consent changes or account deletion during I/O.
        async with self.store.pattern_analysis_lock:
            return await self._analyze(request_id,expected_epoch,require_consent,expected_consent)

    async def _analyze(self, request_id, expected_epoch=None, require_consent=False, expected_consent=None):
        # Serializes admission with account erasure, source changes and native double taps.
        async with self.service.lock:
            if expected_epoch is not None and self.service.document['state']['data_epoch']!=expected_epoch:raise DomainError('epoch_mismatch',409)
            if require_consent:
                from .workflow_contracts import eligible,consent_key
                if not eligible(self.service.document) or expected_consent is not None and consent_key(self.service.document)!=expected_consent:raise DomainError('consent_required',403)
            account=self.store.account_id
            async with self.transaction() as c:
                await self._recover_orphans(c)
                row=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s AND run_id IN (SELECT run_id FROM companion_private.pattern_requests WHERE account_id=%s AND request_id=%s)',(account,account,request_id))).fetchone()
                if row: return await self._view(c,row)
                dto=await current_evidence(c,account,self.service.clock())
                batches=await (await c.execute('SELECT provenance FROM companion_private.pattern_batches WHERE account_id=%s AND active',(account,))).fetchall()
                demo=any(r[0]=='synthetic' for r in batches)
                # Model choice participates in the local cache key but never the model prompt as user identity.
                version=VERSION+':'+os.environ.get('COMPANION_LLM_MODEL','unconfigured')[:200]
                digest=hashlib.sha256(json.dumps({'aggregate':dto.model_dump(),'provenance':sorted(r[0] for r in batches)},sort_keys=True).encode()).hexdigest()
                row=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s AND input_digest=%s AND input_version=%s ORDER BY analyzed_at DESC,sequence DESC LIMIT 1',(account,digest,version))).fetchone()
                if row:
                    latest=await (await c.execute('SELECT run_id FROM companion_private.pattern_runs WHERE account_id=%s ORDER BY analyzed_at DESC,sequence DESC LIMIT 1',(account,))).fetchone()
                    if latest[0]!=row[0]:
                        # A -> B -> A is a new result, with cached model output and no extra call.
                        previous=row[0];new_run=str(uuid4())
                        await c.execute("INSERT INTO companion_private.pattern_runs(account_id,run_id,request_id,input_digest,input_version,status,engine,evidence,is_demo,source_label,assessment) SELECT account_id,%s,%s,input_digest,input_version,status,engine,evidence,is_demo,source_label,assessment FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s",(new_run,request_id,account,previous))
                        await c.execute('INSERT INTO companion_private.pattern_insights(account_id,run_id,concern_id,rank) SELECT account_id,%s,concern_id,rank FROM companion_private.pattern_insights WHERE account_id=%s AND run_id=%s',(new_run,account,previous))
                        row=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s',(account,new_run))).fetchone()
                    await c.execute('INSERT INTO companion_private.pattern_requests(account_id,request_id,run_id) VALUES(%s,%s,%s)',(account,request_id,row[0]))
                    return await self._view(c,row)
                run=str(uuid4()); ids=concerns(dto)
                origins=await (await c.execute('SELECT origin FROM companion_private.pattern_batches WHERE account_id=%s AND active',(account,))).fetchall()
                label=source_label({r[0] for r in batches})
                if demo and any(r[0]=='healthkit' for r in origins):label='Synthetic HealthKit helper records (real Health excluded from this demo comparison)' + (' + Plaid Sandbox transactions' if any(r[0]=='providerSandbox' for r in batches) else '')
                await c.execute("INSERT INTO companion_private.pattern_runs(account_id,run_id,request_id,input_digest,input_version,status,engine,evidence,is_demo,source_label,assessment) VALUES(%s,%s,%s,%s,%s,'running','deterministic',%s,%s,%s,%s)",(account,run,request_id,digest,version,Jsonb(dto.model_dump()),demo,label,Jsonb({'areas':[a.model_dump() for a in area_results(dto)],'findings':[f.model_dump() for f in findings(ids,dto)]})))
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
            async with self.service.lock:
                if expected_epoch is not None and self.service.document['state']['data_epoch']!=expected_epoch:raise DomainError('epoch_mismatch',409)
                if require_consent:
                    from .workflow_contracts import eligible,consent_key
                    if not eligible(self.service.document) or expected_consent is not None and consent_key(self.service.document)!=expected_consent:raise DomainError('consent_required',403)
                async with self.transaction() as c:
                    # Remove old ranks before inserting the validated permutation.
                    await c.execute('DELETE FROM companion_private.pattern_insights WHERE account_id=%s AND run_id=%s',(account,run))
                    for rank,i in enumerate(ids):
                        await c.execute('INSERT INTO companion_private.pattern_insights(account_id,run_id,concern_id,rank) VALUES(%s,%s,%s,%s)',(account,run,i,rank))
                    await c.execute('UPDATE companion_private.pattern_runs SET status=%s,engine=%s,assessment=%s WHERE account_id=%s AND run_id=%s AND status=\'running\'',(status,engine,Jsonb({'areas':[a.model_dump() for a in area_results(dto)],'findings':[f.model_dump() for f in findings(ids,dto)]}),account,run))
                    row=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s',(account,run))).fetchone()
                    result=await self._view(c,row)
            if cancelled: raise asyncio.CancelledError
            return result
        finally:
            self.store.pattern_active_runs.discard(run)
