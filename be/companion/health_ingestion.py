"""Authenticated foreground HealthKit normalization; no seeding or identity selectors."""
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid5, NAMESPACE_URL
from copy import deepcopy
import hashlib
import json
from pydantic import Field, model_validator
from psycopg.types.json import Jsonb
from .patterns import Strict, HealthSample
from .contracts import DomainError

HELPER='org.localcompanion.test.healthseeder'
DATASET='six-month-pattern-demo-v1'
RECIPE=DATASET+':1'

class HealthRecord(HealthSample):
    start_at: datetime = Field(strict=False)
    end_at: datetime = Field(strict=False)
    provenance: Literal['synthetic','realHealthKit']
    source_bundle: str = Field(min_length=1,max_length=180,pattern=r'^[A-Za-z0-9.-]+$')
    source_revision: str = Field(max_length=80)
    dataset_id: str | None
    recipe: str | None
    @model_validator(mode='after')
    def origin(self):
        if self.provenance=='synthetic':
            if (self.source_bundle,self.dataset_id,self.recipe,self.timezone)!=(HELPER,DATASET,RECIPE,'UTC'):raise ValueError('Unrecognized synthetic origin')
        elif self.source_bundle==HELPER or self.dataset_id is not None or self.recipe is not None:
            raise ValueError('Incorrect HealthKit provenance')
        return self

class HealthIngestRequest(Strict):
    schema_version: Literal['1']
    request_id: str
    data_epoch: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$')
    collection_id: str
    page_index: int = Field(ge=0,le=499)
    page_count: int = Field(ge=1,le=500)
    window_start: datetime = Field(strict=False)
    window_end: datetime = Field(strict=False)
    samples: list[HealthRecord] = Field(max_length=100)
    @model_validator(mode='after')
    def valid(self):
        for value in (self.request_id,self.collection_id):UUID(value)
        if self.window_start.tzinfo is None or self.window_end.tzinfo is None or not timedelta(0)<self.window_end-self.window_start<=timedelta(days=187):raise ValueError('Invalid collection window')
        if self.page_index>=self.page_count:raise ValueError('Invalid page')
        if len({s.sample_id for s in self.samples})!=len(self.samples):raise ValueError('Duplicate samples')
        if any(s.end_at<self.window_start or s.start_at>self.window_end for s in self.samples):raise ValueError('Sample outside query window')
        return self


async def stage_health(conn,account,body):
    """Called inside the SAME snapshot/receipt transaction, never after its commit."""
    page=HealthIngestRequest.model_validate_json(json.dumps(body))
    await conn.execute("DELETE FROM companion_private.health_import_pages WHERE account_id=%s AND received_at<now()-interval '1 day'",(account,))
    collections=await (await conn.execute('SELECT DISTINCT collection_id FROM companion_private.health_import_pages WHERE account_id=%s',(account,))).fetchall()
    if len(collections)>=2 and page.collection_id not in {str(r[0]) for r in collections}:raise DomainError('busy',409)
    digest=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
    prior=await (await conn.execute('SELECT digest FROM companion_private.health_import_pages WHERE account_id=%s AND collection_id=%s AND page_index=%s',(account,page.collection_id,page.page_index))).fetchone()
    if prior and prior[0]!=digest:raise DomainError('idempotency_conflict',409)
    received=(await (await conn.execute('SELECT count(*) FROM companion_private.health_import_pages WHERE account_id=%s AND collection_id=%s',(account,page.collection_id))).fetchone())[0]
    if not prior and page.page_index!=received:
        raise DomainError('invalid_request',409,'Health collection expired or arrived out of order. Discard its pending refresh and collect again; existing records were not erased.')
    await conn.execute('INSERT INTO companion_private.health_import_pages(account_id,collection_id,page_index,digest,body) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(account,page.collection_id,page.page_index,digest,Jsonb(body)))
    rows=await (await conn.execute('SELECT body FROM companion_private.health_import_pages WHERE account_id=%s AND collection_id=%s ORDER BY page_index',(account,page.collection_id))).fetchall()
    if len(rows)!=page.page_count:return
    pages=[HealthIngestRequest.model_validate_json(json.dumps(r[0])) for r in rows]
    if any((p.page_index,p.page_count,p.window_start,p.window_end,p.data_epoch)!=(i,page.page_count,page.window_start,page.window_end,page.data_epoch) for i,p in enumerate(pages)):
        raise DomainError('invalid_request',422)
    samples=[s for p in pages for s in p.samples]
    if len({s.sample_id for s in samples})!=len(samples):raise DomainError('invalid_request',422)
    scopes={}
    for s in samples:scopes.setdefault((s.provenance,s.hk_type,s.source_bundle),[]).append(s)
    columns=['sample_id','hk_type','kind','start_at','end_at','timezone','value','unit','category','workout_activity','duration_seconds','energy_kcal','distance_m','source_name','source_bundle','source_revision']
    for (provenance,hk_type,bundle),records in scopes.items():
        # Real and helper samples live in separate source batches. Never sum the same recipe loaded by the old operator.
        if provenance=='synthetic':
            duplicate=await (await conn.execute("SELECT 1 FROM companion_private.pattern_batches WHERE account_id=%s AND dataset='six-month-pattern-demo-v1'",(account,))).fetchone()
            if duplicate:raise DomainError('invalid_request',409,'Remove the old operator health dataset before collecting the HealthKit recipe.')
        dataset='healthkit-'+hashlib.sha256((provenance+':'+bundle).encode()).hexdigest()[:40]
        batch=str(uuid5(NAMESPACE_URL,account+':'+dataset))
        await conn.execute('INSERT INTO companion_private.pattern_batches(account_id,batch_id,dataset,provenance,start_at,end_at,origin,active) VALUES(%s,%s,%s,%s,%s,%s,\'healthkit\',true) ON CONFLICT(account_id,batch_id) DO UPDATE SET start_at=LEAST(companion_private.pattern_batches.start_at,excluded.start_at),end_at=GREATEST(companion_private.pattern_batches.end_at,excluded.end_at),active=true',(account,batch,dataset,provenance,page.window_start,page.window_end))
        # Positive complete source/type scope only: empty/denied queries never imply erasure.
        await conn.execute('UPDATE companion_private.health_samples SET deleted=true,revision=revision+1 WHERE account_id=%s AND batch_id=%s AND hk_type=%s AND end_at>=%s AND start_at<=%s AND NOT(sample_id=ANY(%s)) AND NOT deleted',(account,batch,hk_type,page.window_start,page.window_end,[s.sample_id for s in records]))
        async with conn.cursor() as cur:
            semantic=[k for k in columns if k!='sample_id']
            updates=','.join(k+'=excluded.'+k for k in semantic)
            changed='companion_private.health_samples.deleted OR ('+','.join('companion_private.health_samples.'+k for k in semantic)+') IS DISTINCT FROM ('+','.join('excluded.'+k for k in semantic)+')'
            await cur.executemany('INSERT INTO companion_private.health_samples(account_id,batch_id,'+','.join(columns)+') VALUES('+','.join(['%s']*(len(columns)+2))+') ON CONFLICT(account_id,batch_id,sample_id) DO UPDATE SET '+updates+',deleted=false,revision=companion_private.health_samples.revision+1 WHERE '+changed,[(account,batch,*(getattr(s,k) for k in columns)) for s in records])
    # Keep saved reviews as history; their digest changes with evidence/eligibility on the next explicit action.
    await conn.execute('DELETE FROM companion_private.health_import_pages WHERE account_id=%s AND collection_id=%s',(account,page.collection_id))


async def ingest_health(service,body):
    page=HealthIngestRequest.model_validate_json(json.dumps(body))
    async with service.lock:
        if not getattr(service,'pattern_coordinator',None):raise DomainError('not_configured',409)
        receipt=service.receipt('/health/v1/samples',body)
        replay=await service.replay(receipt)
        if replay is not None:return replay
        if page.data_epoch!=service.document['state']['data_epoch']:raise DomainError('epoch_mismatch',409)
        now=service.clock()
        if page.window_end>now+timedelta(minutes=5) or page.window_start<now-timedelta(days=191):raise DomainError('invalid_request',422)
        doc=deepcopy(service.document)
        source=next(s for s in doc['state']['sources'] if s['id']=='healthkit')
        if not source['enabled'] or source['consent']!='granted':raise DomainError('consent_required',409)
        if page.samples:
            source.update(read_access='samples_readable',status='ready',coverage='partial',authorization_requested=True,last_success_at=now.isoformat(),last_attempt_at=now.isoformat())
        else:
            source.update(read_access='unknown',status='empty',coverage='unknown',authorization_requested=True,last_attempt_at=now.isoformat())
        receipt['response']=doc['state']
        await service.commit(doc,receipt,health_ingest=body)
        return deepcopy(service.document['state'])
