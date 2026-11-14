"""Normal authenticated ingestion and genuine mocked provider deltas, never operator health loading."""
import json
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4
import pytest
from pydantic import ValidationError
from cryptography.hazmat.primitives.asymmetric import rsa
from companion.auth_http import create_authenticated_app
from companion.bootstrap import UnavailableSynthesis
from companion.postgres import PostgresConfig,PostgresRuntime
from companion.pattern_migrations import MIGRATION_V3
from companion.health_ingestion import HealthIngestRequest,HealthRecord,HELPER,RECIPE,DATASET
from companion.pattern_operator import generate
from companion.pattern_banking import normalize_delta
from test_postgres import local_pg_cluster,pg_dsn,runtime
from test_auth_http import Harness,NOW
from test_patterns import Model
from test_banking_postgres import setup,add,act
from test_plaid import transaction,page


def health_pages(epoch,now=NOW):
    start,samples,_=generate(now)
    values=[]
    for sample in samples:
        # Mirror the helper read path: real HK sample IDs would be source+UUID hashes.
        s={**sample,'provenance':'synthetic','source_bundle':HELPER,'source_revision':'1','dataset_id':DATASET,'recipe':RECIPE}
        values.append(HealthRecord.model_validate(s).model_dump(mode='json'))
    cid=str(uuid4());count=(len(values)+99)//100
    return [{'schema_version':'1','request_id':str(uuid4()),'data_epoch':epoch,'collection_id':cid,'page_index':i,'page_count':count,
        'window_start':start.isoformat(),'window_end':now.isoformat(),'samples':values[i*100:i*100+100]} for i in range(count)]

@pytest.fixture
async def health_app(tmp_path,runtime):
    async with runtime.pool.connection() as c:await c.execute(MIGRATION_V3)
    h=Harness(tmp_path/'residual',rsa.generate_private_key(public_exponent=65537,key_size=2048))
    h.app=create_authenticated_app(tmp_path/'residual',verifier=h.verifier,clock=lambda:NOW,housekeeping_interval=3600,
        postgres_runtime=runtime,experience_profile=True,replacements_factory=lambda path:{'synthesis':UnavailableSynthesis(path/'checkpoints.sqlite')})
    yield h

async def consent(client,h,who='a'):
    state=(await client.get('/state',headers=h.headers(who))).json()
    preferences=deepcopy(state['preferences'])
    if 'healthkit' not in preferences['enabled_source_ids']:preferences['enabled_source_ids'].append('healthkit')
    r=await client.put('/preferences',headers=h.headers(who),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'preferences':preferences})
    assert r.status_code==200,r.text
    return r.json()

async def send(client,h,body,who='a'):
    return await client.post('/health/v1/samples',headers=h.headers(who),json=body)

async def test_health_real_route_batches_atomic_publication_epoch_replay_and_analysis(health_app):
    h=health_app
    async with h.client() as client:
        assert (await client.post('/health/v1/samples',json={})).status_code==401
        state=await consent(client,h);pages=health_pages(state['data_epoch'])
        handle=await h.handle('a');coordinator=handle.service.app.state.service.pattern_coordinator
        model=Model();coordinator.model_factory=lambda:model
        for p in pages[:-1]:
            response=await send(client,h,p);assert response.status_code==200,response.text
        async with coordinator.transaction() as c:
            assert (await (await c.execute('SELECT count(*) FROM companion_private.health_samples')).fetchone())[0]==0
        last=await send(client,h,pages[-1]);assert last.status_code==200,last.text
        assert (await send(client,h,pages[-1])).json()==last.json()
        async with coordinator.transaction() as c:
            assert (await (await c.execute('SELECT count(*) FROM companion_private.health_samples WHERE NOT deleted')).fetchone())[0]==sum(len(p['samples']) for p in pages)
            assert (await (await c.execute('SELECT count(*) FROM companion_private.health_import_pages')).fetchone())[0]==0
            assert set(r[0] for r in await (await c.execute('SELECT origin FROM companion_private.pattern_batches')).fetchall())=={'healthkit'}
        assert (await client.get('/patterns/v1',headers=h.headers('b'))).json()['findings']==[]
        # Health-only data is still useful. No purchases or nutrition are fabricated.
        class RankHealth:
            async def ainvoke(self,*args,**kwargs):return type('Reply',(),{'content':'{"concern_ids":["sleep","activity"]}'})()
        coordinator.model_factory=lambda:RankHealth()
        result=await client.post('/patterns/v1/analyze',headers=h.headers('a'),json={'schema_version':'1','request_id':str(uuid4())})
        assert result.status_code==200,result.text
        assert {f['concern_id'] for f in result.json()['findings']}=={'activity','sleep'}
        assert result.json()['is_demo'] and 'HealthKit' in result.json()['source_label']
        wrong={**pages[0],'request_id':str(uuid4()),'data_epoch':str(uuid4())}
        assert (await send(client,h,wrong)).status_code==409
        current=(await client.get('/state',headers=h.headers())).json()
        erased=await client.request('DELETE','/data',headers=h.headers(),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':current['data_epoch'],'confirm':'delete_all'})
        assert erased.status_code==200
        assert (await send(client,h,pages[0])).status_code==409
        assert (await coordinator.get())['findings']==[]

async def test_health_probe_provenance_conflict_empty_and_complete_scope_tombstone(health_app):
    h=health_app
    async with h.client() as client:
        state=await consent(client,h);p=health_pages(state['data_epoch'])[0]
        p.update(page_index=0,page_count=1,samples=p['samples'][:2])
        assert (await send(client,h,p)).status_code==200
        changed=deepcopy(p);changed['samples'][0]['value']=999
        assert (await send(client,h,changed)).status_code==409
        handle=await h.handle('a');c=handle.service.app.state.service.pattern_coordinator
        # No-readable-data refresh must not infer deletion.
        empty={**p,'request_id':str(uuid4()),'collection_id':str(uuid4()),'samples':[]}
        assert (await send(client,h,empty)).status_code==200
        async with c.transaction() as conn:
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_samples WHERE NOT deleted')).fetchone())[0]==2
        bad=deepcopy(p);bad.update(request_id=str(uuid4()),collection_id=str(uuid4()))
        bad['samples'][0]['recipe']='bounded-probe-v1'
        assert (await send(client,h,bad)).status_code==422
        bad['samples'][0]['recipe']=RECIPE;bad['samples'][0]['provenance']='realHealthKit'
        assert (await send(client,h,bad)).status_code==422
        # Positive complete same-source/type scope can tombstone a previously returned UUID.
        same=deepcopy(p);same.update(request_id=str(uuid4()),collection_id=str(uuid4()))
        sample=deepcopy(same['samples'][0]);sample['sample_id']='replacement-id';same['samples']=[sample]
        assert (await send(client,h,same)).status_code==200
        async with c.transaction() as conn:
            assert (await (await conn.execute('SELECT deleted FROM companion_private.health_samples WHERE sample_id=%s',(p['samples'][0]['sample_id'],))).fetchone())[0]
        # Another account cannot replay a collection into this account even with the same IDs.
        other=await consent(client,h,'b');foreign={**same,'request_id':str(uuid4()),'data_epoch':other['data_epoch']}
        assert (await send(client,h,foreign,'b')).status_code==200
        assert (await send(client,h,{**foreign,'account_id':handle.account_id},'b')).status_code==422

async def test_health_requires_source_consent_and_page_bounds(health_app):
    h=health_app
    async with h.client() as client:
        state=(await client.get('/state',headers=h.headers())).json()
        p=health_pages(state['data_epoch'])[0]
        assert (await send(client,h,p)).status_code==409
        with pytest.raises(ValidationError):HealthIngestRequest.model_validate_json(json.dumps({**p,'page_count':501}))
        with pytest.raises(ValidationError):HealthIngestRequest.model_validate_json(json.dumps({**p,'samples':p['samples']*2}))

async def test_normal_bank_sync_populates_owned_patterns_updates_removes_unlinks(tmp_path,runtime):
    async with runtime.pool.connection() as c:await c.execute(MIGRATION_V3)
    # Use a real non-bypass role for scoped application connections and assertions.
    from psycopg import sql
    role='bridge_'+uuid4().hex
    async with runtime.pool.connection() as c:
        await c.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
        await c.execute(sql.SQL('GRANT USAGE ON SCHEMA companion_private TO {}').format(sql.Identifier(role)))
        await c.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA companion_private TO {}').format(sql.Identifier(role)))
    runtime=PostgresRuntime(PostgresConfig(runtime.config.dsn+' user='+role,local_test=True))
    h,providers=setup(tmp_path,rsa.generate_private_key(public_exponent=65537,key_size=2048),runtime)
    async with h.client() as client:
        one=await add(client,h);two=await add(client,h,'two');await add(client,h,'one','b')
        await act(client,h,'sync',connection_id=None);await act(client,h,'sync','b',connection_id=None)
        handle=await h.handle('a');coordinator=handle.service.app.state.service.pattern_coordinator;provider=providers[handle.account_id]
        async with coordinator.transaction() as c:
            assert (await (await c.execute('SELECT count(*) FROM companion_private.pattern_transactions WHERE NOT deleted')).fetchone())[0]==12
            assert (await (await c.execute("SELECT count(*) FROM companion_private.pattern_batches WHERE origin='plaid' AND active")).fetchone())[0]==4
            rows=await (await c.execute('SELECT DISTINCT provenance FROM companion_private.pattern_batches')).fetchall();assert rows==[('providerSandbox',)]
        provider.responses[('access-sandbox-one','same-provider-account')]=page(modified=[transaction('transaction-1',account_id='same-provider-account',personal_finance_category={'primary':'FOOD_AND_DRINK','detailed':'FOOD_AND_DRINK_GROCERIES'})],removed=[{'transaction_id':'transaction-2','account_id':'same-provider-account'}])
        await act(client,h,'sync',connection_id=one['id'])
        async with coordinator.transaction() as c:
            rows=await (await c.execute('SELECT t.category,t.deleted FROM companion_private.pattern_transactions t JOIN companion_private.pattern_batches b USING(account_id,batch_id) WHERE b.connection_id=%s AND b.bank_account_id=%s AND t.transaction_id=%s',(one['id'],one['accounts'][0]['id'],'transaction-1'))).fetchall()
            # Account order is not relied upon; check the modified category exists in this connection.
            assert (await (await c.execute("SELECT count(*) FROM companion_private.pattern_transactions t JOIN companion_private.pattern_batches b USING(account_id,batch_id) WHERE b.connection_id=%s AND t.category='groceries'",(one['id'],))).fetchone())[0]==1
            assert (await (await c.execute('SELECT count(*) FROM companion_private.pattern_transactions WHERE deleted')).fetchone())[0]==1
        await act(client,h,'disconnect',connection_id=one['id'])
        async with coordinator.transaction() as c:
            assert (await (await c.execute('SELECT count(*) FROM companion_private.pattern_batches WHERE connection_id=%s AND active',(one['id'],))).fetchone())[0]==0
            assert (await (await c.execute('SELECT count(*) FROM companion_private.pattern_transactions')).fetchone())[0]==12
            assert (await (await c.execute('SELECT count(*) FROM companion_private.pattern_batches WHERE connection_id=%s AND active',(two['id'],))).fetchone())[0]==2
        other=(await h.handle('b')).service.app.state.service.pattern_coordinator
        async with other.transaction() as c:assert (await (await c.execute('SELECT count(*) FROM companion_private.pattern_transactions WHERE NOT deleted')).fetchone())[0]==6


def test_bank_normalization_uses_pfc_not_merchant_and_keeps_six_month_date():
    row=transaction('six-month',authorized_date='2026-05-15',date='2026-05-15',merchant_name='junk food disease claim',personal_finance_category={'primary':'GENERAL_MERCHANDISE','detailed':'GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE'})
    result=normalize_delta(page([row]),'connection','account','UTC',NOW.isoformat())
    assert result['records'][0]['category']=='other' and result['records'][0]['event_at'].startswith('2026-05-15')
    assert 'junk' not in json.dumps(result) and 'merchant' not in json.dumps(result)

async def test_complete_six_month_normal_health_and_plaid_pipeline_without_operator_loader(tmp_path,runtime):
    async with runtime.pool.connection() as c:await c.execute(MIGRATION_V3)
    h,providers=setup(tmp_path,rsa.generate_private_key(public_exponent=65537,key_size=2048),runtime)
    async with h.client() as client:
        connection=await add(client,h)
        handle=await h.handle('a');service=handle.service.app.state.service;provider=providers[handle.account_id]
        _,_,transactions=generate(NOW)
        rows=[]
        for t in transactions:
            category={'food_service':'FOOD_AND_DRINK_RESTAURANT','groceries':'FOOD_AND_DRINK_GROCERIES','other':'FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK'}[t['category']]
            rows.append(transaction(t['transaction_id'],account_id='same-provider-account',date=t['event_at'].date().isoformat(),authorized_date=t['event_at'].date().isoformat(),amount=t['amount'],personal_finance_category={'primary':'FOOD_AND_DRINK','detailed':category}))
        provider.responses[('access-sandbox-one','same-provider-account')]=page(rows)
        provider.responses[('access-sandbox-one','savings')]=page([])
        await act(client,h,'sync',connection_id=connection['id'])
        state=await consent(client,h)
        for p in health_pages(state['data_epoch']):
            r=await send(client,h,p);assert r.status_code==200,r.text
        model=Model();service.pattern_coordinator.model_factory=lambda:model
        r=await client.post('/patterns/v1/analyze',headers=h.headers(),json={'schema_version':'1','request_id':str(uuid4())})
        assert r.status_code==200,r.text
        result=r.json()
        assert result['engine']=='LLMassisted' and result['status']=='complete'
        assert {f['concern_id'] for f in result['findings']}=={'activity','sleep','food_service'}
        assert result['evidence']['recent']['steps_average_bucket']==3000
        assert result['evidence']['baseline']['steps_average_bucket']==9000
        assert result['evidence']['recent']['food_service_per_week_bucket']==5
        assert result['evidence']['baseline']['food_service_per_week_bucket']==1
        assert 'HealthKit' in result['source_label'] and 'Plaid Sandbox' in result['source_label']
        async with service.pattern_coordinator.transaction() as c:
            assert set(r[0] for r in await (await c.execute('SELECT origin FROM companion_private.pattern_batches')).fetchall())=={'healthkit','plaid'}
            earliest=(await (await c.execute('SELECT min(event_at) FROM companion_private.pattern_transactions')).fetchone())[0]
            assert earliest.month==5 and earliest.day==14
        raw=model.calls[0][0][1].content
        assert all(forbidden not in raw for forbidden in [HELPER,RECIPE,handle.account_id,'PRIVATE','transaction_id','2026-'])
        corrected=await client.post('/patterns/v1/clarify',headers=h.headers(),json={'schema_version':'1','run_id':result['run_id'],'concern_id':'food_service','attribution':'not_for_me'})
        assert corrected.status_code==200
        assert corrected.json()['run_id']==result['run_id']
        assert 'More food-service purchases' not in corrected.json()['doctor_brief']
        assert len(model.calls)==1

async def test_expired_partial_health_collection_fails_visibly_without_erasing_records(health_app):
    h=health_app
    async with h.client() as client:
        state=await consent(client,h);pages=health_pages(state['data_epoch'])
        assert (await send(client,h,pages[0])).status_code==200
        coordinator=(await h.handle('a')).service.app.state.service.pattern_coordinator
        async with coordinator.transaction() as c:
            await c.execute("UPDATE companion_private.health_import_pages SET received_at=now()-interval '2 days'")
        assert (await send(client,h,pages[1])).status_code==409
        async with coordinator.transaction() as c:
            assert (await (await c.execute('SELECT count(*) FROM companion_private.health_samples')).fetchone())[0]==0


def test_exact_verified_recipe_and_duplicate_daily_steps_do_not_double_count():
    from companion.patterns import aggregate
    assert DATASET=='six-month-pattern-demo-v1' and RECIPE=='six-month-pattern-demo-v1:1'
    records=health_pages(str(uuid4()))[0]['samples']
    bad={**records[0],'recipe':DATASET}
    with pytest.raises(ValidationError):HealthRecord.model_validate_json(json.dumps(bad))
    _,samples,tx=generate(NOW)
    expected=aggregate(samples,tx,NOW)
    doubled=samples+[deepcopy(s) for s in samples if s['hk_type']=='stepCount']
    actual=aggregate(doubled,tx,NOW)
    assert actual.recent.steps_average_bucket==expected.recent.steps_average_bucket
    assert actual.baseline.steps_average_bucket==expected.baseline.steps_average_bucket
