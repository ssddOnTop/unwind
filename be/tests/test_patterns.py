"""Pattern vertical slice tests: disposable local PG and captured fake model, never remote services."""
import asyncio
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from types import SimpleNamespace
from uuid import uuid4
import psycopg
from psycopg import sql
import pytest
from pydantic import ValidationError
from companion.patterns import HealthSample,Transaction,Aggregate,aggregate,concerns,model_review,ClarifyRequest
from companion.pattern_operator import generate,load,DATASET,six_month_start
from companion.pattern_migrations import MIGRATION_V3
from companion.postgres import PostgresConfig,PostgresRuntime
from companion.extensions.postgres import PostgresPersistenceExtension
from companion.pattern_store import PatternCoordinator
from test_postgres import local_pg_cluster,pg_dsn,document

NOW=datetime(2026,11,14,20,tzinfo=timezone.utc)

class Model:
    def __init__(self,fail=False):self.calls=[];self.fail=fail
    async def ainvoke(self,messages,**kwargs):
        self.calls.append((messages,kwargs))
        if self.fail:raise ValueError('Private provider failure must not be logged')
        return SimpleNamespace(content='{"concern_ids":["sleep","activity","food_service"]}')

@pytest.fixture
async def configured(pg_dsn):
    role='patterns_'+uuid4().hex
    async with await psycopg.AsyncConnection.connect(pg_dsn,autocommit=True) as c:
        await c.execute(MIGRATION_V3)
        await c.execute(MIGRATION_V3)  # Explicit migration is idempotent.
        await c.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
        await c.execute(sql.SQL('GRANT USAGE ON SCHEMA companion_private TO {}').format(sql.Identifier(role)))
        await c.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA companion_private TO {}').format(sql.Identifier(role)))
    runtime=PostgresRuntime(PostgresConfig(pg_dsn+' user='+role,local_test=True))
    await runtime.check()
    a=await runtime.identity(SimpleNamespace(issuer='https://issuer.invalid',subject='private-subject'))
    b=await runtime.identity(SimpleNamespace(issuer='https://issuer.invalid',subject='other-subject'))
    async with runtime.pool.connection() as c:
        result=await load(c,account_id=a,issuer='https://issuer.invalid',subject='private-subject',now=NOW)
        assert result['health_samples']>1000
        assert (await load(c,account_id=a,issuer='https://issuer.invalid',subject='private-subject',now=NOW))['reused']
    store=PostgresPersistenceExtension(runtime,a)
    await store.handle('commit',{'document':document()})
    service=SimpleNamespace(lock=asyncio.Lock(),clock=lambda:NOW)
    model=Model();coordinator=PatternCoordinator(service,store,lambda:model)
    yield runtime,a,b,store,coordinator,model
    await store.handle('close',{})
    await runtime.close()


def test_six_calendar_month_deterministic_known_patterns_and_missing():
    start,samples,tx=generate(NOW)
    assert start==datetime(2026,5,14,tzinfo=timezone.utc)
    assert generate(NOW)==(start,samples,tx)
    assert six_month_start(datetime(2026,8,31,tzinfo=timezone.utc)).day==28
    assert max(s['end_at'] for s in samples)<=NOW
    evidence=aggregate(samples,tx,NOW)
    assert concerns(evidence)==['activity','sleep','food_service']
    assert evidence.recent.sleep_hours_bucket==6 and evidence.baseline.sleep_hours_bucket==8
    assert len({s['start_at'].date() for s in samples if s['hk_type']=='stepCount'})<185
    empty=aggregate([],[],NOW)
    assert empty.recent.steps_average_bucket is None and empty.recent.missing_sleep
    assert concerns(empty)==[]
    # Identical healthy baseline/recent data is not arbitrarily labeled unhealthy.
    healthy=deepcopy(evidence.model_dump());healthy['recent']={**healthy['baseline'],'label':'recent4weeks'}
    assert concerns(Aggregate.model_validate(healthy))==[]


def test_semantics_reject_wrong_units_unknown_types_nonfinite_and_naive():
    _,samples,tx=generate(NOW)
    s=next(s for s in samples if s['hk_type']=='stepCount')
    for change in [{'unit':'bpm'},{'value':float('nan')},{'kind':'quantity'},{'hk_type':'bloodPressure'},{'start_at':datetime(2026,1,1)},{'extra':'private'}]:
        with pytest.raises((ValidationError,ValueError)):HealthSample.model_validate({**s,**change})
    with pytest.raises(ValidationError):Transaction.model_validate({**tx[0],'merchant':'private merchant'})


async def test_privacy_allowlist_captured_payload_and_invalid_output():
    _,samples,tx=generate(NOW)
    for s in samples:s['sample_id']='PRIVATE-SOURCE-UUID';s['source_name']='PRIVATE DEVICE'
    for t in tx:t['transaction_id']='PRIVATE-TRANSACTION'
    evidence=aggregate(samples,tx,NOW);model=Model()
    assert await model_review(evidence,concerns(evidence),lambda:model)==['sleep','activity','food_service']
    messages,kwargs=model.calls[0]
    payload=messages[1].content
    for forbidden in ['PRIVATE','2026','merchant','account','subject','issuer','email','token','uuid','source_name','transaction_id']:
        assert forbidden not in payload.lower() if forbidden.islower() else forbidden not in payload
    assert set(json.loads(payload))=={'sample_count_bucket','recent','baseline'}
    assert len(model.calls)==1 and kwargs['max_tokens']<800
    class Bad:
        async def ainvoke(self,*args,**kwargs):return SimpleNamespace(content='{"concern_ids":["diabetes"]}')
    with pytest.raises(ValueError):await model_review(evidence,concerns(evidence),lambda:Bad())


async def test_manual_durable_idempotency_cache_context_and_owner_isolation(configured):
    runtime,a,b,store,c,model=configured
    assert (await c.get())['is_demo']
    rid=str(uuid4())
    first,second=await asyncio.gather(c.analyze(rid),c.analyze(rid))
    assert first==second and first['status']=='complete' and first['engine']=='LLMassisted'
    assert first['is_demo'] and len(first['findings'])==3 and len(model.calls)==1
    alias=str(uuid4());assert await c.analyze(alias)==first
    assert len(model.calls)==1
    # Cached alternate request is itself durably bound to its original run.
    async with c.transaction() as conn:
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.pattern_requests')).fetchone())[0]==2
    corrected=await c.clarify(ClarifyRequest(schema_version='1',run_id=first['run_id'],concern_id='food_service',attribution='not_for_me'))
    assert corrected['findings'][2]['attribution']=='not_for_me'
    assert 'More food-service purchases' not in corrected['doctor_brief']
    assert len(model.calls)==1
    otherstore=PostgresPersistenceExtension(runtime,b)
    other=PatternCoordinator(SimpleNamespace(lock=asyncio.Lock(),clock=lambda:NOW),otherstore,lambda:model)
    try:
        assert (await other.get())['findings']==[]
        with pytest.raises(Exception):await other.clarify(ClarifyRequest(schema_version='1',run_id=first['run_id'],concern_id='food_service',attribution='for_me'))
        async with other.transaction() as conn:
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_samples')).fetchone())[0]==0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute("INSERT INTO companion_private.pattern_batches(account_id,batch_id,dataset,provenance,start_at,end_at) VALUES(%s,%s,'bad','synthetic',now(),now())",(a,str(uuid4())))
    finally:await otherstore.handle('close',{})


async def test_failure_never_fabricates_success_or_retries(configured):
    runtime,a,b,store,c,_=configured
    model=Model(fail=True);c.model_factory=lambda:model
    value=await c.analyze(str(uuid4()))
    assert value['status']=='unavailable' and value['engine']=='deterministic' and len(value['findings'])==3
    assert (await c.analyze(str(uuid4())))==value and len(model.calls)==1


async def test_forced_rls_deletion_cascade_and_operator_selection(configured):
    runtime,a,b,store,c,model=configured
    await c.analyze(str(uuid4()))
    async with c.transaction() as conn:
        rows=await (await conn.execute("SELECT relname,relforcerowsecurity FROM pg_class WHERE relnamespace='companion_private'::regnamespace AND relname IN ('pattern_batches','health_samples','pattern_transactions','pattern_runs','pattern_insights','pattern_requests','health_import_pages')")).fetchall()
        assert len(rows)==7 and all(r[1] for r in rows)
        assert (await (await conn.execute('SELECT min(start_at) FROM companion_private.health_samples')).fetchone())[0].month==5
    # Snapshot epoch erasure participates in the same PG transaction and cascades to all six tables.
    await store.handle('commit',{'document':document('new-epoch'),'erase_receipts':True})
    async with c.transaction() as conn:
        for t in ['health_samples','pattern_transactions','pattern_batches','pattern_runs','pattern_insights','pattern_requests']:
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.'+t)).fetchone())[0]==0
    async with runtime.pool.connection() as conn:
        with pytest.raises(ValueError):await load(conn,account_id=b,issuer='https://issuer.invalid',subject='private-subject',now=NOW)


async def test_reserved_run_survives_restart_without_second_model_call(configured):
    runtime,a,b,store,c,model=configured
    first=await c.analyze(str(uuid4()))
    async with c.transaction() as conn:
        await conn.execute("UPDATE companion_private.pattern_runs SET status='running',engine='deterministic'")
    replacement=PatternCoordinator(c.service,store,lambda:model)
    response=await replacement.analyze(str(uuid4()))
    assert response['status']=='unavailable' and response['engine']=='deterministic' and len(model.calls)==1
    assert response['findings']==first['findings']

async def test_existing_free_gateway_actual_wire_shape_mocked(monkeypatch):
    import httpx
    from companion.connections.llm import create_chat_model
    monkeypatch.setenv('COMPANION_LLM_PROVIDER','openrouter')
    monkeypatch.setenv('COMPANION_LLM_MODEL','openrouter/free')
    monkeypatch.setenv('OPENROUTER_API_KEY','synthetic-test-key')
    wires=[]
    async def handle(request):
        wires.append(json.loads(request.content))
        return httpx.Response(200,json={'id':'synthetic-response','object':'chat.completion','created':1,'model':'openrouter/free','choices':[{'index':0,'message':{'role':'assistant','content':'{"concern_ids":["sleep","activity","food_service"]}'},'finish_reason':'stop'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        _,s,t=generate(NOW);dto=aggregate(s,t,NOW)
        result=await model_review(dto,concerns(dto),lambda:create_chat_model(http_async_client=client))
    assert len(wires)==1 and result[0]=='sleep'
    wire=wires[0]
    assert wire.get('max_tokens',wire.get('max_completion_tokens'))==300 and 'tools' not in wire and 'callbacks' not in wire
    assert wire['provider']['allow_fallbacks'] is False
    assert wire['provider']['max_price']['prompt']==0
    assert set(json.loads(wire['messages'][1]['content']))=={'sample_count_bucket','recent','baseline'}


async def test_authenticated_http_vertical_slice_and_delete(pg_dsn,tmp_path):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from companion.auth_http import create_authenticated_app
    from companion.bootstrap import UnavailableSynthesis
    from companion.patterns import PatternOverview
    from test_auth_http import Harness,ISSUER
    async with await psycopg.AsyncConnection.connect(pg_dsn,autocommit=True) as conn:await conn.execute(MIGRATION_V3)
    runtime=PostgresRuntime(PostgresConfig(pg_dsn,local_test=True))
    await runtime.open()
    a=await runtime.identity(SimpleNamespace(issuer=ISSUER,subject='a'))
    async with runtime.pool.connection() as conn:await load(conn,account_id=a,issuer=ISSUER,subject='a',now=NOW)
    h=Harness(tmp_path/'residual',rsa.generate_private_key(public_exponent=65537,key_size=2048))
    h.app=create_authenticated_app(tmp_path/'residual',verifier=h.verifier,clock=lambda:NOW,housekeeping_interval=3600,
        postgres_runtime=runtime,experience_profile=True,replacements_factory=lambda path:{'synthesis':UnavailableSynthesis(path/'checkpoints.sqlite')})
    async with h.client() as client:
        for method,path in [('GET','/patterns/v1'),('POST','/patterns/v1/analyze'),('POST','/patterns/v1/clarify')]:
            assert (await client.request(method,path)).status_code==401
        overview=await client.get('/patterns/v1',headers=h.headers('a'))
        assert overview.status_code==200 and overview.json()['is_demo']
        account_handle=await h.handle('a')
        model=Model();account_handle.service.app.state.service.pattern_coordinator.model_factory=lambda:model
        request={'schema_version':'1','request_id':str(uuid4()),'data_epoch':account_handle.service.app.state.service.document['state']['data_epoch'],'fetch_latest_bank_data':False}
        assert (await client.post('/patterns/v1/analyze',headers=h.headers('a'),json=request)).status_code==403
        # Legacy core replay remains covered internally; the app must use the authorized queue.
        from test_banking_schedule import internal_post
        response=await internal_post(h,request)
        assert response.status_code==200,response.text
        PatternOverview.model_validate(response.json())
        assert response.json()['engine']=='LLMassisted' and len(model.calls)==1
        assert (await internal_post(h,request)).json()==response.json()
        assert (await client.get('/patterns/v1',headers=h.headers('b'))).json()['findings']==[]
        assert (await client.post('/patterns/v1/analyze',headers=h.headers('a'),json={**request,'account_id':a})).status_code==422
        state=(await client.get('/state',headers=h.headers('a'))).json()
        deleted=await client.request('DELETE','/data',headers=h.headers('a'),json={'schema_version':'1','request_id':'erase-patterns','data_epoch':state['data_epoch'],'confirm':'delete_all'})
        assert deleted.status_code==200,deleted.text
        assert (await client.get('/patterns/v1',headers=h.headers('a'))).json()['findings']==[]
        assert len(model.calls)==1

async def test_operator_lock_provider_batch_removal_and_unknown_fields(configured):
    runtime,a,b,store,c,model=configured
    async with runtime.pool.connection() as conn:
        with pytest.raises(ValueError,match='Stop the selected account runtime'):
            await load(conn,account_id=a,issuer='https://issuer.invalid',subject='private-subject',now=NOW)
    await store.handle('close',{})
    row={'transaction_id':'mock-provider-returned-id','event_at':(NOW-timedelta(days=2)).isoformat(),'timezone':'UTC','amount':22.0,'currency':'USD','category':'food_service'}
    async with runtime.pool.connection() as conn:
        await load(conn,account_id=a,issuer='https://issuer.invalid',subject='private-subject',now=NOW,remove=True)
        with pytest.raises(ValidationError):
            await load(conn,account_id=a,issuer='https://issuer.invalid',subject='private-subject',now=NOW,transactions=[{**row,'merchant':'not-allowed'}])
        await load(conn,account_id=a,issuer='https://issuer.invalid',subject='private-subject',now=NOW,transactions=[row])
        async with conn.transaction():
            await conn.execute("SELECT set_config('companion.account_id',%s,true)",(a,))
            assert set(r[0] for r in await (await conn.execute('SELECT provenance FROM companion_private.pattern_batches')).fetchall())=={'synthetic','providerSandbox'}
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.pattern_transactions')).fetchone())[0]==1
        await load(conn,account_id=a,issuer='https://issuer.invalid',subject='private-subject',now=NOW,remove=True)
        async with conn.transaction():
            await conn.execute("SELECT set_config('companion.account_id',%s,true)",(a,))
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.pattern_batches')).fetchone())[0]==0


async def test_190_day_retention_preserves_unaffected_review_attribution_and_request_binding(configured):
    runtime,a,b,store,c,model=configured
    request_id=str(uuid4());review=await c.analyze(request_id)
    corrected=await c.clarify(ClarifyRequest(schema_version='1',run_id=review['run_id'],concern_id='food_service',attribution='not_for_me'))
    async with c.transaction() as conn:
        before=(await (await conn.execute('SELECT count(*) FROM companion_private.health_samples')).fetchone())[0]
    await store.handle('commit',{'document':document()})
    async with c.transaction() as conn:
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_samples')).fetchone())[0]==before
        await conn.execute("UPDATE companion_private.pattern_transactions SET event_at=now()-interval '191 days' WHERE transaction_id=(SELECT transaction_id FROM companion_private.pattern_transactions LIMIT 1)")
    await store.handle('commit',{'document':document()})
    retained=await c.get()
    assert retained['run_id']==review['run_id'] and retained['findings']==corrected['findings']
    assert (await c.analyze(request_id))['run_id']==review['run_id']
    async with c.transaction() as conn:
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.pattern_requests WHERE request_id=%s',(request_id,))).fetchone())[0]==1
    assert len(model.calls)==1


def test_published_contract_matches_typed_allowlist_and_response():
    from pathlib import Path
    from companion.patterns import PatternOverview,AnalyzeRequest,BriefRequest
    schema=PatternOverview.model_json_schema();defs=schema.pop('$defs');defs['Overview']=schema
    defs['Request']=AnalyzeRequest.model_json_schema();defs['ClarifyRequest']=ClarifyRequest.model_json_schema();defs['BriefRequest']=BriefRequest.model_json_schema()
    from companion.health_ingestion import HealthIngestRequest
    h=HealthIngestRequest.model_json_schema();defs.update(h.pop('$defs'));defs['HealthIngestRequest']=h
    from companion.workflow_contracts import WorkflowOverview,EnqueueRequest
    w=WorkflowOverview.model_json_schema();defs.update(w.pop('$defs'));defs.pop('PatternOverview',None);defs['WorkflowOverview']=w;defs['EnqueueRequest']=EnqueueRequest.model_json_schema()
    defs=json.loads(json.dumps(defs).replace('#/$defs/PatternOverview','#/$defs/Overview'))
    published=json.loads((Path(__file__).resolve().parents[2]/'contracts/patterns.schema.json').read_text())
    assert published['$defs']==defs
    _,s,t=generate(NOW);dto=aggregate(s,t,NOW).model_dump()
    for change in [{'sample_count_bucket':'185'}, {'email':'private@example.invalid'}, {'recent':{**dto['recent'],'steps_average_bucket':9876}}]:
        with pytest.raises(ValidationError):Aggregate.model_validate({**dto,**change})

@pytest.mark.parametrize('allowed',[[],['activity'],['activity','sleep'],['activity','sleep','food_service']])
async def test_model_gets_exact_allowed_ids_zero_concerns_skip_call(allowed):
    _,samples,tx=generate(NOW);dto=aggregate(samples,tx,NOW)
    calls=[];factories=[]
    class Exact:
        async def ainvoke(self,messages,**kwargs):
            calls.append(messages)
            assert messages[0].content.endswith(json.dumps(sorted(allowed)))
            return SimpleNamespace(content=json.dumps({'concern_ids':list(reversed(allowed))}))
    def factory():factories.append(True);return Exact()
    assert await model_review(dto,allowed,factory)==list(reversed(allowed))
    assert len(calls)==len(factories)==(1 if allowed else 0)
    class Wrong:
        async def ainvoke(self,*args,**kwargs):return SimpleNamespace(content='{"concern_ids":["food_service","activity","sleep"]}')
    if 0<len(allowed)<3:
        with pytest.raises(ValueError):await model_review(dto,allowed,lambda:Wrong())

async def test_recovery_does_not_mark_active_shared_session_invocation_orphan(configured):
    runtime,a,b,store,c,model=configured
    first=await c.analyze(str(uuid4()))
    async with c.transaction() as conn:await conn.execute("UPDATE companion_private.pattern_runs SET status='running',engine='deterministic'")
    store.pattern_active_runs.add(first['run_id'])
    sibling=PatternCoordinator(c.service,store,lambda:model)
    assert (await sibling.get())['status']=='running'
    store.pattern_active_runs.clear()
    assert (await sibling.get())['status']=='unavailable'
    assert len(model.calls)==1

async def test_actual_exclusive_session_restart_recovers_durable_reservation(configured):
    runtime,a,b,store,c,model=configured
    request_id=str(uuid4());first=await c.analyze(request_id)
    async with c.transaction() as conn:await conn.execute("UPDATE companion_private.pattern_runs SET status='running',engine='deterministic'")
    await store.handle('close',{})
    restarted_store=PostgresPersistenceExtension(runtime,a)
    restarted=PatternCoordinator(SimpleNamespace(lock=asyncio.Lock(),clock=lambda:NOW),restarted_store,lambda:model)
    try:
        replay=await restarted.analyze(request_id)
        assert replay['status']=='unavailable' and replay['engine']=='deterministic'
        assert replay['findings']==first['findings'] and replay['run_id']==first['run_id']
        assert (await restarted.analyze(str(uuid4())))['run_id']==first['run_id']
        assert len(model.calls)==1
    finally:await restarted_store.handle('close',{})

async def test_private_brief_edits_match_run_persist_and_never_reach_model(configured):
    from companion.patterns import BriefRequest
    runtime,a,b,store,c,model=configured
    first=await c.analyze(str(uuid4()))
    saved=await c.save_brief(BriefRequest(schema_version='1',run_id=first['run_id'],expected_brief=first['doctor_brief'],text='My private question for the clinician.'))
    assert 'My private question' in saved['doctor_brief'] and saved['run_id']==first['run_id']
    assert saved['doctor_brief'].startswith('SYNTHETIC HEALTH DEMO')
    assert (await c.get())['doctor_brief']==saved['doctor_brief']
    assert len(model.calls)==1 and 'My private question' not in str(model.calls)
    with pytest.raises(Exception):
        await c.save_brief(BriefRequest(schema_version='1',run_id=first['run_id'],expected_brief=first['doctor_brief'],text='Stale overwrite'))
    corrected=await c.clarify(ClarifyRequest(schema_version='1',run_id=first['run_id'],concern_id='food_service',attribution='not_for_me'))
    assert 'My private question' not in corrected['doctor_brief']
    otherstore=PostgresPersistenceExtension(runtime,b)
    other=PatternCoordinator(SimpleNamespace(lock=asyncio.Lock(),clock=lambda:NOW),otherstore,lambda:model)
    try:
        with pytest.raises(Exception):await other.save_brief(BriefRequest(schema_version='1',run_id=first['run_id'],expected_brief=saved['doctor_brief'],text='Cross-account'))
    finally:await otherstore.handle('close',{})

async def test_zero_concern_run_is_honestly_deterministic_without_model(configured):
    runtime,a,b,store,c,model=configured
    async with c.transaction() as conn:
        await conn.execute("DELETE FROM companion_private.health_samples WHERE hk_type IN ('stepCount','sleepAnalysis')")
        await conn.execute("UPDATE companion_private.pattern_transactions SET category='other'")
    value=await c.analyze(str(uuid4()))
    assert value['status']=='complete' and value['engine']=='deterministic' and value['findings']==[]
    assert len(model.calls)==0
