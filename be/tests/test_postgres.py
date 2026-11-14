"""Real disposable local PostgreSQL only. Never reads DATABASE_URL or migrates remote data."""
import asyncio
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from companion.postgres import PostgresConfig,PostgresRuntime,PostgresUnavailable
from companion.extensions.postgres import PostgresPersistenceExtension
from companion.webhook_jobs import WebhookInbox,WebhookWorker
from companion.plaid_webhooks import VerifiedPlaidEvent
from companion.postgres_migrations import MIGRATION_V1

ROOT=Path(__file__).resolve().parents[2]


@pytest.fixture(scope='session')
def local_pg_cluster(tmp_path_factory):
    binary=Path('/opt/homebrew/opt/postgresql@18/bin')
    if not (binary/'postgres').exists():
        found=shutil.which('postgres')
        if not found: pytest.skip('Local PostgreSQL server binary unavailable; not a remote DB test')
        binary=Path(found).parent
    directory=tmp_path_factory.mktemp('disposable-pg')
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    init=subprocess.run([str(binary/'initdb'),'-D',str(directory/'data'),'-A','trust','--no-locale','-E','UTF8'],capture_output=True)
    if init.returncode: pytest.fail('Disposable PostgreSQL initialization failed (details withheld)')
    command=[str(binary/'pg_ctl'),'-D',str(directory/'data'),'-l',str(directory/'server.log')]
    options=f"-h 127.0.0.1 -p {port} -c unix_socket_directories='' -c max_connections=40 -c log_statement=none -c log_min_error_statement=panic"
    result=subprocess.run([*command,'-o',options,'-w','start'],capture_output=True)
    if result.returncode: pytest.fail('Disposable PostgreSQL startup failed (details withheld)')
    try:yield f'host=127.0.0.1 port={port} dbname=postgres sslmode=disable'
    finally:
        result=subprocess.run([*command,'-m','fast','-w','stop'],capture_output=True)
        if result.returncode:pytest.fail('Disposable PostgreSQL shutdown failed')


@pytest.fixture
def pg_dsn(local_pg_cluster):
    name='test_'+uuid4().hex
    with psycopg.connect(local_pg_cluster,autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier(name)))
    dsn=local_pg_cluster.replace('dbname=postgres','dbname='+name)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute("""DO $$ BEGIN
            IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon NOLOGIN; END IF;
            IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF;
            END $$;""")
        conn.execute(MIGRATION_V1)
    return dsn


@pytest.fixture
async def runtime(pg_dsn):
    runtime=PostgresRuntime(PostgresConfig(pg_dsn,local_test=True))
    await runtime.check()
    yield runtime
    await runtime.close()


def document(epoch='same-epoch', *, item=None, marker='synthetic'):
    return {'state':{'data_epoch':epoch,'revision':1,'private_test':marker},'meta':{'plaid_connected':bool(item),'plaid_item_id':item,'plaid_vault':'encrypted-ciphertext-only' if item else None}}


def receipt(epoch='same-epoch',value='one'):
    return {'epoch':epoch,'route':'/test','request_id':'same-request','digest':'digest','response':{'value':value},'created_at':'2026-11-14T12:00:00Z'}


def event(item='sandbox_item',n=1):
    return VerifiedPlaidEvent(item,'sandbox','TRANSACTIONS','SYNC_UPDATES_AVAILABLE',hashlib.sha256(str(n).encode()).hexdigest(),datetime.now(timezone.utc))


async def account(runtime,sub='a',issuer='https://issuer.invalid'):
    # Persistence unit tests: a trusted stand-in, never used as an HTTP Principal.
    return await runtime.identity(SimpleNamespace(issuer=issuer,subject=sub))


@pytest.mark.parametrize('dsn',['host=example.invalid sslmode=disable','host=example.invalid sslmode=require','hostaddr=127.0.0.1 host=example.invalid','not a dsn'])
def test_invalid_or_unverified_config_is_sanitized(dsn):
    with pytest.raises(ValueError) as failure:PostgresConfig(dsn)
    assert dsn not in str(failure.value)
    assert 'password' not in str(failure.value)


async def test_identity_globally_stable_issuer_scoped_and_no_sqlite(runtime):
    values=await asyncio.gather(*(account(runtime) for _ in range(8)))
    assert len(set(values))==1
    assert await account(runtime,'a','https://another.invalid')!=values[0]
    assert await account(runtime,'b')!=values[0]


async def test_snapshots_receipts_same_epoch_ids_isolated_and_restart(runtime):
    a,b=await account(runtime),await account(runtime,'b')
    sa,sb=PostgresPersistenceExtension(runtime,a),PostgresPersistenceExtension(runtime,b)
    try:
        await asyncio.gather(sa.handle('commit',{'document':document(marker='a'),'receipt':receipt(value='a')}),sb.handle('commit',{'document':document(marker='b'),'receipt':receipt(value='b')}))
        assert (await sa.handle('load',{}))['state']['private_test']=='a'
        assert (await sb.handle('receipt',receipt()))['response']=={'value':'b'}
        await sa.handle('commit',{'document':document('new-epoch'),'erase_receipts':True})
        assert await sa.handle('receipt',receipt())=={}
        assert (await sb.handle('load',{}))['state']['private_test']=='b'
    finally:await sa.handle('close',{});await sb.handle('close',{})
    restored=PostgresPersistenceExtension(runtime,a)
    try:assert (await restored.handle('load',{}))['state']['data_epoch']=='new-epoch'
    finally:await restored.handle('close',{})


async def test_atomic_owner_collision_rolls_back_snapshot_receipt_and_private_state(runtime):
    a,b=await account(runtime),await account(runtime,'b')
    sa,sb=PostgresPersistenceExtension(runtime,a),PostgresPersistenceExtension(runtime,b)
    try:
        await sa.handle('commit',{'document':document(item='item_unique'),'receipt':receipt()})
        await sb.handle('commit',{'document':document(marker='b')})
        with pytest.raises(PostgresUnavailable):
            await sb.handle('commit',{'document':document(item='item_unique'),'receipt':receipt()})
        assert not (await sb.handle('load',{}))['meta']['plaid_connected']
        assert await sb.handle('receipt',receipt())=={}
        assert (await sa.handle('load',{}))['meta']['plaid_vault']=='encrypted-ciphertext-only'
    finally:await sa.handle('close',{});await sb.handle('close',{})


async def test_advisory_lock_cross_process_and_handoff(runtime,pg_dsn):
    a=await account(runtime)
    store=PostgresPersistenceExtension(runtime,a)
    await store.handle('commit',{'document':document()})
    script='''import asyncio,os,json
from companion.postgres import PostgresRuntime,PostgresConfig
from companion.extensions.postgres import PostgresPersistenceExtension
async def main():
 r=PostgresRuntime(PostgresConfig(os.environ['LOCAL_TEST_DSN'],local_test=True))
 s=PostgresPersistenceExtension(r,os.environ['LOCAL_TEST_ACCOUNT'])
 try:
  await s.handle('load',{}); print(json.dumps({'acquired':True}))
 except Exception:print(json.dumps({'acquired':False}))
 finally:await s.handle('close',{});await r.close()
asyncio.run(main())'''
    env={**os.environ,'LOCAL_TEST_DSN':pg_dsn,'LOCAL_TEST_ACCOUNT':a}
    async def child():
        p=await asyncio.create_subprocess_exec(sys.executable,'-c',script,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        stdout,stderr=await p.communicate();assert p.returncode==0
        return json.loads(stdout)['acquired']
    assert not await child()
    await store.handle('close',{})
    assert await child()


async def test_no_open_transaction_over_external_work_and_connection_loss_fails_closed(runtime):
    a=await account(runtime);s=PostgresPersistenceExtension(runtime,a)
    await s.handle('commit',{'document':document()})
    assert s.conn.info.transaction_status==psycopg.pq.TransactionStatus.IDLE
    await s.conn.close()
    with pytest.raises(PostgresUnavailable):await s.handle('commit',{'document':document('late')})
    await s.handle('close',{})


async def test_queue_dedupe_coalesce_arrival_during_running_and_lease_recovery(runtime):
    a=await account(runtime);s=PostgresPersistenceExtension(runtime,a);q=WebhookInbox(runtime)
    try:
        await s.handle('commit',{'document':document(item='sandbox_item')})
        assert not (await q.enqueue_verified(event()))['duplicate']
        assert (await q.enqueue_verified(event()))['duplicate']
        first=await q.claim();assert first['account_id']==a
        assert await q.claim() is None
        await q.enqueue_verified(event(n=2))
        await q.finish(first,True)
        second=await q.claim();assert second['requested']==2
        async with runtime.pool.connection() as conn:
            await conn.execute("UPDATE companion_private.webhook_jobs SET lease_until=now()-interval '1 second' WHERE account_id=%s",(a,))
        reclaimed=await q.claim();assert reclaimed['token']!=second['token']
        await q.finish(second,True)
        assert await q.claim() is None
        await q.finish(reclaimed,True)
        assert await q.claim() is None
    finally:await s.handle('close',{})


async def test_unknown_quarantine_does_not_create_identity_then_atomic_exchange_releases(runtime):
    q=WebhookInbox(runtime)
    await q.enqueue_verified(event('unknown'))
    assert await q.claim() is None
    async with runtime.pool.connection() as conn:
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.accounts')).fetchone())[0]==0
    a=await account(runtime);s=PostgresPersistenceExtension(runtime,a)
    try:
        await s.handle('commit',{'document':document(item='unknown')})
        job=await q.claim();assert job['account_id']==a
    finally:await s.handle('close',{})


async def test_revoke_delete_fence_jobs_and_cannot_reassign_item(runtime):
    a,b=await account(runtime),await account(runtime,'b')
    sa,sb=PostgresPersistenceExtension(runtime,a),PostgresPersistenceExtension(runtime,b);q=WebhookInbox(runtime)
    try:
        await sa.handle('commit',{'document':document(item='sandbox_item')})
        await q.enqueue_verified(event());job=await q.claim()
        await sa.handle('commit',{'document':document('deleted'),'erase_receipts':True})
        assert not await q.current(job)
        await q.finish(job,True)
        assert await q.claim() is None
        await q.enqueue_verified(event(n=3));assert await q.claim() is None
        with pytest.raises(PostgresUnavailable):await sb.handle('commit',{'document':document(item='sandbox_item')})
    finally:await sa.handle('close',{});await sb.handle('close',{})


async def test_worker_retries_no_inline_sync_and_fences_stale_owner(runtime):
    a=await account(runtime);s=PostgresPersistenceExtension(runtime,a);q=WebhookInbox(runtime);calls=[]
    async def callback(job):calls.append(job);raise RuntimeError('synthetic failure')
    w=WebhookWorker(q,callback)
    try:
        await s.handle('commit',{'document':document(item='sandbox_item')})
        await q.enqueue_verified(event());assert not calls
        assert await w.tick();assert len(calls)==1
        assert await q.claim() is None
        async with runtime.pool.connection() as conn:
            await conn.execute('UPDATE companion_private.webhook_jobs SET available_at=now() WHERE account_id=%s',(a,))
        assert await w.tick();assert len(calls)==2
    finally:await s.handle('close',{})


async def test_rls_defense_non_bypass_role_and_database_owner_immutability(runtime):
    a,b=await account(runtime),await account(runtime,'b')
    sa,sb=PostgresPersistenceExtension(runtime,a),PostgresPersistenceExtension(runtime,b)
    try:
        await sa.handle('commit',{'document':document(item='immutable')})
        await sb.handle('commit',{'document':document(marker='b')})
        async with runtime.pool.connection() as conn:
            role='test_role_'+uuid4().hex
            await conn.execute(psycopg.sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS').format(psycopg.sql.Identifier(role)))
            await conn.execute(psycopg.sql.SQL('GRANT USAGE ON SCHEMA companion_private TO {}').format(psycopg.sql.Identifier(role)))
            await conn.execute(psycopg.sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA companion_private TO {}').format(psycopg.sql.Identifier(role)))
            async with conn.transaction():
                await conn.execute(psycopg.sql.SQL('SET LOCAL ROLE {}').format(psycopg.sql.Identifier(role)))
                assert (await (await conn.execute('SELECT count(*) FROM companion_private.snapshots')).fetchone())[0]==0
                await conn.execute("SELECT set_config('companion.account_id',%s,true)",(a,))
                rows=await (await conn.execute('SELECT account_id FROM companion_private.snapshots')).fetchall()
                assert [str(r[0]) for r in rows]==[a]
                assert not await (await conn.execute('SELECT body FROM companion_private.snapshots WHERE account_id=%s',(b,))).fetchall()
            with pytest.raises(psycopg.Error):
                async with conn.transaction():
                    await conn.execute('UPDATE companion_private.plaid_owners SET account_id=%s WHERE account_id=%s',(b,a))
            for api_role in ('anon','authenticated'):
                assert not (await (await conn.execute("SELECT has_schema_privilege(%s,'companion_private','USAGE')",(api_role,))).fetchone())[0]
                assert not (await (await conn.execute("SELECT has_table_privilege(%s,'companion_private.snapshots','SELECT')",(api_role,))).fetchone())[0]
    finally:await sa.handle('close',{});await sb.handle('close',{})


async def test_repeated_identical_body_during_lease_still_requests_another_pass(runtime):
    a=await account(runtime);s=PostgresPersistenceExtension(runtime,a);q=WebhookInbox(runtime)
    try:
        await s.handle('commit',{'document':document(item='sandbox_item')})
        await q.enqueue_verified(event());job=await q.claim()
        assert (await q.enqueue_verified(event()))['duplicate']
        await q.finish(job,True)
        another=await q.claim()
        assert another['requested']==2
        await q.finish(another,True)
    finally:await s.handle('close',{})


async def test_quarantine_visibility_race_reconciles_and_expired_unknowns_never_route(runtime):
    a=await account(runtime);s=PostgresPersistenceExtension(runtime,a);q=WebhookInbox(runtime)
    try:
        await s.handle('commit',{'document':document(item='sandbox_item')})
        await q.enqueue_verified(event());
        # Model the event's previously captured missing-owner view becoming visible after exchange.
        async with runtime.pool.connection() as conn:
            await conn.execute('UPDATE companion_private.webhook_jobs SET account_id=NULL,epoch=NULL,owner_generation=NULL WHERE account_id=%s',(a,))
        assert (await q.claim())['account_id']==a
        await q.enqueue_verified(event('never-owned',2))
        async with runtime.pool.connection() as conn:
            await conn.execute("UPDATE companion_private.webhook_jobs SET expires_at=now()-interval '1 second' WHERE account_id IS NULL AND item_id=%s",('never-owned',))
        await q.enqueue_verified(event('another-unknown',3))
        async with runtime.pool.connection() as conn:
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.webhook_jobs WHERE item_id=%s',('never-owned',))).fetchone())[0]==0
    finally:await s.handle('close',{})


async def test_shared_volume_lock_survives_pg_session_loss(runtime,tmp_path):
    a=await account(runtime);s=PostgresPersistenceExtension(runtime,a,tmp_path)
    contender=PostgresPersistenceExtension(runtime,a,tmp_path)
    try:
        await s.handle('commit',{'document':document()})
        await s.conn.close()
        with pytest.raises(PostgresUnavailable):await contender.handle('load',{})
        await s.handle('close',{})
        assert (await contender.handle('load',{}))['state']['data_epoch']=='same-epoch'
    finally:await s.handle('close',{});await contender.handle('close',{})
