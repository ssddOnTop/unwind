"""Additional failure, restart, generation and privacy conformance for Sandbox connections."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from companion.connections.plaid import PlaidSandboxClient, SandboxCredentials
from companion.contracts import DomainError
from sample_data import BATCH
from companion.domain import iso
from companion.plaid_contracts import validate_profile
from test_backend import NOW
from test_plaid import ACCESS, PUBLIC, LINK, Provider, app_for, call, connect, get_state, page, transaction


def test_401_disables_provider_without_other_environment(tmp_path):
    paths = []
    def handler(request):
        paths.append(str(request.url))
        return httpx.Response(401, json={"error_code":"INVALID_API_KEYS", "error_message":"PRIVATE_ERROR"})
    from companion.extensions.plaid import PlaidExtension
    from sample_data import create_app
    from companion.bootstrap import UnavailableSynthesis
    extension=PlaidExtension(tmp_path, PlaidSandboxClient(SandboxCredentials('synthetic','synthetic'),httpx.MockTransport(handler)))
    app=create_app(tmp_path,plaid_profile=True,replacements={'plaid':extension,'synthesis':UnavailableSynthesis()},clock=lambda:NOW)
    with TestClient(app,base_url='http://127.0.0.1') as c:
        r=call(c,'link_token',consent_sandbox=True)
        assert r.status_code==409 and r.json()['error']['code']=='not_configured'
        assert get_state(c)['sources'][2]['capability']=='not_configured'
        assert call(c,'link_token',consent_sandbox=True).status_code==409
        assert len(paths)==1 and paths[0].startswith('https://sandbox.plaid.com/')


def test_preference_revocation_removes_remote_item_and_old_token_receipts(tmp_path):
    provider=Provider()
    with TestClient(app_for(tmp_path,provider),base_url='http://127.0.0.1') as c:
        link=call(c,'link_token',rid='link',consent_sandbox=True)
        call(c,'exchange',public_token=PUBLIC)
        call(c,'sync')
        before=get_state(c)
        before['preferences']['enabled_source_ids'].remove('plaid')
        r=c.put('/preferences',json={'schema_version':'1','request_id':'revoke','data_epoch':before['data_epoch'],'preferences':before['preferences']})
        assert r.status_code==200,r.text
        assert r.json()['sources'][2]['consent']=='revoked'
        assert r.json()['ledger'][0]['purchase_count']==0
        assert len(r.json()['observations'])==1
        assert any(path=='/item/remove' for path,_ in provider.requests)
        # Old Link response no longer replays; this is a new consented reservation, not an old secret.
        count=len([p for p,_ in provider.requests if p=='/link/token/create'])
        assert call(c,'link_token',rid='link',consent_sandbox=True).status_code==200
        assert len([p for p,_ in provider.requests if p=='/link/token/create'])==count+1


def test_profile_all_existing_state_routes_and_forged_observations(tmp_path):
    provider=Provider()
    with TestClient(app_for(tmp_path,provider),base_url='http://127.0.0.1') as c:
        connect(c);call(c,'sync')
        s=get_state(c)
        common={'schema_version':'1','data_epoch':s['data_epoch']}
        r=c.put('/preferences',json={**common,'request_id':'prefs','preferences':s['preferences']})
        assert r.status_code==200; validate_profile('state',r.json())
        r=c.post('/process',json={**common,'request_id':'process','synthesis_mode':'disabled'})
        assert r.status_code==200;validate_profile('state',r.json())
        r=c.post('/observations',json={**common,'request_id':'empty','observations':[],'source_updates':[]})
        assert r.status_code==200;validate_profile('state',r.json()['state'])
        o=deepcopy(s['observations'][0])
        assert c.post('/observations',json={**common,'request_id':'forged','observations':[o],'source_updates':[]}).status_code==422
        event={'observation_id':o['id'],'revision':o['revision']}
        r=c.post('/feedback',json={**common,'request_id':'feedback','card_id':'purchase_recurrence','kind':'correctness','value':'correct','scope':'event','event_ref':event,'attribution':'self','note':''})
        assert r.status_code==200; validate_profile('state',r.json())


def test_demo_to_plaid_exits_fixed_clock_and_does_not_merge_purchase_sources(tmp_path):
    provider=Provider(); clock=[NOW+timedelta(hours=18)]
    from sample_data import enable_test_fixtures
    app = app_for(tmp_path,provider,clock=lambda:clock[0])
    enable_test_fixtures(app.state.service)
    with TestClient(app,base_url='http://127.0.0.1') as c:
        s=get_state(c)
        r=c.post('/demo',json={'schema_version':'1','request_id':'demo','data_epoch':s['data_epoch'],'action':'seed','fixture_set':'representative-v1','consent_demo':True})
        assert r.status_code==200 and r.json()['ledger'][0]['purchase_count']==3
        assert r.json()['sources'][2]['capability']=='available'
        connect(c)
        s=get_state(c)
        assert s['evaluation_date']=='2026-11-15'
        assert s['ledger'][0]['purchase_count']==0  # connected but import not ready; no demo fallback
        r=call(c,'sync')
        assert r.json()['state']['ledger'][0]['purchase_count']==1  # never adds demo's three
        ids={o['id'] for o in r.json()['state']['observations'] if o['source_id']=='plaid'}
        assert all(ref['observation_id'] in ids for ref in r.json()['state']['ledger'][0]['evidence'])


def test_expired_link_reservation_requires_fresh_link(tmp_path):
    clock=[NOW]
    provider=Provider()
    with TestClient(app_for(tmp_path,provider,clock=lambda:clock[0]),base_url='http://127.0.0.1') as c:
        call(c,'link_token',consent_sandbox=True)
        clock[0]+=timedelta(days=2)
        r=call(c,'exchange',public_token=PUBLIC)
        assert r.status_code==409 and r.json()['error']['code']=='consent_required'
        assert not any(p=='/item/public_token/exchange' for p,_ in provider.requests)
        assert not c.app.state.service.plaid_coordinator.busy


def test_ambiguous_exchange_invalidates_reservation_no_automatic_retry(tmp_path):
    provider=Provider(); provider.fail_exchange=True
    with TestClient(app_for(tmp_path,provider),base_url='http://127.0.0.1') as c:
        call(c,'link_token',consent_sandbox=True)
        assert call(c,'exchange',rid='exchange',public_token=PUBLIC).status_code==500
        assert call(c,'exchange',rid='exchange',public_token=PUBLIC).status_code==409
        assert sum(p=='/item/public_token/exchange' for p,_ in provider.requests)==1
        assert 'fresh' in get_state(c)['sources'][2]['reason'].lower() or 'new' in get_state(c)['sources'][2]['reason'].lower()


def test_mutation_retries_exhausted_without_partial_evidence(tmp_path):
    provider=Provider()
    with TestClient(app_for(tmp_path,provider),base_url='http://127.0.0.1') as c:
        connect(c)
        provider.pages=[{'error_code':'TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION'}]*3
        r=call(c,'sync')
        assert r.json()['result']['status']=='error' and r.json()['result']['retry_after_seconds']==5
        assert not get_state(c)['observations']
        assert sum(p=='/transactions/sync' for p,_ in provider.requests)==3


def test_pagination_has_global_twenty_page_budget(tmp_path):
    provider=Provider()
    with TestClient(app_for(tmp_path,provider),base_url='http://127.0.0.1') as c:
        connect(c)
        provider.pages=[page(cursor=str(i),has_more=True) for i in range(25)]
        r=call(c,'sync')
        assert r.json()['result']['status']=='error'
        assert sum(p=='/transactions/sync' for p,_ in provider.requests)==20
        assert not get_state(c)['observations']


def test_duplicate_contradictory_provider_rows_reject_cycle(tmp_path):
    provider=Provider()
    with TestClient(app_for(tmp_path,provider),base_url='http://127.0.0.1') as c:
        connect(c)
        provider.pages=[page([transaction(),transaction(merchant_name='Different')])]
        r=call(c,'sync')
        assert r.json()['result']['status']=='error' and not get_state(c)['observations']


def test_pending_removed_prior_cycle_posted_can_link_tombstone(tmp_path):
    provider=Provider()
    with TestClient(app_for(tmp_path,provider),base_url='http://127.0.0.1') as c:
        connect(c)
        provider.pages=[page([transaction('pending',pending=True)])]
        call(c,'sync')
        provider.pages=[page(removed=[{'transaction_id':'pending'}],cursor='c2')]
        call(c,'sync')
        provider.pages=[page([transaction('posted',pending_transaction_id='pending')],cursor='c3')]
        r=call(c,'sync')
        assert r.json()['result']['added']==1
        posted=next(o for o in r.json()['state']['observations'] if o['lifecycle']=='posted')
        assert posted['supersedes']['revision']==2
        assert r.json()['state']['ledger'][0]['purchase_count']==1


@pytest.mark.parametrize('delete', [False,True])
async def test_sync_race_revocation_or_deletion_never_publishes(tmp_path,delete):
    provider=Provider();provider.sync_entered=asyncio.Event();provider.sync_release=asyncio.Event()
    app=app_for(tmp_path,provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as c:
            s=(await c.get('/state')).json();base={'schema_version':'1','data_epoch':s['data_epoch']}
            async def action(name,rid,**fields):
                return await c.post('/connections/plaid',json={**base,'request_id':rid,'action':name,**fields})
            await action('link_token','link',consent_sandbox=True)
            await action('exchange','exchange',public_token=PUBLIC)
            syncing=asyncio.create_task(action('sync','sync'))
            await provider.sync_entered.wait()
            if delete:
                r=await c.request('DELETE','/data',json={**base,'request_id':'delete','confirm':'delete_all'})
            else:
                r=await action('disconnect','disconnect')
            assert r.status_code==200
            provider.sync_release.set()
            assert (await syncing).status_code==409
            final=(await c.get('/state')).json()
            assert not final['observations'] and not final['sources'][2]['enabled']


async def test_late_exchange_cleanup_failure_is_disclosed_without_tokens(tmp_path):
    provider=Provider();provider.exchange_entered=asyncio.Event();provider.exchange_release=asyncio.Event();provider.fail_remove=True
    app=app_for(tmp_path,provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as c:
            s=(await c.get('/state')).json();base={'schema_version':'1','data_epoch':s['data_epoch']}
            await c.post('/connections/plaid',json={**base,'request_id':'link','action':'link_token','consent_sandbox':True})
            exchange=asyncio.create_task(c.post('/connections/plaid',json={**base,'request_id':'exchange','action':'exchange','public_token':PUBLIC}))
            await provider.exchange_entered.wait()
            await c.request('DELETE','/data',json={**base,'request_id':'delete','confirm':'delete_all'})
            provider.exchange_release.set();assert (await exchange).status_code==409
            final=(await c.get('/state')).json()
            assert 'Remote Sandbox Item removal unconfirmed.' in final['limitations']
            assert ACCESS not in json.dumps(final) and not (tmp_path/'plaid.key').exists()


async def test_daily_sync_uses_elapsed_24h_and_restart_stable_attempt(tmp_path):
    clock=[NOW];provider=Provider();app=app_for(tmp_path,provider,clock=lambda:clock[0])
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as c:
            s=(await c.get('/state')).json();base={'schema_version':'1','data_epoch':s['data_epoch']}
            await c.post('/connections/plaid',json={**base,'request_id':'link','action':'link_token','consent_sandbox':True})
            await c.post('/connections/plaid',json={**base,'request_id':'exchange','action':'exchange','public_token':PUBLIC})
            coordinator=app.state.service.plaid_coordinator
            assert await coordinator.daily_sync() is None
            clock[0]+=timedelta(hours=24)
            assert (await coordinator.daily_sync())['result']['status']=='complete'
            assert await coordinator.daily_sync() is None
    app=app_for(tmp_path,provider,clock=lambda:clock[0])
    async with app.router.lifespan_context(app):
        assert await app.state.service.plaid_coordinator.daily_sync() is None
