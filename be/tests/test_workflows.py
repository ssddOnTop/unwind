"""Offline UTC queue, permission, cancellation and scoped PostgreSQL regressions."""
import asyncio,json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
from psycopg import sql
from companion.workflow_contracts import WorkflowConfig,WorkflowOverview
from companion.workflow_migrations import MIGRATION_V4
from companion.pattern_migrations import MIGRATION_V3
from companion.workflows import WorkflowQueue,WorkflowWorker
from companion.postgres import PostgresRuntime,PostgresConfig
from test_postgres import local_pg_cluster,pg_dsn,runtime
from test_postgres_http import signing_key
from test_auth_http import NOW,ISSUER
from test_banking_postgres import setup,add,act
from test_pattern_bridges import consent,health_pages,send
from scripted_models import review_model

@pytest.fixture
async def app(tmp_path,signing_key,runtime):
    async with runtime.pool.connection() as c:
        await c.execute(MIGRATION_V3);await c.execute(MIGRATION_V4);await c.execute(MIGRATION_V4)
    account=await runtime.identity(SimpleNamespace(issuer=ISSUER,subject='a'))
    h,providers=setup(tmp_path,signing_key,runtime,workflow_config=WorkflowConfig(True,frozenset({account})))
    clock=[NOW];q=h.app.state.workflow_queue;q.clock=lambda:clock[0]
    return h,providers,clock

async def seed(client,h,who='a'):
    state=await consent(client,h,who)
    preferences=deepcopy(state['preferences']);preferences['cloud_consent']=True
    r=await client.put('/preferences',headers=h.headers(who),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'preferences':preferences})
    assert r.status_code==200,r.text
    state=r.json()
    for page in health_pages(state['data_epoch']):assert (await send(client,h,page,who)).status_code==200
    return state

async def enqueue(client,h,state,who='a',**extra):
    return await client.post('/workflows/v1/enqueue',headers=h.headers(who),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],**extra})

async def test_default_deny_self_only_capability_strict_selector_and_legacy_rejection(app):
    h,providers,clock=app
    async with h.client() as client:
        assert (await client.get('/workflows/v1')).status_code==401
        a=await seed(client,h);b=await seed(client,h,'b')
        view=(await client.get('/workflows/v1',headers=h.headers('a'))).json();WorkflowOverview.model_validate(view)
        assert view['action']['id']=='enqueue' and {p['id'] for p in view['pages']}=={'home','ledger','brief','settings'}
        assert (await client.get('/workflows/v1',headers=h.headers('b'))).json()['action'] is None
        assert (await enqueue(client,h,b,'b')).status_code==403
        assert (await enqueue(client,h,a,account_id=(await h.handle('b')).account_id)).status_code==422
        for who,state in [('a',a),('b',b)]:
            r=await client.post('/patterns/v1/analyze',headers=h.headers(who),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'fetch_latest_bank_data':True})
            assert r.status_code==403
            r=await client.post('/process',headers=h.headers(who),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'synthesis_mode':'live'})
            assert r.status_code==403
        r=await enqueue(client,h,a);assert r.status_code==200,r.text
        assert r.json()['job']['status']=='queued' and r.json()['report']['status']=='not_analyzed'
        assert all(not p.calls for p in providers.values())

async def test_utc_boundary_concurrent_ticks_restart_current_day_only_and_rls(app,runtime):
    h,_,clock=app
    async with h.client() as client:
        state=await seed(client,h);q=h.app.state.workflow_queue;account=(await h.handle('a')).account_id
        clock[0]=NOW.replace(hour=23,minute=59,second=59)
        await asyncio.gather(q.scan(),q.scan())
        async with q.scoped(account) as c:
            rows=await (await c.execute('SELECT utc_date FROM companion_private.pattern_jobs')).fetchall()
            assert len(rows)==1 and rows[0][0]==clock[0].date()
        clock[0]+=timedelta(seconds=1);await q.scan()
        restarted=WorkflowQueue(runtime,q.config,lambda:clock[0]);await restarted.scan()
        async with q.scoped(account) as c:assert (await (await c.execute('SELECT count(*) FROM companion_private.pattern_jobs')).fetchone())[0]==2
        clock[0]+=timedelta(days=8);await restarted.scan()
        async with q.scoped(account) as c:assert (await (await c.execute('SELECT count(*) FROM companion_private.pattern_jobs')).fetchone())[0]==3
        job=await restarted.claim(account);assert job
        async with q.scoped(account) as c:
            rows=await (await c.execute("SELECT utc_date,status FROM companion_private.pattern_jobs ORDER BY utc_date")).fetchall()
            assert [r[1] for r in rows]==['cancelled','cancelled','running']
            assert rows[-1][0]==clock[0].date()  # no historical execution storm
        # Genuine non-bypass role proves FORCE RLS, including unknown scope.
        role='workflow_'+uuid4().hex
        async with runtime.pool.connection() as c:
            await c.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
            await c.execute(sql.SQL('GRANT USAGE ON SCHEMA companion_private TO {}').format(sql.Identifier(role)))
            await c.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON companion_private.pattern_jobs TO {}').format(sql.Identifier(role)))
        restricted=PostgresRuntime(PostgresConfig(runtime.config.dsn+' user='+role,local_test=True));await restricted.open()
        try:
            async with restricted.pool.connection() as c:
                assert (await (await c.execute('SELECT count(*) FROM companion_private.pattern_jobs')).fetchone())[0]==0
                async with c.transaction():
                    await c.execute("SELECT set_config('companion.account_id',%s,true)",(account,))
                    assert (await (await c.execute('SELECT count(*) FROM companion_private.pattern_jobs')).fetchone())[0]==3
                    forced=await (await c.execute("SELECT relforcerowsecurity FROM pg_class WHERE relname='pattern_jobs'")).fetchone();assert forced[0]
        finally:await restricted.close()

async def test_worker_cache_manual_nightly_race_bank_gate_and_no_raw_prompt(app):
    h,providers,clock=app
    async with h.client() as client:
        state=await seed(client,h);await add(client,h);await act(client,h,'sync',connection_id=None)
        handle=await h.handle('a');service=handle.service.app.state.service;provider=providers[handle.account_id];q=h.app.state.workflow_queue
        calls=[]
        class Model:
            async def ainvoke(self,messages,**kwargs):
                calls.append(messages);return SimpleNamespace(content='{"concern_ids":["sleep","activity"]}')
        service.pattern_coordinator.model_factory=lambda:review_model(Model())
        r=await enqueue(client,h,state);assert r.status_code==200
        await q.scan()
        first,second=await asyncio.gather(q.claim(handle.account_id),q.claim(handle.account_id))
        assert bool(first)!=bool(second)
        before=len(provider.calls);worker=h.app.state.workflow_worker
        await worker.run(first or second)
        next_job=await q.claim(handle.account_id);assert next_job
        await worker.run(next_job)
        assert len(calls)==1 and len(provider.calls)==before
        async with q.scoped(handle.account_id) as c:
            rows=await (await c.execute('SELECT status,bank_status,run_id FROM companion_private.pattern_jobs')).fetchall()
            assert len(rows)==2 and all(r[0]=='complete' and r[1]=='saved' for r in rows)
            assert len({r[2] for r in rows})==1
        prompt=str(calls)
        assert all(secret not in prompt for secret in [handle.account_id,'PRIVATE','2026-','org.localcompanion','access-sandbox'])
        # Unknown running lease is terminalized, never repeated.
        await enqueue(client,h,state);job=await q.claim(handle.account_id);assert job
        clock[0]+=timedelta(minutes=6)
        assert await q.claim(handle.account_id) is None
        assert (await q.latest(handle.account_id,state['data_epoch'])).status=='unavailable'
        assert len(calls)==1

async def test_consent_cancellation_and_delete_fence_job_and_results(app):
    h,_,clock=app
    async with h.client() as client:
        state=await seed(client,h);handle=await h.handle('a');service=handle.service.app.state.service;q=h.app.state.workflow_queue
        entered=asyncio.Event();calls=[]
        class Slow:
            async def ainvoke(self,*args,**kwargs):
                calls.append(True);entered.set();await asyncio.Event().wait()
        service.pattern_coordinator.model_factory=lambda:review_model(Slow())
        await enqueue(client,h,state);job=await q.claim(handle.account_id)
        task=asyncio.create_task(h.app.state.workflow_worker.run(job));await asyncio.wait_for(entered.wait(),10)
        preferences=deepcopy(service.document['state']['preferences']);preferences['cloud_consent']=False
        r=await client.put('/preferences',headers=h.headers(),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'preferences':preferences});assert r.status_code==200
        await asyncio.wait_for(task,5)
        assert (await q.latest(handle.account_id,state['data_epoch'])).status=='cancelled' and len(calls)==1
        assert (await enqueue(client,h,state)).status_code==403
        r=await client.request('DELETE','/data',headers=h.headers(),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'confirm':'delete_all'});assert r.status_code==200
        async with q.scoped(handle.account_id) as c:
            assert (await (await c.execute('SELECT count(*) FROM companion_private.pattern_jobs')).fetchone())[0]==0
        assert not await q.current(job)

async def test_unloaded_account_worker_borrow_is_bounded_and_closed(app):
    h,_,_=app
    async with h.client() as client:
        state=await seed(client,h);handle=await h.handle('a');account=handle.account_id;q=h.app.state.workflow_queue
        await q.scan()
        # Remove only this disposable test handle to represent a never-opened account after restart.
        async with h.app.state.accounts._lock:
            await handle.service.close();del h.app.state.accounts._handles[account]
        assert account not in h.app.state.accounts._handles
        async with h.app.state.accounts.borrow_account(account,temporary=True) as opened:
            assert opened.account_id==account
            # HTTP can pin the same handle without a second lifetime DB owner.
            async with h.app.state.accounts.borrow_account(account) as shared:assert shared is opened
        assert account not in h.app.state.accounts._handles
        job=await q.claim(account);assert job
        # No provider is invoked in this test: deny consent before starting the queued job.
        async with q.scoped(account) as c:
            await c.execute("UPDATE companion_private.snapshots SET body=jsonb_set(body,'{state,preferences,cloud_consent}','false') WHERE account_id=%s",(account,))
        await h.app.state.workflow_worker.run(job)
        assert (await q.latest(account,state['data_epoch'])).status=='cancelled'
        assert account not in h.app.state.accounts._handles


async def test_private_operator_explicit_verified_target_audit_and_no_public_elevation(app,runtime,monkeypatch,capsys):
    from companion.workflow_operator import run
    from datetime import datetime,timezone
    h,_,_=app
    async with h.client() as client:
        state=await seed(client,h,'b');account=(await h.handle('b')).account_id
        assert (await enqueue(client,h,state,'b',origin='operator')).status_code==422
        role='operator_'+uuid4().hex
        async with runtime.pool.connection() as c:
            await c.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
            await c.execute(sql.SQL('GRANT USAGE ON SCHEMA companion_private TO {}').format(sql.Identifier(role)))
            await c.execute(sql.SQL('GRANT SELECT ON companion_private.accounts,companion_private.schema_version TO {}').format(sql.Identifier(role)))
            await c.execute(sql.SQL('GRANT SELECT,UPDATE ON companion_private.snapshots TO {}').format(sql.Identifier(role)))
            await c.execute(sql.SQL('GRANT SELECT,INSERT ON companion_private.pattern_jobs TO {}').format(sql.Identifier(role)))
        config=PostgresConfig(runtime.config.dsn+' user='+role,local_test=True)
        monkeypatch.setattr(PostgresConfig,'from_env',classmethod(lambda cls:config))
        args=SimpleNamespace(account_id=account,issuer=ISSUER,subject='wrong',request_id=str(uuid4()),confirm_enqueue_utc_date=datetime.now(timezone.utc).date().isoformat())
        with pytest.raises(ValueError):await run(args)
        args.subject='b';await run(args);await run(args)
        assert account not in capsys.readouterr().out
        async with h.app.state.workflow_queue.scoped(account) as c:
            rows=await (await c.execute('SELECT origin,actor_role,status FROM companion_private.pattern_jobs')).fetchall()
            assert rows==[('operator',role,'queued')]


def test_workflow_configuration_defaults_to_no_manual_privilege(monkeypatch):
    monkeypatch.delenv('COMPANION_PATTERN_WORKFLOWS',raising=False)
    monkeypatch.delenv('COMPANION_PATTERN_MANUAL_ACCOUNTS',raising=False)
    assert WorkflowConfig.from_env()==WorkflowConfig()
    monkeypatch.setenv('COMPANION_PATTERN_MANUAL_ACCOUNTS','person@example.invalid')
    with pytest.raises(ValueError):WorkflowConfig.from_env()

async def test_bounded_manual_admission_and_pre_execution_retry_limit(app):
    from contextlib import asynccontextmanager
    from companion.accounts import AccountError
    h,_,clock=app
    async with h.client() as client:
        state=await seed(client,h);account=(await h.handle('a')).account_id;q=h.app.state.workflow_queue
        responses=await asyncio.gather(*(enqueue(client,h,state) for _ in range(10)))
        assert sum(r.status_code==200 for r in responses)==8
        assert sum(r.status_code==409 for r in responses)==2
        class Unavailable:
            @asynccontextmanager
            async def borrow_account(self,*args,**kwargs):
                raise AccountError('account_capacity')
                yield
        worker=WorkflowWorker(q,Unavailable())
        first=await q.claim(account);assert first
        await worker.run(first)
        async with q.scoped(account) as c:
            row=await (await c.execute('SELECT status,attempts FROM companion_private.pattern_jobs WHERE job_id=%s',(first['job_id'],))).fetchone()
            assert row==('queued',1)
            # Remove other disposable requests to isolate retries of this exact reservation.
            await c.execute('DELETE FROM companion_private.pattern_jobs WHERE job_id<>%s',(first['job_id'],))
        for delay in (31,61):
            clock[0]+=timedelta(seconds=delay)
            job=await q.claim(account);assert job and job['job_id']==first['job_id']
            await worker.run(job)
        assert (await q.latest(account,state['data_epoch'])).status=='failed'
        clock[0]+=timedelta(hours=1)
        assert await q.claim(account) is None

async def test_delete_during_model_call_cannot_publish_old_epoch_or_requeue(app):
    h,_,_=app
    async with h.client() as client:
        state=await seed(client,h);handle=await h.handle('a');service=handle.service.app.state.service;q=h.app.state.workflow_queue
        entered=asyncio.Event();calls=[]
        class Slow:
            async def ainvoke(self,*args,**kwargs):
                calls.append(True);entered.set();await asyncio.Event().wait()
        service.pattern_coordinator.model_factory=lambda:review_model(Slow())
        await enqueue(client,h,state);job=await q.claim(handle.account_id)
        pending=asyncio.create_task(h.app.state.workflow_worker.run(job));await asyncio.wait_for(entered.wait(),10)
        result=await client.request('DELETE','/data',headers=h.headers(),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'confirm':'delete_all'})
        assert result.status_code==200,result.text
        await asyncio.wait_for(pending,5);await q.scan()
        async with q.scoped(handle.account_id) as c:
            for table in ('pattern_jobs','pattern_runs','pattern_requests','pattern_insights'):
                assert (await (await c.execute(sql.SQL('SELECT count(*) FROM companion_private.{}').format(sql.Identifier(table)))).fetchone())[0]==0
        assert len(calls)==1
        assert (await client.get('/workflows/v1',headers=h.headers())).json()['action'] is None
