"""Offline durable daily admission and explicit refresh-before-analysis regressions."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
from companion import banking_schedule as schedule
from companion.pattern_migrations import MIGRATION_V3
from companion.webhook_jobs import WebhookInbox,WebhookWorker
from test_postgres import local_pg_cluster,pg_dsn,runtime,event
from test_postgres_http import signing_key
from test_banking_postgres import setup,add,act
from test_auth_http import NOW
from test_pattern_bridges import consent,health_pages,send


async def internal_post(h,body):
    """Retained legacy core replay tests; public direct execution is now forbidden."""
    import httpx
    from companion.patterns import AnalyzeRequest
    from companion.contracts import DomainError
    try:
        coordinator=(await h.handle('a')).service.app.state.service.pattern_coordinator
        return httpx.Response(200,json=await coordinator.execute(AnalyzeRequest.model_validate(body)))
    except DomainError as exc:return httpx.Response(exc.status,json={'error':{'code':exc.code}})

def count(provider):return sum(p=='/transactions/sync' for p,_ in provider.calls)

async def prepared(tmp_path,signing_key,runtime):
    async with runtime.pool.connection() as c:await c.execute(MIGRATION_V3)
    return setup(tmp_path,signing_key,runtime)

async def test_daily_boundary_per_item_restart_explicit_override_and_inventory(tmp_path,signing_key,runtime):
    h,providers=await prepared(tmp_path,signing_key,runtime)
    async with h.client() as client:
        one=await add(client,h);handle=await h.handle('a');service=handle.service.app.state.service
        provider=providers[handle.account_id];clock=[NOW];service.clock=lambda:clock[0]
        assert count(provider)==0  # exchange is inventory, not transaction fetching
        bank=service.banking_coordinator
        await bank.daily_sync();assert count(provider)==2
        assert schedule.last_full_success(service.document)==NOW.isoformat().replace('+00:00','Z')
        persisted=deepcopy(service.document['meta']['banking_fetch_schedule'])
        await service.close();await service.start()
        assert service.document['meta']['banking_fetch_schedule']==persisted
        clock[0]=NOW+timedelta(hours=24)-timedelta(seconds=1)
        assert 'deferred_until' in await bank.daily_sync();assert count(provider)==2
        clock[0]+=timedelta(seconds=1)
        await bank.daily_sync();assert count(provider)==4
        await act(client,h,'sync',connection_id=one['id']);assert count(provider)==6
        # A newly owned Item is independently due, even though its sibling just synced.
        clock[0]=NOW  # The offline Link fixture has a fixed same-day expiry.
        await add(client,h,'two');clock[0]=NOW+timedelta(hours=24)
        await bank.daily_sync();assert count(provider)==8
        await bank.daily_sync();assert count(provider)==8
        before=count(provider)
        clock[0]=NOW
        link=await act(client,h,'update_link',connection_id=one['id'])
        await act(client,h,'reconcile',connection_id=one['id'],link_session_id=link['result']['link_session_id'])
        assert count(provider)==before

async def test_failure_backoff_has_no_false_success_and_is_durable(tmp_path,signing_key,runtime):
    h,providers=await prepared(tmp_path,signing_key,runtime)
    async with h.client() as client:
        await add(client,h);handle=await h.handle('a');service=handle.service.app.state.service
        provider=providers[handle.account_id];clock=[NOW];service.clock=lambda:clock[0]
        provider.fail.update({'same-provider-account','savings'})
        bank=service.banking_coordinator
        await bank.daily_sync();n=count(provider);assert n>0
        assert schedule.last_full_success(service.document) is None
        await bank.daily_sync();assert count(provider)==n
        clock[0]+=timedelta(hours=24);await bank.daily_sync();assert count(provider)>n
        n=count(provider);clock[0]+=timedelta(hours=24)
        await bank.daily_sync();assert count(provider)==n  # second failure backs off 48 hours
        assert all(v['failures']==2 for v in service.document['meta']['banking_fetch_schedule'].values())
        await service.close();await service.start();await bank.daily_sync();assert count(provider)==n

async def test_webhooks_coalesce_deferred_work_survives_expiry_and_quiet_items(tmp_path,signing_key,runtime):
    h,providers=await prepared(tmp_path,signing_key,runtime)
    async with h.client() as client:
        await add(client,h);handle=await h.handle('a');service=handle.service.app.state.service;provider=providers[handle.account_id]
        clock=[NOW];service.clock=lambda:clock[0]
        inbox=h.app.state.webhook_inbox;worker=h.app.state.webhook_worker;item='item-'+handle.account_id+'-one'
        await inbox.schedule_quiet_items();assert await worker.tick();assert count(provider)==2
        await inbox.enqueue_verified(event(item,1));assert await worker.tick();assert count(provider)==2
        async with runtime.pool.connection() as c:
            row=await (await c.execute('SELECT requested,completed,available_at FROM companion_private.webhook_jobs WHERE item_id=%s',(item,))).fetchone()
            assert row[0]>row[1]
        for n in range(2,5):await inbox.enqueue_verified(event(item,n))
        async with runtime.pool.connection() as c:
            after=await (await c.execute('SELECT available_at FROM companion_private.webhook_jobs WHERE item_id=%s',(item,))).fetchone()
            assert after[0]==row[2]  # notifications cannot undo admission deferral
            await c.execute("UPDATE companion_private.webhook_jobs SET expires_at=now()-interval '1 day' WHERE item_id=%s",(item,))
        await inbox.enqueue_verified(event(item,5))
        # New worker/inbox represents process restart; defer is still persisted.
        restarted=WebhookWorker(WebhookInbox(runtime),worker.callback)
        assert not await restarted.tick();assert count(provider)==2
        clock[0]+=timedelta(hours=24)
        async with runtime.pool.connection() as c:await c.execute("UPDATE companion_private.webhook_jobs SET available_at=now()-interval '1 second' WHERE item_id=%s",(item,))
        assert await restarted.tick();assert count(provider)==4
        async with runtime.pool.connection() as c:
            r=await (await c.execute('SELECT requested,completed FROM companion_private.webhook_jobs WHERE item_id=%s',(item,))).fetchone()
            assert r[0]==r[1]
        await inbox.schedule_quiet_items();assert await restarted.tick();assert count(provider)==4
        async with runtime.pool.connection() as c:
            r=await (await c.execute('SELECT requested,completed FROM companion_private.webhook_jobs WHERE item_id=%s',(item,))).fetchone()
            assert r[0]>r[1]  # quiet Item is queued for its next daily due time
            await c.execute("UPDATE companion_private.webhook_jobs SET attempts=12,available_at=now()+interval '24 hours' WHERE item_id=%s",(item,))
        await inbox.schedule_quiet_items()
        assert await inbox.claim() is None
        async with runtime.pool.connection() as c:await c.execute("UPDATE companion_private.webhook_jobs SET available_at=now()-interval '1 second' WHERE item_id=%s",(item,))
        await inbox.schedule_quiet_items()
        assert await restarted.tick();assert count(provider)==4  # exhausted scheduler work revives, but daily fetch gate still holds

async def test_analyze_off_on_replay_partial_failure_epoch_and_concurrent(tmp_path,signing_key,runtime):
    h,providers=await prepared(tmp_path,signing_key,runtime)
    async with h.client() as client:
        await add(client,h);handle=await h.handle('a');service=handle.service.app.state.service;provider=providers[handle.account_id]
        state=await consent(client,h)
        for page in health_pages(state['data_epoch']):assert (await send(client,h,page)).status_code==200
        calls=[]
        class Model:
            async def ainvoke(self,messages,**kwargs):
                calls.append(messages)
                return SimpleNamespace(content='{"concern_ids":["sleep","activity"]}')
        service.pattern_coordinator.model_factory=lambda:Model()
        def request(fetch):return {'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'fetch_latest_bank_data':fetch}
        async def post(body):return await internal_post(h,body)
        off=request(False);r=await post(off);assert r.status_code==200,r.text
        assert count(provider)==0 and len(calls)==1 and r.json()['bank_refresh']['status']=='not_requested'
        on=request(True);provider.entered=asyncio.Event();provider.release=asyncio.Event()
        running=asyncio.create_task(post(on));await provider.entered.wait()
        assert (await post(on)).status_code==409
        provider.release.set();r=await running;assert r.status_code==200,r.text
        assert r.json()['bank_refresh']['status']=='complete' and r.json()['bank_refresh']['last_updated']
        n=count(provider);m=len(calls)
        assert n==2
        assert (await post(on)).json()==r.json();assert count(provider)==n and len(calls)==m
        assert (await post({**on,'fetch_latest_bank_data':False})).status_code==409
        assert (await post({**request(True),'data_epoch':str(uuid4())})).status_code==409
        assert count(provider)==n
        provider.entered=None;provider.fail.update({'same-provider-account','savings'})
        failed=request(True);r=await post(failed);assert r.status_code==200,r.text
        assert r.json()['bank_refresh']['status']=='partial' and len(calls)==m
        n=count(provider);assert (await post(failed)).json()==r.json();assert count(provider)==n
        assert (await post(request(False))).status_code==200;assert count(provider)==n
        # An orphaned action reservation must never repeat either external effect.
        orphan=request(True)
        async with service.lock:
            receipt=service.receipt('/patterns/v1/analyze',orphan)
            await service.commit(deepcopy(service.document),receipt,bump=False)
        await service.close();await service.start()
        r=await post(orphan);assert r.status_code==200,r.text
        assert r.json()['bank_refresh']['status']=='failed' and count(provider)==n

@pytest.mark.parametrize('interrupted',[False,True])
async def test_action_binding_survives_observation_prune_and_receipt_age_until_reset(tmp_path,signing_key,runtime,monkeypatch,interrupted):
    from companion.patterns import AnalyzeRequest
    h,providers=await prepared(tmp_path,signing_key,runtime)
    async with h.client() as client:
        await add(client,h);handle=await h.handle('a');service=handle.service.app.state.service
        provider=providers[handle.account_id];state=await consent(client,h)
        for page in health_pages(state['data_epoch']):assert (await send(client,h,page)).status_code==200
        calls=[]
        class Model:
            async def ainvoke(self,*args,**kwargs):
                calls.append(True);return SimpleNamespace(content='{"concern_ids":["sleep","activity"]}')
        coordinator=service.pattern_coordinator;coordinator.model_factory=lambda:Model()
        body={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'fetch_latest_bank_data':True}
        if interrupted:
            async def crash(*args,**kwargs):raise RuntimeError('Simulated stop after bank commit')
            with monkeypatch.context() as patch:
                patch.setattr(coordinator,'analyze',crash)
                with pytest.raises(RuntimeError):await coordinator.execute(AnalyzeRequest.model_validate(body))
        else:
            response=await internal_post(h,body)
            assert response.status_code==200,response.text
            assert response.json()['bank_refresh']['status']=='complete'
        n=count(provider);m=len(calls);assert n==2 and m==(0 if interrupted else 1)
        # Exercise the real housekeep -> prune -> commit(erase=True) path without epoch rotation.
        async with service.lock:
            doc=deepcopy(service.document);expired=deepcopy(doc['state']['observations'][0])
            expired['id']='unrelated-expired-observation';expired['local_date']=(NOW-timedelta(days=200)).date().isoformat()
            doc['state']['observations'].append(expired)
            await service.commit(doc)
        await service.housekeep()
        assert service.document['state']['data_epoch']==body['data_epoch']
        assert all(o['id']!=expired['id'] for o in service.document['state']['observations'])
        async with coordinator.transaction() as c:
            rows=await (await c.execute("SELECT request_id,response FROM companion_private.receipts WHERE request_id IN (%s,%s)",(body['request_id'],'pattern-bank:'+body['request_id']))).fetchall()
            assert len(rows)==2 and all(row[1] is None for row in rows)
            await c.execute("UPDATE companion_private.receipts SET created_at=now()-interval '400 days' WHERE request_id IN (%s,%s)",(body['request_id'],'pattern-bank:'+body['request_id']))
        # The normal 90-day receipt cutoff cannot re-admit the request in the same epoch either.
        async with service.lock:await service.commit(deepcopy(service.document),bump=False,recover=True)
        await service.close();await service.start()
        replay=await internal_post(h,body)
        assert replay.status_code==200,replay.text
        assert replay.json()['bank_refresh']['status']=='failed'
        assert count(provider)==n and len(calls)==m
        conflict=await internal_post(h,{**body,'fetch_latest_bank_data':False})
        assert conflict.status_code==409 and conflict.json()['error']['code']=='idempotency_conflict'
        async with coordinator.transaction() as c:
            rows=await (await c.execute("SELECT response FROM companion_private.receipts WHERE request_id IN (%s,%s)",(body['request_id'],'pattern-bank:'+body['request_id']))).fetchall()
            assert len(rows)==2 and all(row[0] is None for row in rows)  # no private full response rehydrated
        deleted=await client.request('DELETE','/data',headers=h.headers(),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':body['data_epoch'],'confirm':'delete_all'})
        assert deleted.status_code==200,deleted.text
        async with coordinator.transaction() as c:
            assert (await (await c.execute("SELECT count(*) FROM companion_private.receipts WHERE request_id IN (%s,%s)",(body['request_id'],'pattern-bank:'+body['request_id']))).fetchone())[0]==0
        assert (await internal_post(h,body)).status_code==409
        assert count(provider)==n and len(calls)==m
