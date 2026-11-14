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
        async def post(body):return await client.post('/patterns/v1/analyze',headers=h.headers(),json=body)
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
