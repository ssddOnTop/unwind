"""PostgreSQL-only pattern repository, sharing the account persistence session and lock."""
from contextlib import asynccontextmanager
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
        return 'Synthetic wearable demo + operator-imported Plaid Sandbox transactions'
    if 'synthetic' in provenances:
        return 'Operator demo — synthetic records, not Apple or Plaid provider data'
    if 'providerSandbox' in provenances:
        return 'Operator-imported Plaid Sandbox records — not live finances'
    return 'Normalized connected records — coverage is limited'


class PatternCoordinator:
    def __init__(self, service, persistence, model_factory=None):
        self.service,self.store=service,persistence
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
        brief=('SYNTHETIC OPERATOR DEMO — not medical or provider records.\n' if is_demo else '')+'Patterns worth reviewing\n'
        brief+='\n'.join(c.title+': '+c.explanation+' '+c.evidence_summary for c in cards if c.attribution!='not_for_me')
        brief+='\nOptional question for a clinician: Could changes in my routine or recording coverage help explain these observations? These records do not establish a diagnosis, diet quality or causality.'
        provenance=await (await conn.execute('SELECT provenance FROM companion_private.pattern_batches WHERE account_id=%s',(self.store.account_id,))).fetchall()
        return PatternOverview(doctor_brief=brief,status=status,run_id=str(run_id),analyzed_at=at.isoformat(),engine=engine,is_demo=is_demo,can_analyze=True,
            source_label=source_label({r[0] for r in provenance}),
            evidence=dto,findings=cards).model_dump()

    async def get(self):
        async with self.service.lock:
            async with self.transaction() as c:
                row=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s ORDER BY analyzed_at DESC LIMIT 1',(self.store.account_id,))).fetchone()
                if row: return await self._view(c,row)
                value=empty_overview()
                batches=await (await c.execute('SELECT provenance FROM companion_private.pattern_batches WHERE account_id=%s',(self.store.account_id,))).fetchall()
                if batches:
                    value['can_analyze']=True
                    value['is_demo']=any(r[0]=='synthetic' for r in batches)
                    value['source_label']=source_label({r[0] for r in batches})
                return value

    async def clarify(self, body):
        async with self.service.lock:
            async with self.transaction() as c:
                row=await (await c.execute('UPDATE companion_private.pattern_insights SET attribution=%s WHERE account_id=%s AND run_id=%s AND concern_id=%s RETURNING run_id',(body.attribution,self.store.account_id,body.run_id,body.concern_id))).fetchone()
                if not row: raise DomainError('invalid_request',409,'This pattern is no longer available.')
                run=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s',(self.store.account_id,body.run_id))).fetchone()
                return await self._view(c,run)

    async def analyze(self, request_id):
        # Serializes admission with account erasure, source changes and native double taps.
        async with self.service.lock:
            account=self.store.account_id
            async with self.transaction() as c:
                row=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s AND run_id IN (SELECT run_id FROM companion_private.pattern_requests WHERE account_id=%s AND request_id=%s)',(account,account,request_id))).fetchone()
                if row: return await self._view(c,row)
                rows=await (await c.execute('SELECT hk_type,start_at,end_at,timezone,value,category FROM companion_private.health_samples WHERE account_id=%s',(account,))).fetchall()
                samples=[dict(zip(('hk_type','start_at','end_at','timezone','value','category'),r)) for r in rows]
                rows=await (await c.execute('SELECT event_at,category,amount FROM companion_private.pattern_transactions WHERE account_id=%s',(account,))).fetchall()
                tx=[dict(zip(('event_at','category','amount'),r)) for r in rows]
                if not samples and not tx: raise DomainError('invalid_request',409,'No normalized records are available. Load the operator dataset first.')
                dto=aggregate(samples,tx,self.service.clock())
                batches=await (await c.execute('SELECT provenance FROM companion_private.pattern_batches WHERE account_id=%s',(account,))).fetchall()
                demo=any(r[0]=='synthetic' for r in batches)
                # Model choice participates in the local cache key but never the model prompt as user identity.
                version=VERSION+':'+os.environ.get('COMPANION_LLM_MODEL','unconfigured')[:200]
                digest=hashlib.sha256(json.dumps({'aggregate':dto.model_dump(),'provenance':sorted(r[0] for r in batches)},sort_keys=True).encode()).hexdigest()
                row=await (await c.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s AND input_digest=%s AND input_version=%s',(account,digest,version))).fetchone()
                if row:
                    await c.execute('INSERT INTO companion_private.pattern_requests(account_id,request_id,run_id) VALUES(%s,%s,%s)',(account,request_id,row[0]))
                    return await self._view(c,row)
                run=str(uuid4()); ids=concerns(dto)
                await c.execute("INSERT INTO companion_private.pattern_runs(account_id,run_id,request_id,input_digest,input_version,status,engine,evidence,is_demo) VALUES(%s,%s,%s,%s,%s,'running','deterministic',%s,%s)",(account,run,request_id,digest,version,Jsonb(dto.model_dump()),demo))
                await c.execute('INSERT INTO companion_private.pattern_requests(account_id,request_id,run_id) VALUES(%s,%s,%s)',(account,request_id,run))
                for rank,i in enumerate(ids):
                    await c.execute('INSERT INTO companion_private.pattern_insights(account_id,run_id,concern_id,rank) VALUES(%s,%s,%s,%s)',(account,run,i,rank))
            # No transaction is held across network I/O. The durable reservation forbids transport retry calls.
            status,engine='unavailable','deterministic'
            cancelled=False
            try:
                from langsmith import tracing_context
                with tracing_context(enabled=False):
                    ids=await model_review(dto,ids,self.model_factory)
                status,engine='complete','LLMassisted'
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
