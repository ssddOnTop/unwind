"""Local migration, privacy and atomic recovery; provider traffic is offline."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from companion.bootstrap import UnavailableSynthesis
from companion.domain import synthesis
from companion.extensions.persistence import PersistenceExtension
from companion.extensions.plaid import PlaidExtension
from companion.extensions.banking import BankingExtension
from companion.http import create_app
from test_backend import NOW
from test_banking import BankProvider,app_for,banking,action_body,act,add,enable,sync,bank
from test_plaid import transaction,page


class LegacyProvider(BankProvider):
    async def handle(self,request):
        body=json.loads(request.content)
        if request.url.path=='/transactions/sync' and 'options' not in body:
            self.calls.append((request.url.path,body))
            rows=[transaction('transaction-'+str(i),account_id='same-provider-account',date='2026-11-'+str(10+i),authorized_date='2026-11-'+str(10+i)) for i in (1,2,3)]
            return httpx.Response(200,json=page(rows,cursor='NEVER_REUSE_ITEM_CURSOR'))
        return await super().handle(request)


def old_app(path,provider):
    return create_app(path,plaid_profile=True,clock=lambda:NOW,housekeeping_interval=3600,
        replacements={'plaid':PlaidExtension(path,provider.client()),'synthesis':UnavailableSynthesis()})


def old_request(client,action,**fields):
    return {'schema_version':'1','data_epoch':client.get('/state').json()['data_epoch'],'request_id':str(uuid4()),'action':action,**fields}


def seed_legacy(path,provider):
    with TestClient(old_app(path,provider),base_url='http://127.0.0.1') as client:
        for action,fields in [('link_token',{'consent_sandbox':True}),('exchange',{'public_token':'public-sandbox-one'})]:
            response=client.post('/connections/plaid',json=old_request(client,action,**fields));assert response.status_code==200,response.text
        body=old_request(client,'sync');reply=client.post('/connections/plaid',json=body)
        assert reply.status_code==200,reply.text
        return client.get('/state').json(),body,reply.json()


def test_single_item_upgrade_keeps_key_token_history_receipts_and_never_reuses_item_cursor(tmp_path):
    provider=LegacyProvider();before,receipt,reply=seed_legacy(tmp_path,provider)
    key=(tmp_path/'plaid.key').read_bytes()
    with TestClient(app_for(tmp_path,provider),base_url='http://127.0.0.1') as client:
        value=banking(client);connection=value['connections'][0]
        assert value['data_epoch']==before['data_epoch'] and (tmp_path/'plaid.key').read_bytes()==key
        assert client.get('/state').json()['observations']==before['observations']
        assert all(not a['enabled'] for a in connection['accounts'])
        assert not any(c['evidence'] for c in client.get('/state').json()['ledger'])
        assert client.post('/connections/plaid',json=receipt).json()==reply
        assert client.post('/connections/plaid',json=receipt|{'action':'disconnect'}).json()['error']['code']=='idempotency_conflict'
        account=next(a for a in connection['accounts'] if a['subtype']=='checking')
        enable(client,connection,[account['id']]);value=sync(client,connection)
        assert value['result']['added']==0 and value['result']['modified']==0
        assert client.get('/state').json()['observations']==before['observations']
        filtered=[b for p,b in provider.calls if p=='/transactions/sync' and 'options' in b]
        assert len(filtered)==1 and 'cursor' not in filtered[0]
        assert sum(p=='/item/public_token/exchange' for p,_ in provider.calls)==1


@pytest.mark.asyncio
async def test_private_v2_cannot_be_silently_opened_by_legacy_writer(tmp_path):
    provider=BankProvider();app=app_for(tmp_path,provider)
    async with app.router.lifespan_context(app):
        coordinator=app.state.service.banking_coordinator
        state=app.state.service.document['state']
        await coordinator.action({'schema_version':'1','request_id':'link','data_epoch':state['data_epoch'],
            'expected_revision':state['revision'],'action':'add_link','consent_sandbox':True})
    legacy=old_app(tmp_path,provider)
    try:
        with pytest.raises(RuntimeError,match='no downgrade'):
            async with legacy.router.lifespan_context(legacy):pass
    finally:await legacy.state.service.close()


def test_upgrade_never_reactivates_legacy_disconnected_pending_removal(tmp_path):
    provider=LegacyProvider();seed_legacy(tmp_path,provider);provider.fail.add('/item/remove')
    with TestClient(old_app(tmp_path,provider),base_url='http://127.0.0.1') as client:
        response=client.post('/connections/plaid',json=old_request(client,'disconnect'))
        assert response.status_code==200,response.text
        old=client.get('/state').json()['observations']
    calls=len(provider.calls)
    with TestClient(app_for(tmp_path,provider),base_url='http://127.0.0.1') as client:
        connection=banking(client)['connections'][0]
        assert connection['status']=='disconnected' and connection['can_disconnect']
        assert len(provider.calls)==calls
        assert client.get('/state').json()['observations']==old
        provider.fail.clear();assert act(client,'disconnect',connection_id=connection['id'])['result']['status']=='disconnected'


class FailingPersistence:
    id,version,status='persistence','1','available'
    def __init__(self,path,route='/banking/v1/actions'):self.storage=PersistenceExtension(path/'state.sqlite');self.fail=False;self.route=route
    async def handle(self,operation,payload):
        if operation=='commit' and self.fail and (payload.get('receipt') or {}).get('route')==self.route:
            self.fail=False;raise RuntimeError('offline injected atomic failure')
        return await self.storage.handle(operation,payload)


def test_failed_exchange_commit_restart_recovers_encrypted_one_use_token_without_reexchange(tmp_path):
    provider=BankProvider();persistence=FailingPersistence(tmp_path)
    app=create_app(tmp_path,banking_profile=True,clock=lambda:NOW,housekeeping_interval=3600,
        replacements={'banking':BankingExtension(tmp_path,provider.client()),'synthesis':UnavailableSynthesis(),'persistence':persistence})
    with TestClient(app,base_url='http://127.0.0.1',raise_server_exceptions=False) as client:
        session=act(client,'add_link',consent_sandbox=True)['result']['link_session_id']
        body=action_body(client,'exchange',link_session_id=session,public_token='public-sandbox-one')
        persistence.fail=True
        response=client.post('/banking/v1/actions',json=body);assert response.status_code==500,response.text
        assert banking(client)['connections']==[] and (tmp_path/'plaid-pending.vault').exists()
        assert b'access-sandbox-one' not in (tmp_path/'plaid-pending.vault').read_bytes()
    with TestClient(app_for(tmp_path,provider),base_url='http://127.0.0.1') as client:
        assert act(client,'exchange',link_session_id=session,public_token='public-sandbox-one')['result']['status']=='complete'
        assert sum(p=='/item/public_token/exchange' for p,_ in provider.calls)==1
        assert not (tmp_path/'plaid-pending.vault').exists()
        assert len(banking(client)['connections'])==1


def test_upgrade_recovers_staged_legacy_one_use_exchange_only_after_new_snapshot_commits(tmp_path):
    provider=LegacyProvider();persistence=FailingPersistence(tmp_path,'/connections/plaid')
    app=create_app(tmp_path,plaid_profile=True,clock=lambda:NOW,housekeeping_interval=3600,
        replacements={'plaid':PlaidExtension(tmp_path,provider.client()),'synthesis':UnavailableSynthesis(),'persistence':persistence})
    with TestClient(app,base_url='http://127.0.0.1',raise_server_exceptions=False) as client:
        assert client.post('/connections/plaid',json=old_request(client,'link_token',consent_sandbox=True)).status_code==200
        persistence.fail=True
        response=client.post('/connections/plaid',json=old_request(client,'exchange',public_token='public-sandbox-one'))
        assert response.status_code==500 and (tmp_path/'plaid-pending.vault').exists()
        key=(tmp_path/'plaid.key').read_bytes()
    with TestClient(app_for(tmp_path,provider),base_url='http://127.0.0.1') as client:
        value=banking(client)
        assert len(value['connections'])==1 and value['connections'][0]['status']=='connected'
        assert (tmp_path/'plaid.key').read_bytes()==key and not (tmp_path/'plaid-pending.vault').exists()
        assert sum(p=='/item/public_token/exchange' for p,_ in provider.calls)==1


def test_expired_history_mapping_fences_changed_date_backfill(tmp_path):
    provider=BankProvider();app=app_for(tmp_path,provider)
    with TestClient(app,base_url='http://127.0.0.1') as client:
        connection=add(client);enable(client,connection);sync(client)
        later=NOW+timedelta(days=100);app.state.service.clock=lambda:later
        banking(client)  # Run the ordinary housekeeping/read projection boundary.
        assert not client.get('/state').json()['observations']
        for token in provider.items:
            for raw in ('same-provider-account','savings'):
                provider.responses[token,raw]=page([transaction('transaction-1',account_id=raw,date=later.date().isoformat(),authorized_date=later.date().isoformat())],cursor='after-retention')
        assert sync(client)['result']['added']==0
        assert not client.get('/state').json()['observations']


def test_real_removal_and_pending_posted_have_revisions_but_local_removal_does_not(bank):
    client,provider,_=bank;connection=add(client)
    account=next(a for a in connection['accounts'] if a['subtype']=='checking');enable(client,connection,[account['id']])
    raw='same-provider-account';token='access-sandbox-one'
    provider.responses[token,raw]=page([transaction('pending',account_id=raw,pending=True)],cursor='p1')
    sync(client);original=client.get('/state').json()['observations'][0]
    provider.responses[token,raw]=page([transaction('posted',account_id=raw,pending_transaction_id='pending')],removed=[{'transaction_id':'pending','account_id':raw}],cursor='p2')
    sync(client);observations=client.get('/state').json()['observations']
    posted=next(o for o in observations if o['lifecycle']=='posted')
    assert original in observations and posted['supersedes']=={'observation_id':original['id'],'revision':original['revision']}
    provider.responses[token,raw]=page([],removed=[{'transaction_id':'posted','account_id':raw}],cursor='p3')
    sync(client);history=client.get('/state').json()['observations']
    removal=next(o for o in history if o['operation']=='remove')
    assert removal['id']==posted['id'] and removal['revision']==posted['revision']+1 and posted in history
    enable(client,connection,[account['id']],enabled=False)
    assert client.get('/state').json()['observations']==history


def test_synthesis_and_replaced_notification_planner_receive_only_eligible_observations(tmp_path):
    class Spy(UnavailableSynthesis):
        def __init__(self):self.payloads=[];super().__init__()
        async def handle(self,operation,payload):
            if operation=='synthesize':self.payloads.append(deepcopy(payload));return synthesis(payload['mode'],'skipped','disabled')
            return await super().handle(operation,payload)
    from companion.scheduling import NotificationScheduler
    class Planner(NotificationScheduler):
        def __init__(self):self.payloads=[]
        async def handle(self,operation,payload):
            self.payloads.append(deepcopy(payload));return await super().handle(operation,payload)
    provider=BankProvider();spy=Spy();planner=Planner()
    app=create_app(tmp_path,banking_profile=True,experience_profile=True,clock=lambda:NOW,housekeeping_interval=3600,scheduler=planner,
        replacements={'banking':BankingExtension(tmp_path,provider.client()),'synthesis':spy})
    with TestClient(app,base_url='http://127.0.0.1') as client:
        connection=add(client);enable(client,connection);sync(client);enable(client,connection,enabled=False)
        state=client.get('/state').json()
        response=client.post('/process',json={'schema_version':'1','request_id':'model-spy','data_epoch':state['data_epoch'],'synthesis_mode':'disabled'})
        assert response.status_code==200,response.text
        assert state['observations'] and not spy.payloads[-1]['state']['observations']
        assert not planner.payloads[-1]['state']['observations']
        raw=json.dumps(spy.payloads)
        assert not any(s in raw for s in ('PRIVATE Checking','PRIVATE Bank','0007','same-provider-account','access-sandbox-one'))


def test_invalid_inventory_does_not_import_or_overwrite_previous_inventory(bank):
    client,provider,_=bank;connection=add(client);enable(client,connection);sync(client)
    before=deepcopy(banking(client)['connections'][0]['accounts']);history=client.get('/state').json()['observations']
    provider.items['access-sandbox-one']['accounts'].append(deepcopy(provider.items['access-sandbox-one']['accounts'][0]))
    value=sync(client);assert value['result']['status']=='partial'
    assert value['connections'][0]['accounts']==before
    assert client.get('/state').json()['observations']==history
    assert not any(c['evidence'] for c in client.get('/state').json()['ledger'])


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['disconnect','revoke','delete'])
async def test_privacy_changes_while_import_waits_never_publish_late_pages(tmp_path,operation):
    provider=BankProvider();app=app_for(tmp_path,provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
            async def action(name,**fields):
                value=(await client.get('/banking/v1')).json()
                return await client.post('/banking/v1/actions',json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':value['data_epoch'],'expected_revision':value['revision'],'action':name,**fields})
            link=(await action('add_link',consent_sandbox=True)).json()
            connection=(await action('exchange',link_session_id=link['result']['link_session_id'],public_token='public-sandbox-one')).json()['connections'][0]
            await action('select_accounts',connection_id=connection['id'],account_ids=[a['id'] for a in connection['accounts']],enabled=True)
            provider.entered,provider.release=asyncio.Event(),asyncio.Event()
            pending=asyncio.create_task(action('sync',connection_id=connection['id']));await asyncio.wait_for(provider.entered.wait(),2)
            state=(await client.get('/state')).json()
            if operation=='disconnect':response=await action('disconnect',connection_id=connection['id'])
            elif operation=='revoke':
                preferences=state['preferences'];preferences['enabled_source_ids']=[s for s in preferences['enabled_source_ids'] if s!='plaid']
                response=await client.put('/preferences',json={'schema_version':'1','request_id':'revoke','data_epoch':state['data_epoch'],'preferences':preferences})
            else:response=await client.request('DELETE','/data',json={'schema_version':'1','request_id':'erase','data_epoch':state['data_epoch'],'confirm':'delete_all'})
            assert response.status_code==200,response.text
            provider.release.set()
            try:late=await pending
            except asyncio.CancelledError:late=None
            if late is not None:assert late.status_code in (200,409)
            assert not (await client.get('/state')).json()['observations']
            value=(await client.get('/banking/v1')).json()
            assert not any(a['enabled'] for c in value['connections'] for a in c['accounts'])


def test_source_revoke_fences_every_item_keeps_history_and_cannot_be_reenabled_without_new_link(bank):
    client,provider,_=bank;one,two=add(client),add(client,'two');enable(client,one);enable(client,two);sync(client)
    state=client.get('/state').json();history=state['observations'];preferences=deepcopy(state['preferences'])
    preferences['enabled_source_ids']=[s for s in preferences['enabled_source_ids'] if s!='plaid']
    body={'schema_version':'1','request_id':'revoke-all','data_epoch':state['data_epoch'],'preferences':preferences}
    response=client.put('/preferences',json=body);assert response.status_code==200,response.text
    assert client.get('/state').json()['observations']==history
    assert not any(c['evidence'] for c in client.get('/state').json()['ledger'])
    assert all(c['status']=='disconnected' for c in banking(client)['connections'])
    removed=[b['access_token'] for p,b in provider.calls if p=='/item/remove']
    assert set(removed)=={'access-sandbox-one','access-sandbox-two'}
    assert client.put('/preferences',json=body).json()==response.json()
    assert sum(p=='/item/remove' for p,_ in provider.calls)==2
    preferences['enabled_source_ids'].append('plaid')
    assert client.put('/preferences',json=body|{'request_id':'forged-regrant','preferences':preferences}).status_code==409


def test_foreign_account_rows_in_filtered_response_never_import(bank):
    client,provider,_=bank;connection=add(client);enable(client,connection)
    provider.responses['access-sandbox-one','same-provider-account']=page([transaction('cross-account',account_id='unselected-foreign')])
    value=sync(client);assert value['result']['status']=='partial' and value['result']['added']==3
    state=client.get('/state').json();assert len(state['observations'])==3
    assert all(not c['evidence'] for c in state['ledger'])


@pytest.mark.asyncio
async def test_cancelled_delete_drains_cleanup_and_blocks_new_bank_until_key_erasure(tmp_path):
    provider=BankProvider();app=app_for(tmp_path,provider)
    async with app.router.lifespan_context(app):
        service=app.state.service;coordinator=service.banking_coordinator
        def body(action,**fields):
            state=service.document['state']
            return {'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'expected_revision':state['revision'],'action':action,**fields}
        linked=await coordinator.action(body('add_link',consent_sandbox=True))
        await coordinator.action(body('exchange',link_session_id=linked['result']['link_session_id'],public_token='public-sandbox-one'))
        old_key=(tmp_path/'plaid.key').read_bytes();entered,release=asyncio.Event(),asyncio.Event()
        # Gate the public extension boundary without opening a real provider connection.
        extension_call=coordinator.call
        async def call(operation,payload):
            if operation=='remove':entered.set();await release.wait()
            return await extension_call(operation,payload)
        coordinator.call=call
        epoch=service.document['state']['data_epoch']
        pending=asyncio.create_task(coordinator.delete({'schema_version':'1','request_id':'erase','data_epoch':epoch,'confirm':'delete_all'}))
        await asyncio.wait_for(entered.wait(),2)
        from companion.contracts import DomainError
        with pytest.raises(DomainError) as error:await coordinator.action(body('add_link',consent_sandbox=True))
        assert error.value.code=='busy'
        pending.cancel();release.set()
        with pytest.raises(asyncio.CancelledError):await pending
        assert not (tmp_path/'plaid.key').exists() and not coordinator.deleting
        await coordinator.action(body('add_link',consent_sandbox=True))
        assert (tmp_path/'plaid.key').read_bytes()!=old_key


@pytest.mark.asyncio
async def test_cancel_during_one_use_exchange_keeps_disabled_cleanup_handle_without_blocking_next_bank(tmp_path):
    class GatedProvider(BankProvider):
        async def handle(self,request):
            if request.url.path=='/accounts/get' and json.loads(request.content)['access_token']=='access-sandbox-one':
                self.entered.set();await self.release.wait()
            return await super().handle(request)
    provider=GatedProvider();provider.entered,provider.release=asyncio.Event(),asyncio.Event();app=app_for(tmp_path,provider)
    async with app.router.lifespan_context(app):
        coordinator=app.state.service.banking_coordinator
        def body(action,**fields):
            state=app.state.service.document['state']
            return {'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'expected_revision':state['revision'],'action':action,**fields}
        link=await coordinator.action(body('add_link',consent_sandbox=True));sid=link['result']['link_session_id']
        exchange=asyncio.create_task(coordinator.action(body('exchange',link_session_id=sid,public_token='public-sandbox-one')))
        await asyncio.wait_for(provider.entered.wait(),2)
        cancelled=await coordinator.action(body('cancel_link',link_session_id=sid))
        assert cancelled['result']['status']=='cancelled'
        provider.release.set();value=await exchange
        assert value['result']['status']=='cancelled' and value['connections'][0]['status']=='disconnected'
        assert value['connections'][0]['can_disconnect'] and not (tmp_path/'plaid-pending.vault').exists()
        assert not any(a['enabled'] for c in value['connections'] for a in c['accounts'])
        link=await coordinator.action(body('add_link',consent_sandbox=True))
        value=await coordinator.action(body('exchange',link_session_id=link['result']['link_session_id'],public_token='public-sandbox-two'))
        assert len(value['connections'])==2 and sum(c['status']=='connected' for c in value['connections'])==1
