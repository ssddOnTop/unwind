"""Banking integration on real disposable PostgreSQL, with mock Plaid/JWKS only."""
import asyncio
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from companion.auth_http import create_authenticated_app
from companion.bootstrap import UnavailableSynthesis
from companion.extensions.banking import BankingExtension
from companion.postgres import PostgresUnavailable
from test_auth_http import Harness,NOW,EPOCH
from test_banking import BankProvider
from test_postgres import local_pg_cluster,pg_dsn,runtime,event,receipt
from test_postgres_http import signing_key,VerifyEvents


def setup(tmp_path,key,runtime,*,collision=False,workflow_config=None):
    h=Harness(tmp_path/'residual',key);providers={}
    def replacements(path):
        provider=BankProvider();providers[path.name]=provider
        for name in ('one','two'):
            provider.add(name,item_id='same-colliding-item' if collision else 'item-'+path.name+'-'+name)
        return {'synthesis':UnavailableSynthesis(path/'checkpoints.sqlite'),
            'banking':BankingExtension(path,provider.client(),client_user_id=path.name,
                webhook_url='https://receiver.example/webhooks/plaid',
                ownership_check=lambda item:runtime.may_remove_item(path.name,item),
                claim_item=lambda item,epoch:runtime.reserve_item(path.name,item,epoch))}
    h.app=create_authenticated_app(tmp_path/'residual',verifier=h.verifier,clock=lambda:NOW,housekeeping_interval=3600,
        postgres_runtime=runtime,banking_profile=True,experience_profile=True,replacements_factory=replacements,
        webhook_url='https://receiver.example/webhooks/plaid',webhook_verifier=VerifyEvents(),webhook_worker_enabled=False,workflow_config=workflow_config,workflow_worker_enabled=False)
    h.app.state.webhook_worker.periodic=False  # Explicit ticks in these tests exercise notification targeting only.
    return h,providers


async def act(client,h,action,who='a',request_id=None,**fields):
    state=(await client.get('/banking/v1',headers=h.headers(who))).json()
    response=await client.post('/banking/v1/actions',headers=h.headers(who),json={
        'schema_version':'1','request_id':request_id or str(uuid4()),'data_epoch':state['data_epoch'],
        'expected_revision':state['revision'],'action':action,**fields})
    assert response.status_code==200,response.text
    return response.json()


async def add(client,h,key='one',who='a'):
    session=(await act(client,h,'add_link',who,request_id='add-'+key,consent_sandbox=True))['result']['link_session_id']
    value=await act(client,h,'exchange',who,request_id='exchange-'+key,link_session_id=session,public_token='public-sandbox-'+key)
    return next(c for c in value['connections'] if c['institution_name']=='PRIVATE Bank '+key)


async def test_banking_routes_require_verified_user_before_account_admission(tmp_path,signing_key,runtime):
    h,providers=setup(tmp_path,signing_key,runtime)
    async with h.client() as client:
        assert (await client.get('/banking/v1')).status_code==401
        assert (await client.post('/banking/v1/actions',json={'user_id':'a'})).status_code==401
        assert (await client.get('/auth/v1/session',headers=h.headers('b'))).status_code==200
        async with runtime.pool.connection() as conn:
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.accounts')).fetchone())[0]==0
        assert providers=={}


async def test_same_epoch_request_and_provider_account_ids_two_users_and_items_are_isolated(tmp_path,signing_key,runtime):
    h,providers=setup(tmp_path,signing_key,runtime)
    async with h.client() as client:
        for who in ('a','b'):
            await client.get('/banking/v1',headers=h.headers(who));handle=await h.handle(who)
            service=handle.service.app.state.service;doc=deepcopy(service.document);doc['state']['data_epoch']=EPOCH
            async with service.lock:await service.commit(doc)
        async def populate(who):
            for key in ('one','two'):await add(client,h,key,who)
            value=await act(client,h,'sync',who,request_id='same-sync',connection_id=None)
            assert value['result']['added']==12
            return value
        a,b=await asyncio.gather(populate('a'),populate('b'))
        assert not {c['id'] for c in a['connections']} & {c['id'] for c in b['connections']}
        ha,hb=await h.handle('a'),await h.handle('b')
        sa=ha.service.app.state.service.document['state'];sb=hb.service.app.state.service.document['state']
        assert not {o['id'] for o in sa['observations']} & {o['id'] for o in sb['observations']}
        assert (ha.data_dir/'plaid.key').read_bytes()!=(hb.data_dir/'plaid.key').read_bytes()
        async with runtime.pool.connection() as conn:
            owners=await (await conn.execute('SELECT account_id,count(*) FROM companion_private.plaid_owners WHERE active GROUP BY account_id')).fetchall()
            assert {str(row[0]):row[1] for row in owners}=={ha.account_id:2,hb.account_id:2}
            rows=await (await conn.execute('SELECT body::text FROM companion_private.snapshots')).fetchall()
            assert not any(secret in row[0] for row in rows for secret in ('PRIVATE Bank','PRIVATE Checking','access-sandbox-','public-sandbox-','"mask"'))
        assert not list(tmp_path.rglob('state.sqlite'))
        assert not list(tmp_path.rglob('identities.sqlite'))


async def test_item_collision_denied_before_any_inventory_sync_or_cleanup_other_user(tmp_path,signing_key,runtime):
    h,providers=setup(tmp_path,signing_key,runtime,collision=True)
    async with h.client() as client:
        await add(client,h);ha=await h.handle('a');before=deepcopy(ha.service.app.state.service.document)
        session=(await act(client,h,'add_link','b',consent_sandbox=True))['result']['link_session_id']
        value=(await client.get('/banking/v1',headers=h.headers('b'))).json()
        response=await client.post('/banking/v1/actions',headers=h.headers('b'),json={
            'schema_version':'1','request_id':'collision','data_epoch':value['data_epoch'],'expected_revision':value['revision'],
            'action':'exchange','link_session_id':session,'public_token':'public-sandbox-one'})
        assert response.status_code==409 and response.json()['error']['code']=='consent_required'
        hb=await h.handle('b');provider=providers[hb.account_id]
        assert not any(p in ('/accounts/get','/transactions/sync','/item/remove') for p,_ in provider.calls)
        result=await client.request('DELETE','/data',headers=h.headers('b'),json={'schema_version':'1','request_id':'delete','data_epoch':value['data_epoch'],'confirm':'delete_all'})
        assert result.status_code==200,result.text
        assert not any(p=='/item/remove' for p,_ in provider.calls)
        assert ha.service.app.state.service.document==before


async def test_webhook_exact_item_target_and_selection_generation_fence(tmp_path,signing_key,runtime):
    h,providers=setup(tmp_path,signing_key,runtime)
    async with h.client() as client:
        one,two=await add(client,h),await add(client,h,'two')
        handle=await h.handle('a');provider=providers[handle.account_id];inbox=h.app.state.webhook_inbox
        item='item-'+handle.account_id+'-two'
        await inbox.enqueue_verified(event(item));assert await h.app.state.webhook_worker.tick()
        calls=[b for p,b in provider.calls if p=='/transactions/sync']
        assert len(calls)==2 and all(b['access_token']=='access-sandbox-two' for b in calls)
        await inbox.enqueue_verified(event(item,2));job=await inbox.claim();assert job and await inbox.current(job)
        provider.items['access-sandbox-two']['accounts']=[]
        link=await act(client,h,'update_link',connection_id=two['id'])
        await act(client,h,'reconcile',connection_id=two['id'],link_session_id=link['result']['link_session_id'])
        assert not await inbox.current(job)
        from companion.contracts import DomainError
        with pytest.raises(DomainError):await h.app.state.webhook_worker.callback(job)
        assert len([p for p,_ in provider.calls if p=='/transactions/sync'])==2


async def test_all_item_owner_mutations_snapshot_and_receipt_rollback_atomically(tmp_path,signing_key,runtime):
    h,providers=setup(tmp_path,signing_key,runtime)
    async with h.client() as client:
        await add(client,h);await add(client,h,'two');await add(client,h,'one','b')
        ha,hb=await h.handle('a'),await h.handle('b');service=ha.service.app.state.service
        before=deepcopy(service.document)
        async with runtime.pool.connection() as conn:
            owners=await (await conn.execute('SELECT item_id,account_id,active,generation FROM companion_private.plaid_owners ORDER BY item_id')).fetchall()
        bad=deepcopy(before);bad['meta']['banking_item_contexts']=[{'item_id':'item-'+hb.account_id+'-one','generation':999}]
        with pytest.raises(PostgresUnavailable):
            async with service.lock:await service.commit(bad,receipt(before['state']['data_epoch']))
        assert service.document==before
        async with runtime.pool.connection() as conn:
            assert await (await conn.execute('SELECT item_id,account_id,active,generation FROM companion_private.plaid_owners ORDER BY item_id')).fetchall()==owners
            assert not await (await conn.execute("SELECT 1 FROM companion_private.receipts WHERE account_id=%s AND route='/test'",(ha.account_id,))).fetchone()


async def test_delete_fences_all_owned_items_jobs_but_not_another_user(tmp_path,signing_key,runtime):
    h,providers=setup(tmp_path,signing_key,runtime)
    async with h.client() as client:
        await add(client,h);await add(client,h,'two');await add(client,h,'one','b')
        ha,hb=await h.handle('a'),await h.handle('b');inbox=h.app.state.webhook_inbox
        for n,(owner,key) in enumerate(((ha,'one'),(ha,'two'),(hb,'one')),1):await inbox.enqueue_verified(event('item-'+owner.account_id+'-'+key,n))
        before=deepcopy(hb.service.app.state.service.document);key=(hb.data_dir/'plaid.key').read_bytes()
        epoch=ha.service.app.state.service.document['state']['data_epoch']
        response=await client.request('DELETE','/data',headers=h.headers('a'),json={'schema_version':'1','request_id':'delete','data_epoch':epoch,'confirm':'delete_all'})
        assert response.status_code==200,response.text
        assert not (ha.data_dir/'plaid.key').exists()
        assert hb.service.app.state.service.document==before and (hb.data_dir/'plaid.key').read_bytes()==key
        async with runtime.pool.connection() as conn:
            rows=await (await conn.execute('SELECT account_id,active FROM companion_private.plaid_owners')).fetchall()
            assert [r[1] for r in rows if str(r[0])==ha.account_id]==[False,False]
            assert [r[1] for r in rows if str(r[0])==hb.account_id]==[True]
            jobs=await (await conn.execute('SELECT account_id FROM companion_private.webhook_jobs')).fetchall()
            assert [str(r[0]) for r in jobs]==[hb.account_id]
        removals=[b['access_token'] for p,b in providers[ha.account_id].calls if p=='/item/remove']
        assert set(removals)=={'access-sandbox-one','access-sandbox-two'}
        assert not any(p=='/item/remove' for p,_ in providers[hb.account_id].calls)


async def test_exchange_visibility_quarantine_resolves_only_after_snapshot_activation(tmp_path,signing_key,runtime):
    h,providers=setup(tmp_path,signing_key,runtime)
    async with h.client() as client:
        await client.get('/banking/v1',headers=h.headers());handle=await h.handle('a');inbox=h.app.state.webhook_inbox
        item='item-'+handle.account_id+'-one'
        await inbox.enqueue_verified(event(item));assert await inbox.claim() is None
        await add(client,h)
        job=await inbox.claim();assert job and job['account_id']==handle.account_id and job['item_id']==item
        assert await inbox.current(job)
        assert len(providers)==1


async def test_existing_pg_single_item_upgrades_locally_preserving_identity_ownership_key_and_receipts(tmp_path,signing_key,runtime):
    from companion.extensions.plaid import PlaidExtension
    from test_banking_migration import LegacyProvider
    provider=LegacyProvider();root=tmp_path/'residual';old=Harness(root,signing_key)
    def legacy_replacements(path):
        return {'synthesis':UnavailableSynthesis(path/'checkpoints.sqlite'),
            'plaid':PlaidExtension(path,provider.client(),ownership_check=lambda item:runtime.may_remove_item(path.name,item))}
    old.app=create_authenticated_app(root,verifier=old.verifier,clock=lambda:NOW,housekeeping_interval=3600,
        postgres_runtime=runtime,plaid_profile=True,replacements_factory=legacy_replacements)
    async with old.client() as client:
        state=(await client.get('/state',headers=old.headers())).json();epoch=state['data_epoch']
        for name,fields in [('link_token',{'consent_sandbox':True}),('exchange',{'public_token':'public-sandbox-one'}),('sync',{})]:
            body={'schema_version':'1','request_id':name,'data_epoch':epoch,'action':name,**fields}
            response=await client.post('/connections/plaid',headers=old.headers(),json=body)
            assert response.status_code==200,response.text
        replay_body,replay=body,response.json();handle=await old.handle('a')
        identifier,key=handle.account_id,(handle.data_dir/'plaid.key').read_bytes()
        history=deepcopy(handle.service.app.state.service.document['state']['observations'])
        async with runtime.pool.connection() as conn:
            owner=await (await conn.execute("SELECT account_id,generation FROM companion_private.plaid_owners WHERE item_id='private-item-one'")).fetchone()
    new=Harness(root,signing_key)
    def replacements(path):
        return {'synthesis':UnavailableSynthesis(path/'checkpoints.sqlite'),
            'banking':BankingExtension(path,provider.client(),ownership_check=lambda item:runtime.may_remove_item(path.name,item),
                claim_item=lambda item,epoch:runtime.reserve_item(path.name,item,epoch))}
    new.app=create_authenticated_app(root,verifier=new.verifier,clock=lambda:NOW,housekeeping_interval=3600,
        postgres_runtime=runtime,banking_profile=True,replacements_factory=replacements)
    async with new.client() as client:
        value=(await client.get('/banking/v1',headers=new.headers())).json();handle=await new.handle('a')
        assert handle.account_id==identifier and (handle.data_dir/'plaid.key').read_bytes()==key
        assert value['data_epoch']==epoch and len(value['connections'])==1
        assert all(a['enabled'] for a in value['connections'][0]['accounts'])
        assert handle.service.app.state.service.document['state']['observations']==history
        assert (await client.post('/connections/plaid',headers=new.headers(),json=replay_body)).json()==replay
        async with runtime.pool.connection() as conn:
            current=await (await conn.execute("SELECT account_id,generation,active FROM companion_private.plaid_owners WHERE item_id='private-item-one'")).fetchone()
            assert current[0]==owner[0] and current[1]>owner[1] and current[2]
        assert sum(p=='/item/public_token/exchange' for p,_ in provider.calls)==1
