"""Authenticated monolith + real disposable PostgreSQL; all provider traffic is mocked."""
import asyncio
from copy import deepcopy
from datetime import timezone
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from companion.auth_http import create_authenticated_app
from companion.bootstrap import UnavailableSynthesis
from companion.connections.plaid import PlaidSandboxClient,SandboxCredentials,PlaidFailure
from companion.extensions.plaid import PlaidExtension
from companion.postgres import PostgresConfig,PostgresRuntime,PostgresUnavailable
from companion.plaid_webhooks import VerifiedPlaidEvent,WebhookError
from companion.webhook_http import SandboxVerificationKeys
from test_auth_http import Harness,NOW
from test_postgres import local_pg_cluster,pg_dsn,runtime,account,document,event
from companion.extensions.postgres import PostgresPersistenceExtension
from test_plaid import Provider,page,transaction


@pytest.fixture
def signing_key():return rsa.generate_private_key(public_exponent=65537,key_size=2048)


class VerifyEvents:
    def __init__(self):self.calls=0;self.closed=False
    async def verify(self,raw,headers):
        self.calls+=1
        if dict(headers).get(b'plaid-verification')!=b'scratch-valid':raise WebhookError(401)
        body=json.loads(raw)
        return VerifiedPlaidEvent(body['item_id'],'sandbox','TRANSACTIONS','SYNC_UPDATES_AVAILABLE',hashlib.sha256(raw).hexdigest(),NOW)
    async def aclose(self):self.closed=True


def harness(tmp_path,signing_key,runtime, *, webhook=True, webhook_override=None, collision_item=None):
    h=Harness(tmp_path/'residual',signing_key)
    providers=[]
    def replacements(path):
        provider=Provider();providers.append(provider)
        # Unique synthetic Item per actual account, not per current HTTP caller.
        old_handle=provider.handle
        async def handle(request):
            result=await old_handle(request)
            if request.url.path=='/item/public_token/exchange':
                value=result.json();value['item_id']=collision_item or 'item_'+path.name
                return httpx.Response(200,json=value)
            return result
        return {'synthesis':UnavailableSynthesis(path/'checkpoints.sqlite'),
            'plaid':PlaidExtension(path,PlaidSandboxClient(SandboxCredentials('synthetic','synthetic'),httpx.MockTransport(handle)),webhook_url='https://receiver.example/webhooks/plaid',ownership_check=lambda item:runtime.may_remove_item(path.name,item))}
    verifier=webhook_override or VerifyEvents()
    h.app=create_authenticated_app(tmp_path/'residual',verifier=h.verifier,clock=lambda:NOW,housekeeping_interval=3600,
        postgres_runtime=runtime,plaid_profile=True,experience_profile=True,replacements_factory=replacements,
        webhook_url='https://receiver.example/webhooks/plaid' if webhook else None,
        webhook_verifier=verifier,webhook_worker_enabled=False)
    return h,providers,verifier


async def connect(client,h,who='a'):
    headers=h.headers(who)
    state=(await client.get('/state',headers=headers)).json()
    common={'schema_version':'1','data_epoch':state['data_epoch']}
    r=await client.post('/connections/plaid',headers=headers,json={**common,'request_id':'consent','action':'link_token','consent_sandbox':True})
    assert r.status_code==200,r.text
    r=await client.post('/connections/plaid',headers=headers,json={**common,'request_id':'exchange','action':'exchange','public_token':'public-sandbox-synthetic'})
    assert r.status_code==200,r.text
    return r.json()['state']


async def test_auth_gates_pg_and_admission_does_not_create_account(tmp_path,signing_key,runtime):
    h,providers,v=harness(tmp_path,signing_key,runtime)
    async with h.client() as client:
        assert (await client.get('/state')).status_code==401
        assert (await client.get('/health')).json()=={'status':'ok'}
        assert (await client.get('/auth/v1/session',headers=h.headers())).status_code==200
        async with runtime.pool.connection() as conn:
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.accounts')).fetchone())[0]==0
        assert not providers
        for path in ['/demo','/docs','/openapi.json']:
            assert (await client.get(path,headers=h.headers())).status_code==404


async def test_two_inflight_users_identical_request_ids_isolated_keys_delete(tmp_path,signing_key,runtime):
    h,providers,v=harness(tmp_path,signing_key,runtime)
    async with h.client() as client:
        a,b=await asyncio.gather(connect(client,h,'a'),connect(client,h,'b'))
        ha,hb=await h.handle('a'),await h.handle('b')
        assert ha.account_id!=hb.account_id
        assert (ha.data_dir/'plaid.key').read_bytes()!=(hb.data_dir/'plaid.key').read_bytes()
        assert not list(tmp_path.rglob('state.sqlite'))
        assert not list(tmp_path.rglob('identities.sqlite'))
        # Deletion A does not touch B's committed document, key or ownership.
        before_b=deepcopy(hb.service.app.state.service.document)
        key_b=(hb.data_dir/'plaid.key').read_bytes()
        r=await client.request('DELETE','/data',headers=h.headers('a'),json={'schema_version':'1','request_id':'same-request','data_epoch':a['data_epoch'],'confirm':'delete_all'})
        assert r.status_code==200,r.text
        assert r.json()['data_epoch']!=a['data_epoch']
        assert hb.service.app.state.service.document==before_b
        assert (hb.data_dir/'plaid.key').read_bytes()==key_b
        assert not (ha.data_dir/'plaid.key').exists()
        assert 'item_' not in (await client.get('/experience/v1',headers=h.headers('b'))).text
        async with runtime.pool.connection() as conn:
            rows=await (await conn.execute('SELECT account_id,active FROM companion_private.plaid_owners ORDER BY account_id')).fetchall()
            status={str(row[0]):row[1] for row in rows}
            assert status[ha.account_id] is False and status[hb.account_id] is True


async def test_webhook_bypasses_clerk_only_after_verification_and_durable_commit(tmp_path,signing_key,runtime):
    h,providers,v=harness(tmp_path,signing_key,runtime)
    async with h.client() as client:
        await connect(client,h)
        handle=await h.handle('a');item='item_'+handle.account_id
        raw=json.dumps({'item_id':item,'user_id':'untrusted-other-owner'}).encode()
        denied=await client.post('/webhooks/plaid',content=raw)
        assert denied.status_code==401
        assert not (await h.app.state.webhook_inbox.claim())
        accepted=await client.post('/webhooks/plaid',content=raw,headers={'Plaid-Verification':'scratch-valid'})
        assert accepted.status_code==200 and accepted.json()=={'accepted':True}
        job=await h.app.state.webhook_inbox.claim();assert job['account_id']==handle.account_id
        await h.app.state.webhook_inbox.finish(job,True)
        original=h.app.state.webhook_inbox.enqueue_verified
        async def failure(event):raise PostgresUnavailable()
        h.app.state.webhook_inbox.enqueue_verified=failure
        result=await client.post('/webhooks/plaid',content=raw,headers={'Plaid-Verification':'scratch-valid'})
        assert result.status_code==503 and result.json()['error']['retryable']
        h.app.state.webhook_inbox.enqueue_verified=original
        assert (await client.get('/state')).status_code==401
        assert (await client.post('/webhooks/plaid?user_id=other',content=raw)).status_code==400
        assert (await client.post('/webhooks/plaid',content=b'x'*(1024*1024+1))).status_code==413
    assert v.closed


async def test_worker_syncs_backend_owned_account_not_payload_identity(tmp_path,signing_key,runtime):
    h,providers,v=harness(tmp_path,signing_key,runtime)
    async with h.client() as client:
        await connect(client,h,'a');b=(await client.get('/state',headers=h.headers('b'))).json()
        ha=await h.handle('a')
        providers[0].pages=[page([transaction('synthetic-tx',date='2026-11-10',authorized_date='2026-11-10')])]
        raw=json.dumps({'item_id':'item_'+ha.account_id,'user_id':'b'}).encode()
        assert (await client.post('/webhooks/plaid',content=raw,headers={'Plaid-Verification':'scratch-valid'})).status_code==200
        assert len((await client.get('/state',headers=h.headers('a'))).json()['observations'])==0
        assert await h.app.state.webhook_worker.tick()
        after_a=(await client.get('/state',headers=h.headers('a'))).json()
        assert len(after_a['observations'])==1
        assert (await client.get('/state',headers=h.headers('b'))).json()==b


async def test_pending_exchange_stays_encrypted_and_restart_one_use_token_not_reexchanged(tmp_path,signing_key,pg_dsn):
    # Real encrypted private state lives in PG, while its key remains on the volume.
    first_runtime=PostgresRuntime(PostgresConfig(pg_dsn,local_test=True))
    h,providers,v=harness(tmp_path,signing_key,first_runtime)
    async with h.client() as client:
        state=await connect(client,h)
        ha=await h.handle('a');account_id=ha.account_id
        epoch=state['data_epoch']
    second_runtime=PostgresRuntime(PostgresConfig(pg_dsn,local_test=True))
    h2,providers2,v2=harness(tmp_path,signing_key,second_runtime)
    async with h2.client() as client:
        state=(await client.get('/state',headers=h2.headers())).json()
        assert state['data_epoch']==epoch
        assert (await h2.handle('a')).account_id==account_id
        assert state['sources'][2]['consent']=='granted'
        assert 'access-sandbox' not in json.dumps(state)
        assert not (await h2.handle('a')).data_dir.joinpath('plaid-pending.vault').exists()


@pytest.mark.parametrize('response,status',[(httpx.Response(400,json={'error_code':'INVALID_WEBHOOK_VERIFICATION_KEY_ID'}),401),
    (httpx.Response(500,json={'error_code':'SERVER_ERROR'}),503),
    (httpx.Response(200,content=b'{"key":{},"key":{}}'),503),
    (httpx.Response(200,content=b'x'*8193),503)])
async def test_verification_provider_bounded_sanitized_unknown_vs_transient(response,status):
    def serve(request):
        assert request.url.host=='sandbox.plaid.com'
        assert request.url.path=='/webhook_verification_key/get'
        return response
    client=PlaidSandboxClient(SandboxCredentials('synthetic','synthetic'),httpx.MockTransport(serve))
    with pytest.raises(WebhookError) as failure:await SandboxVerificationKeys(client)('synthetic-kid')
    assert failure.value.status==status
    assert 'synthetic' not in str(failure.value)


async def test_real_es256_verification_to_durable_pg_ignores_forged_owner(tmp_path,signing_key,runtime):
    from test_plaid_webhooks import Clock,Provider as KeyProvider
    from companion.plaid_webhooks import PlaidWebhookVerifier
    clock=Clock();keys=KeyProvider(clock)
    async def fetch(request):
        assert request.url.host=='sandbox.plaid.com'
        assert request.url.path=='/webhook_verification_key/get'
        return httpx.Response(200,json=await keys(json.loads(request.content)['key_id']))
    client=PlaidSandboxClient(SandboxCredentials('synthetic','synthetic'),httpx.MockTransport(fetch))
    verifier=PlaidWebhookVerifier(SandboxVerificationKeys(client),clock=lambda:clock.wall,monotonic=lambda:clock.tick)
    h,providers,_=harness(tmp_path,signing_key,runtime,webhook_override=verifier)
    async with h.client() as http:
        await connect(http,h,'a')
        ha=await h.handle('a')
        body=json.dumps({'item_id':'item_'+ha.account_id,'environment':'sandbox','webhook_type':'TRANSACTIONS','webhook_code':'SYNC_UPDATES_AVAILABLE','user_id':'b'}).encode()
        signature=keys.token(body).decode()
        headers={'Plaid-Verification':signature,'Content-Type':'application/json'}
        result=await http.post('/webhooks/plaid',content=body,headers=headers)
        assert result.status_code==200
        job=await h.app.state.webhook_inbox.claim();assert job['account_id']==ha.account_id
        await h.app.state.webhook_inbox.finish(job,True)
        tampered=await http.post('/webhooks/plaid',content=body+b' ',headers=headers)
        assert tampered.status_code==401
        duplicate=await http.post('/webhooks/plaid',content=body,headers=[('Plaid-Verification',signature),('Plaid-Verification',signature)])
        assert duplicate.status_code==401
        assert await h.app.state.webhook_inbox.claim() is None
        async with runtime.pool.connection() as conn:
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.accounts')).fetchone())[0]==1
        assert len(keys.calls)==1


async def test_missing_schema_never_falls_back_or_initializes_sqlite(pg_dsn,tmp_path):
    import psycopg
    with psycopg.connect(pg_dsn,autocommit=True) as conn:
        conn.execute('ALTER TABLE companion_private.snapshots RENAME TO intentionally_unavailable')
    runtime=PostgresRuntime(PostgresConfig(pg_dsn,local_test=True))
    try:
        with pytest.raises(PostgresUnavailable):await runtime.check()
        assert not list(tmp_path.rglob('*.sqlite'))
    finally:await runtime.close()


async def test_inflight_provider_for_a_does_not_block_b_or_cross_commit(tmp_path,signing_key,runtime):
    h,providers,_=harness(tmp_path,signing_key,runtime)
    async with h.client() as client:
        a=await connect(client,h,'a')
        providers[0].sync_entered=asyncio.Event();providers[0].sync_release=asyncio.Event()
        task=asyncio.create_task(client.post('/connections/plaid',headers=h.headers('a'),json={'schema_version':'1','request_id':'slow-a','data_epoch':a['data_epoch'],'action':'sync'}))
        await asyncio.wait_for(providers[0].sync_entered.wait(),2)
        try:
            b=await asyncio.wait_for(client.get('/state',headers=h.headers('b')),2)
            assert b.status_code==200
            deleted=await client.request('DELETE','/data',headers=h.headers('b'),json={'schema_version':'1','request_id':'delete-b','data_epoch':b.json()['data_epoch'],'confirm':'delete_all'})
            assert deleted.status_code==200
            assert not task.done()
        finally:providers[0].sync_release.set()
        result=await task
        assert result.status_code==200
        assert result.json()['state']['data_epoch']==a['data_epoch']
        assert (await client.get('/state',headers=h.headers('b'))).json()['data_epoch']==deleted.json()['data_epoch']
        assert not (await client.get('/state',headers=h.headers('b'))).json()['observations']


async def test_pg_commit_failure_exchange_restart_uses_encrypted_staging_not_public_token_again(tmp_path,signing_key,pg_dsn,monkeypatch):
    original=PostgresPersistenceExtension.handle
    armed=[True]
    async def fail_once(self,operation,payload):
        if operation=='commit' and payload['document']['meta'].get('plaid_connected') and payload.get('receipt') and armed[0]:
            armed[0]=False
            raise PostgresUnavailable()
        return await original(self,operation,payload)
    monkeypatch.setattr(PostgresPersistenceExtension,'handle',fail_once)
    r1=PostgresRuntime(PostgresConfig(pg_dsn,local_test=True))
    h,providers,_=harness(tmp_path,signing_key,r1)
    async with h.client() as client:
        state=(await client.get('/state',headers=h.headers())).json()
        common={'schema_version':'1','data_epoch':state['data_epoch']}
        assert (await client.post('/connections/plaid',headers=h.headers(),json={**common,'request_id':'link','action':'link_token','consent_sandbox':True})).status_code==200
        exchange={**common,'request_id':'one-use-exchange','action':'exchange','public_token':'public-sandbox-synthetic'}
        assert (await client.post('/connections/plaid',headers=h.headers(),json=exchange)).status_code==500
        handle=await h.handle('a')
        staged=(handle.data_dir/'plaid-pending.vault').read_bytes()
        assert b'access-sandbox' not in staged and b'public-sandbox' not in staged
    r2=PostgresRuntime(PostgresConfig(pg_dsn,local_test=True))
    h2,providers2,_=harness(tmp_path,signing_key,r2)
    async with h2.client() as client:
        retry=await client.post('/connections/plaid',headers=h2.headers(),json=exchange)
        assert retry.status_code==200,retry.text
        assert not any(endpoint=='/item/public_token/exchange' for endpoint,_ in providers2[0].requests)
        assert not (await h2.handle('a')).data_dir.joinpath('plaid-pending.vault').exists()


async def test_collision_pending_cleanup_cannot_remove_other_users_item(tmp_path,signing_key,runtime):
    h,providers,_=harness(tmp_path,signing_key,runtime,collision_item='shared-synthetic-item')
    async with h.client() as client:
        await connect(client,h,'a')
        before=(await client.get('/state',headers=h.headers('a'))).json()
        b=(await client.get('/state',headers=h.headers('b'))).json()
        common={'schema_version':'1','data_epoch':b['data_epoch']}
        assert (await client.post('/connections/plaid',headers=h.headers('b'),json={**common,'request_id':'link-b','action':'link_token','consent_sandbox':True})).status_code==200
        failed=await client.post('/connections/plaid',headers=h.headers('b'),json={**common,'request_id':'exchange-b','action':'exchange','public_token':'public-sandbox-synthetic'})
        assert failed.status_code==500
        deleted=await client.request('DELETE','/data',headers=h.headers('b'),json={**common,'request_id':'delete-b','confirm':'delete_all'})
        assert deleted.status_code==200
        assert 'Remote Sandbox Item removal unconfirmed.' in deleted.json()['limitations']
        assert not any(endpoint=='/item/remove' for endpoint,_ in providers[1].requests)
        assert (await client.get('/state',headers=h.headers('a'))).json()==before
