"""Disposable localhost PostgreSQL only; never reads DATABASE_URL or invokes providers."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import pytest
from pydantic import ValidationError

from companion.contracts import DomainError
from companion.health_incremental import (
    HealthIncrementalRequest, IncrementalSample, ROUTE, DAILY_BUNDLE, classify,
    daily_total, stable, stamp, stage_health_incremental, ingest_health_incremental,
)
from companion.health_incremental_migrations import TABLES, apply_migration
from companion.health_ingestion import HELPER, DATASET, RECIPE
from companion.pattern_migrations import MIGRATION_V3
from test_postgres import local_pg_cluster, pg_dsn

NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
DAY = NOW.replace(hour=0)-timedelta(days=1)
EPOCH = 'epoch-one'


def sample(kind='heartRate', **changes):
    result = dict(hk_uuid=str(uuid4()).upper(), hk_type=kind, start_at=stamp(DAY),
        end_at=stamp(DAY+timedelta(hours=1)), timezone='UTC', value=70.0, unit='bpm', category=None,
        workout_activity=None,duration_seconds=None,energy_kcal=None,distance_m=None,
        source_name='Test source',source_bundle='org.example.watch',source_revision='1',source_stream=None,
        synthetic=False,dataset_id=None,recipe=None,metadata_timezone=None,excluded_reason=None)
    if kind == 'stepCount':
        result.update(value=100.0,unit='count',source_stream=stable('watch'))
    if kind == 'sleepAnalysis':
        result.update(value=None,unit=None,category='asleepCore')
    if kind == 'workout':
        result.update(value=None,unit=None,workout_activity='walking',duration_seconds=3600.0)
    result.update(changes)
    return result


def request(samples=(), deleted=(), *, kind='heartRate', stream=None, sequence=0, more=False, epoch=EPOCH):
    return dict(schema_version='1',request_id=str(uuid4()),data_epoch=epoch,hk_type=kind,
        stream_id=stream or str(uuid4()),sequence=sequence,has_more=more,
        samples=list(samples),deleted_uuids=list(deleted))


class Harness:
    def __init__(self, dsn, role, accounts):
        self.dsn,self.role,self.accounts = dsn,role,accounts

    @asynccontextmanager
    async def transaction(self, who=0):
        async with await psycopg.AsyncConnection.connect(self.dsn+' user='+self.role, autocommit=True) as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('companion.account_id',%s,true)",(self.accounts[who],))
                yield conn

    async def send(self, body, who=0):
        async with self.transaction(who) as conn:
            return await stage_health_incremental(conn,self.accounts[who],body,now=NOW)

    async def rows(self, who=0):
        async with self.transaction(who) as conn:
            return await (await conn.execute('SELECT sample_id,value,deleted,revision,source_bundle FROM companion_private.health_samples ORDER BY sample_id')).fetchall()


@pytest.fixture
async def db(pg_dsn):
    role = 'health_delta_'+uuid4().hex
    accounts = [str(uuid4()),str(uuid4())]
    with psycopg.connect(pg_dsn,autocommit=True) as conn:
        conn.execute(MIGRATION_V3)
        conn.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA companion_private TO {}').format(sql.Identifier(role)))
        for table in ('accounts','snapshots','receipts','pattern_batches','health_samples','health_import_pages'):
            conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON companion_private.{} TO {}').format(sql.Identifier(table),sql.Identifier(role)))
        apply_migration(conn,role)
        # Prove repeatable additive migration without changing shared schema version.
        apply_migration(conn,role)
        for account in accounts:
            conn.execute('INSERT INTO companion_private.accounts(account_id,issuer,subject) VALUES(%s,%s,%s)',(account,'https://issuer.invalid',account))
            doc = {'state': {'data_epoch': EPOCH, 'revision': 1,
                'sources':[{'id':'healthkit','enabled':True,'consent':'granted','read_access':'unknown'}]}, 'meta':{}}
            conn.execute('INSERT INTO companion_private.snapshots(account_id,body) VALUES(%s,%s)',(account,Jsonb(doc)))
    return Harness(pg_dsn,role,accounts)


async def test_deletion_only_never_deletes_unchanged_and_maps_uuid(db):
    a,b = sample(),sample(value=80.0)
    first=request([a,b]); await db.send(first)
    deleted=request(deleted=[a['hk_uuid']],stream=first['stream_id'],sequence=1)
    receipt=await db.send(deleted)
    rows={row[0]: row for row in await db.rows()}
    assert rows[stable(a['source_bundle']+':'+a['hk_uuid'])][2]
    assert not rows[stable(b['source_bundle']+':'+b['hk_uuid'])][2]
    assert receipt['deleted']==1 and receipt['unknown_deletions']==0
    async with db.transaction() as conn:
        mapping=await (await conn.execute('SELECT batch_id,sample_id,deleted FROM companion_private.health_incremental_objects WHERE hk_uuid=%s',(a['hk_uuid'],))).fetchone()
        assert mapping[0] and mapping[1] and mapping[2]


async def test_replay_payload_conflict_and_immutable_uuid(db):
    a=sample(); first=request([a]); receipt=await db.send(first); before=await db.rows()
    assert await db.send(first)==receipt
    assert await db.rows()==before
    changed=deepcopy(first); changed['samples'][0]['value']=999.0
    with pytest.raises(DomainError) as error: await db.send(changed)
    assert error.value.code=='idempotency_conflict'
    changed.update(request_id=str(uuid4()),sequence=1)
    with pytest.raises(DomainError) as error: await db.send(changed)
    assert error.value.code=='idempotency_conflict'
    assert await db.rows()==before


async def test_interrupted_transaction_then_retry_and_lost_response(db):
    body=request([sample()])
    class InterruptBeforeReceipt:
        def __init__(self, connection): self.connection=connection
        @property
        def info(self): return self.connection.info
        async def execute(self, query, params=None):
            if query.startswith('INSERT INTO companion_private.receipts'):
                raise asyncio.CancelledError()
            return await self.connection.execute(query,params)
    with pytest.raises(asyncio.CancelledError):
        async with db.transaction() as conn:
            await stage_health_incremental(InterruptBeforeReceipt(conn),db.accounts[0],body,now=NOW)
    assert await db.rows()==[]
    async with db.transaction() as conn:
        for table in (*TABLES,'receipts'):
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.'+table)).fetchone())[0]==0
    # Commit succeeded but the client loses the response: exact retry returns its receipt.
    receipt=await db.send(body)
    assert await db.send(body)==receipt
    assert len(await db.rows())==1


async def test_account_rls_scope_and_epoch_fence_before_receipt(db):
    body=request([sample()]); await db.send(body)
    assert await db.rows(1)==[]
    assert (await db.send(body,1))['upserted']==1  # Same identities are account-local.
    async with db.transaction(1) as conn:
        with pytest.raises(DomainError) as error:
            await stage_health_incremental(conn,db.accounts[0],body,now=NOW)
        assert error.value.status==403
    async with db.transaction() as conn:
        await conn.execute("UPDATE companion_private.snapshots SET body=jsonb_set(body,'{state,data_epoch}',%s) WHERE account_id=%s",(Jsonb('epoch-two'),db.accounts[0]))
        for table in TABLES:
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.'+table)).fetchone())[0]==0
    with pytest.raises(DomainError) as error: await db.send(body)
    assert error.value.code=='epoch_mismatch'  # Even though the old durable receipt exists.
    assert (await db.send(body,1))['upserted']==1
    async with await psycopg.AsyncConnection.connect(db.dsn+' user='+db.role,autocommit=True) as conn:
        for table in TABLES:
            assert await (await conn.execute('SELECT * FROM companion_private.'+table)).fetchall()==[]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute('INSERT INTO companion_private.health_incremental_days(account_id,epoch,day) VALUES(%s,%s,%s)',(db.accounts[0],EPOCH,DAY.date()))


async def test_consent_rechecked_before_replay(db):
    body=request([sample()]); await db.send(body)
    async with db.transaction() as conn:
        await conn.execute("UPDATE companion_private.snapshots SET body=jsonb_set(body,'{state,sources,0,enabled}','false')")
    with pytest.raises(DomainError) as error: await db.send(body)
    assert error.value.code=='consent_required'


@pytest.mark.parametrize('same_bundle',[False,True])
async def test_steps_accumulate_streams_choose_max_and_delete_recomputes(db,same_bundle):
    a=sample('stepCount',value=100.0)
    b=sample('stepCount',value=200.0,start_at=stamp(DAY+timedelta(hours=1)),end_at=stamp(DAY+timedelta(hours=2)))
    phone=sample('stepCount',value=250.0,source_bundle=a['source_bundle'] if same_bundle else 'org.example.phone',source_stream=stable('phone'))
    first=request([a],kind='stepCount',more=True); response=await db.send(first)
    assert not response['bootstrapped'] and await db.rows()==[]
    second=request([b,phone],kind='stepCount',stream=first['stream_id'],sequence=1)
    await db.send(second)
    rows=await db.rows(); assert len(rows)==1 and rows[0][1]==300 and rows[0][4]==DAILY_BUNDLE
    third=request(deleted=[b['hk_uuid']],kind='stepCount',stream=first['stream_id'],sequence=2)
    await db.send(third)
    assert (await db.rows())[0][1]==250  # Not 100; surviving phone wins.
    fourth=request(deleted=[phone['hk_uuid']],kind='stepCount',stream=first['stream_id'],sequence=3)
    await db.send(fourth)
    assert (await db.rows())[0][1]==100
    await db.send(request(deleted=[a['hk_uuid']],kind='stepCount',stream=first['stream_id'],sequence=4))
    assert (await db.rows())[0][2]


async def test_steps_cross_midnight_point_and_unaffected_day(db):
    crossing=sample('stepCount',value=120.0,start_at=stamp(DAY-timedelta(hours=1)),end_at=stamp(DAY+timedelta(hours=1)))
    other=sample('stepCount',value=80.0,start_at=stamp(DAY-timedelta(days=2)),end_at=stamp(DAY-timedelta(days=2)+timedelta(hours=1)))
    point=sample('stepCount',value=10.0,start_at=stamp(DAY),end_at=stamp(DAY))
    first=request([crossing,other,point],kind='stepCount'); await db.send(first)
    async with db.transaction() as conn:
        rows=await (await conn.execute('SELECT start_at,value FROM companion_private.health_samples WHERE NOT deleted ORDER BY start_at')).fetchall()
        assert [r[1] for r in rows]==[80,60,70]
    unchanged=next(r for r in await db.rows() if r[1]==80)
    await db.send(request(deleted=[crossing['hk_uuid']],kind='stepCount',stream=first['stream_id'],sequence=1))
    assert unchanged in await db.rows()
    assert any(r[1]==10 and not r[2] for r in await db.rows())


@pytest.mark.parametrize('kind',['heartRate','stepCount'])
async def test_same_page_addition_then_deletion_is_delete_wins(db,kind):
    a=sample(kind)
    response=await db.send(request([a],[a['hk_uuid']],kind=kind))
    assert response['upserted']==1 and response['deleted']==1
    assert not any(not row[2] for row in await db.rows())


async def test_future_clock_skew_step_portion_is_not_counted_yet(db):
    a=sample('stepCount',value=65.0,start_at=stamp(NOW-timedelta(hours=1)),end_at=stamp(NOW+timedelta(minutes=5)))
    await db.send(request([a],kind='stepCount'))
    assert (await db.rows())[0][1]==pytest.approx(60.0)
    async with db.transaction() as conn:
        assert (await (await conn.execute('SELECT end_at FROM companion_private.health_samples')).fetchone())[0]==NOW


async def test_tombstone_before_addition_prevents_resurrection(db):
    a=sample(); first=request(deleted=[a['hk_uuid']]); response=await db.send(first)
    assert response['unknown_deletions']==1
    response=await db.send(request([a],stream=first['stream_id'],sequence=1))
    assert response['superseded']==1 and await db.rows()==[]


async def test_sequence_and_stream_conflicts(db):
    body=request([sample()]); await db.send(body)
    for bad in [request([sample()],sequence=2,stream=body['stream_id']),request([sample()],sequence=1)]:
        with pytest.raises(DomainError) as error: await db.send(bad)
        assert error.value.status==409
    assert len(await db.rows())==1


async def test_exact_helper_provenance_and_probe_exclusion(db):
    helper=sample(source_bundle=HELPER,synthetic=True,dataset_id=DATASET,recipe=RECIPE,metadata_timezone='UTC')
    probe=sample(source_bundle=HELPER,synthetic=True,dataset_id='probe',recipe='bounded-probe-v1',metadata_timezone='UTC')
    mislabeled=sample(source_bundle=HELPER,synthetic=False)
    unknown=sample(synthetic=True)
    first=request([helper,probe,mislabeled,unknown]); response=await db.send(first)
    assert response['upserted']==1 and response['excluded']==3
    async with db.transaction() as conn:
        assert (await (await conn.execute('SELECT provenance FROM companion_private.pattern_batches')).fetchone())[0]=='synthetic'
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_incremental_objects WHERE excluded AND raw IS NULL')).fetchone())[0]==3
    assert len(await db.rows())==1
    await db.send(request(deleted=[probe['hk_uuid']],stream=first['stream_id'],sequence=1))
    assert not (await db.rows())[0][2]


async def test_unsupported_workout_is_explicitly_acknowledged_not_fabricated(db):
    supported=sample('workout')
    unsupported=sample('workout',workout_activity=None,excluded_reason='unsupported_workout_activity')
    first=request([supported,unsupported],kind='workout')
    receipt=await db.send(first)
    assert receipt['upserted']==1 and receipt['excluded']==1 and len(await db.rows())==1
    async with db.transaction() as conn:
        assert (await (await conn.execute('SELECT excluded,raw FROM companion_private.health_incremental_objects WHERE hk_uuid=%s',(unsupported['hk_uuid'],))).fetchone())==(True,None)
    await db.send(request(deleted=[unsupported['hk_uuid']],kind='workout',stream=first['stream_id'],sequence=1))
    assert not (await db.rows())[0][2]
    with pytest.raises(ValidationError):
        IncrementalSample.model_validate_json(json.dumps(sample(excluded_reason='unsupported_workout_activity')))


async def test_synthetic_steps_remain_distinct_from_real_streams(db):
    helper=sample('stepCount',source_bundle=HELPER,source_stream=None,synthetic=True,dataset_id=DATASET,
        recipe=RECIPE,metadata_timezone='UTC',value=9000.0,end_at=stamp(DAY+timedelta(days=1)))
    real=sample('stepCount',value=200.0)
    first=request([helper,real],kind='stepCount'); await db.send(first)
    assert sorted(r[1] for r in await db.rows())==[200,9000]
    await db.send(request(deleted=[helper['hk_uuid']],kind='stepCount',stream=first['stream_id'],sequence=1))
    assert [r[1] for r in await db.rows() if not r[2]]==[200]


async def test_terminal_empty_page_finishes_bootstrap_without_erasure(db):
    first=request([sample('stepCount')],kind='stepCount',more=True); await db.send(first)
    assert await db.rows()==[]
    response=await db.send(request(kind='stepCount',stream=first['stream_id'],sequence=1))
    assert response['bootstrapped'] and response['upserted']==0
    before=await db.rows()
    await db.send(request(kind='stepCount',stream=first['stream_id'],sequence=2))
    assert await db.rows()==before


async def test_handler_uses_existing_transaction_and_does_not_invoke_provider(db):
    service=SimpleNamespace(lock=asyncio.Lock(),clock=lambda:NOW,
        pattern_coordinator=SimpleNamespace(transaction=db.transaction,store=SimpleNamespace(account_id=db.accounts[0])))
    response=await ingest_health_incremental(service,request([sample()]))
    assert response['upserted']==1 and len(await db.rows())==1


async def test_migration_force_rls_and_no_public_grants(db):
    with psycopg.connect(db.dsn,autocommit=True) as conn:
        for table in TABLES:
            assert conn.execute('SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=%s::regclass',('companion_private.'+table,)).fetchone()==(True,True)
            for role in ('anon','authenticated'):
                assert not conn.execute("SELECT has_table_privilege(%s,%s,'SELECT,INSERT,UPDATE,DELETE')",(role,'companion_private.'+table)).fetchone()[0]
        assert conn.execute("SELECT prosecdef FROM pg_proc WHERE oid='companion_private.clear_health_incremental_epoch()'::regprocedure").fetchone()==(False,)
        assert conn.execute('SELECT version FROM companion_private.schema_version').fetchall()==[(3,)]
        assert not conn.execute("SELECT has_table_privilege(%s,'companion_private.health_incremental_acknowledgments','UPDATE')",(db.role,)).fetchone()[0]
        with pytest.raises(ValueError): apply_migration(conn,'postgres')


async def test_incremental_stream_blocks_later_snapshot_replacement(db):
    from companion.health_ingestion import stage_health
    from companion.health_incremental import normalized
    await db.send(request([sample()]))
    before=await db.rows()
    legacy={'schema_version':'1','request_id':str(uuid4()),'data_epoch':EPOCH,'collection_id':str(uuid4()),
        'page_index':0,'page_count':1,'window_start':stamp(DAY),'window_end':stamp(NOW),
        'samples':[normalized(IncrementalSample.model_validate_json(json.dumps(sample()))).model_dump(mode='json')]}
    with pytest.raises(psycopg.errors.CheckViolation):
        async with db.transaction() as conn:
            await stage_health(conn,db.accounts[0],legacy)
    assert await db.rows()==before


async def test_pending_snapshot_blocks_delta_start_without_partial_writes(db):
    async with db.transaction() as conn:
        await conn.execute('INSERT INTO companion_private.health_import_pages(account_id,collection_id,page_index,digest,body) VALUES(%s,%s,0,%s,%s)',
            (db.accounts[0],str(uuid4()),'digest',Jsonb({'data_epoch':EPOCH})))
    with pytest.raises(DomainError) as error: await db.send(request([sample()]))
    assert 'pending complete-window' in error.value.message and await db.rows()==[]


async def test_handler_uses_real_existing_account_persistence_session(db):
    from companion.postgres import PostgresConfig,PostgresRuntime
    from companion.extensions.postgres import PostgresPersistenceExtension
    from companion.pattern_store import PatternCoordinator
    runtime=PostgresRuntime(PostgresConfig(db.dsn+' user='+db.role,local_test=True))
    store=PostgresPersistenceExtension(runtime,db.accounts[0])
    service=SimpleNamespace(lock=asyncio.Lock(),clock=lambda:NOW)
    def forbidden_provider(): raise AssertionError('Provider must never be invoked')
    service.pattern_coordinator=PatternCoordinator(service,store,model_factory=forbidden_provider)
    try:
        result=await ingest_health_incremental(service,request([sample()]))
        assert result['upserted']==1
        assert (await store.handle('load',{}))['state']['data_epoch']==EPOCH
        assert len(await db.rows())==1
    finally:
        await store.handle('close',{}); await runtime.close()


async def test_concurrent_duplicate_delivery_serializes_to_one_receipt(db):
    body=request([sample()])
    replies=await asyncio.gather(db.send(body),db.send(body),db.send(body))
    assert replies[0]==replies[1]==replies[2]
    rows=await db.rows(); assert len(rows)==1 and rows[0][3]==1
    async with db.transaction() as conn:
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.receipts')).fetchone())[0]==1


async def test_outer_commit_interruption_does_not_leave_receipt(db):
    body=request([sample()])
    with pytest.raises(RuntimeError):
        async with db.transaction() as conn:
            response=await stage_health_incremental(conn,db.accounts[0],body,now=NOW)
            assert response['upserted']==1
            raise RuntimeError('Simulated caller interruption before durable commit')
    assert await db.rows()==[]
    assert (await db.send(body))['upserted']==1


@pytest.mark.parametrize('kind',['heartRate','stepCount'])
async def test_legacy_uuid_deletion_recovers_exact_source_identity(db,kind):
    from companion.health_incremental import normalized,upsert_normalized
    helper = dict(source_bundle=HELPER,synthetic=True,dataset_id=DATASET,recipe=RECIPE,metadata_timezone='UTC',source_stream=None) if kind=='stepCount' else {}
    a,b=sample(kind,**helper),sample(kind,value=80.0,**helper)
    async with db.transaction() as conn:
        for item in (a,b):
            await upsert_normalized(conn,db.accounts[0],normalized(IncrementalSample.model_validate_json(json.dumps(item))))
    receipt=await db.send(request(deleted=[a['hk_uuid']],kind=kind))
    assert receipt['unknown_deletions']==0
    rows=await db.rows()
    assert len([row for row in rows if row[2]])==1
    assert next(row for row in rows if row[1]==80)[2] is False
    async with db.transaction() as conn:
        assert (await (await conn.execute('SELECT sample_id FROM companion_private.health_incremental_objects WHERE hk_uuid=%s',(a['hk_uuid'],))).fetchone())[0]==stable(a['source_bundle']+':'+a['hk_uuid'])


async def test_legacy_step_totals_block_unsafe_partial_baseline(db):
    from companion.health_incremental import normalized,upsert_normalized
    old=sample('stepCount',source_bundle=DAILY_BUNDLE,value=9000.0)
    async with db.transaction() as conn:
        await upsert_normalized(conn,db.accounts[0],normalized(IncrementalSample.model_validate_json(json.dumps(old))))
    before=await db.rows()
    with pytest.raises(DomainError) as error:
        await db.send(request([sample('stepCount',value=1.0)],kind='stepCount'))
    assert 'baseline migration' in error.value.message
    assert await db.rows()==before
    async with db.transaction() as conn:
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_incremental_objects')).fetchone())[0]==0


async def test_object_bound_rolls_back_without_dropping_data(db,monkeypatch):
    monkeypatch.setattr('companion.health_incremental.MAX_OBJECTS',2)
    first=request([sample(),sample()]); await db.send(first)
    before=await db.rows()
    with pytest.raises(DomainError):
        await db.send(request([sample()],stream=first['stream_id'],sequence=1))
    assert await db.rows()==before
    async with db.transaction() as conn:
        assert (await (await conn.execute('SELECT next_sequence FROM companion_private.health_incremental_streams')).fetchone())[0]==1


@pytest.mark.parametrize('kind',['heartRate','stepCount'])
async def test_retention_prunes_raw_and_projection_without_resetting_sequence(db,kind):
    past=NOW-timedelta(days=200)
    a=sample(kind,start_at=stamp(past-timedelta(days=1)),end_at=stamp(past-timedelta(hours=23)))
    first=request([a],kind=kind)
    async with db.transaction() as conn:
        old_receipt=await stage_health_incremental(conn,db.accounts[0],first,now=past)
    assert len(await db.rows())==1
    await db.send(request([sample(kind)],kind=kind,stream=first['stream_id'],sequence=1))
    assert len(await db.rows())==1
    async with db.transaction() as conn:
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_incremental_objects')).fetchone())[0]==1
        assert (await (await conn.execute('SELECT next_sequence FROM companion_private.health_incremental_streams')).fetchone())[0]==2
    assert await db.send(first)==old_receipt  # Replay never reconstructs expired private rows.
    assert len(await db.rows())==1


async def test_mixed_expired_current_and_deletions_advance_atomically(db):
    a,b=sample(),sample(value=80.0)
    first=request([a,b]); await db.send(first)
    expired=sample(start_at=stamp(DAY-timedelta(days=250)),end_at=stamp(DAY-timedelta(days=250)+timedelta(hours=1)))
    current=sample(value=90.0)
    body=request([expired,current],[a['hk_uuid']],stream=first['stream_id'],sequence=1)
    receipt=await db.send(body)
    assert (receipt['expired'],receipt['upserted'],receipt['deleted'])==(1,1,1)
    rows=await db.rows(); assert sorted(row[1] for row in rows if not row[2])==[80,90]
    async with db.transaction() as conn:
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_incremental_objects WHERE hk_uuid=%s',(expired['hk_uuid'],))).fetchone())[0]==0
        assert (await (await conn.execute('SELECT next_sequence FROM companion_private.health_incremental_streams')).fetchone())[0]==2
    assert await db.send(body)==receipt and await db.rows()==rows


async def test_interval_overlapping_retention_is_not_misclassified_as_expired(db):
    cutoff=(NOW-timedelta(days=190)).replace(hour=0,minute=0,second=0,microsecond=0)
    overlapping=sample('sleepAnalysis',start_at=stamp(cutoff-timedelta(days=2)),end_at=stamp(cutoff+timedelta(hours=1)))
    receipt=await db.send(request([overlapping],kind='sleepAnalysis'))
    assert receipt['expired']==0 and receipt['upserted']==1 and len(await db.rows())==1


async def test_expired_full_page_then_current_and_terminal_publishes_baseline(db):
    expired=[sample('stepCount',start_at=stamp(DAY-timedelta(days=250)),end_at=stamp(DAY-timedelta(days=250)+timedelta(hours=1))) for _ in range(100)]
    first=request(expired,kind='stepCount',more=True)
    receipt=await db.send(first)
    assert receipt['expired']==100 and not receipt['bootstrapped'] and await db.rows()==[]
    async with db.transaction() as conn:
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_incremental_objects')).fetchone())[0]==0
    second=request([sample('stepCount',value=321.0)],kind='stepCount',stream=first['stream_id'],sequence=1,more=True)
    await db.send(second); assert await db.rows()==[]
    terminal=await db.send(request(kind='stepCount',stream=first['stream_id'],sequence=2))
    assert terminal['bootstrapped'] and [r[1] for r in await db.rows()]==[321]
    assert await db.send(first)==receipt


async def test_delayed_bootstrap_acknowledges_expired_additions_without_reconstruction(db):
    past=NOW-timedelta(days=200)
    old=sample('stepCount',start_at=stamp(past-timedelta(days=1)),end_at=stamp(past-timedelta(hours=23)))
    first=request([old],kind='stepCount',more=True)
    async with db.transaction() as conn:
        first_receipt=await stage_health_incremental(conn,db.accounts[0],first,now=past)
    assert await db.rows()==[]
    delayed=sample('stepCount',start_at=stamp(past),end_at=stamp(past+timedelta(hours=1)))
    second=request([old,delayed,sample('stepCount',value=456.0)],kind='stepCount',stream=first['stream_id'],sequence=1)
    receipt=await db.send(second)
    assert receipt['expired']==2 and receipt['upserted']==1 and receipt['bootstrapped']
    assert [r[1] for r in await db.rows()]==[456]
    async with db.transaction() as conn:
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_incremental_objects')).fetchone())[0]==1
        await conn.execute('DELETE FROM companion_private.receipts')
    assert await db.send(first)==first_receipt
    assert [r[1] for r in await db.rows()]==[456]


async def test_expired_addition_does_not_mask_invalid_future_sample(db):
    expired=sample(start_at=stamp(DAY-timedelta(days=250)),end_at=stamp(DAY-timedelta(days=250)+timedelta(hours=1)))
    future=sample(start_at=stamp(NOW-timedelta(minutes=5)),end_at=stamp(NOW+timedelta(minutes=6)))
    with pytest.raises(DomainError) as error: await db.send(request([expired,sample(),future]))
    assert error.value.code=='invalid_request' and 'future' in error.value.message
    assert await db.rows()==[]
    async with db.transaction() as conn:
        for table in TABLES:
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.'+table)).fetchone())[0]==0


async def test_minimal_ack_survives_generic_and_raw_retention_and_binds_payload(db):
    from companion.health_incremental import prune_health_incremental_history
    past=NOW-timedelta(days=200)
    old=sample(start_at=stamp(past-timedelta(days=1)),end_at=stamp(past-timedelta(hours=23)))
    body=request([old])
    async with db.transaction() as conn:
        receipt=await stage_health_incremental(conn,db.accounts[0],body,now=past)
    async with db.transaction() as conn:
        await prune_health_incremental_history(conn,db.accounts[0],EPOCH,now=NOW)
        await conn.execute('DELETE FROM companion_private.receipts')
        ack=await (await conn.execute('SELECT digest,response FROM companion_private.health_incremental_acknowledgments')).fetchone()
        assert len(ack[0])==64 and ack[1]==receipt
        assert not {'samples','deleted_uuids','source_name','start_at'} & set(ack[1])
    assert await db.rows()==[]
    assert await db.send(body)==receipt and await db.rows()==[]
    changed=deepcopy(body); changed['samples'][0]['value']=999.0
    with pytest.raises(DomainError) as error: await db.send(changed)
    assert error.value.code=='idempotency_conflict'
    new_identity={**body,'request_id':str(uuid4())}
    with pytest.raises(DomainError): await db.send(new_identity)
    async with db.transaction() as conn:
        assert (await (await conn.execute('SELECT next_sequence FROM companion_private.health_incremental_streams')).fetchone())[0]==1
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_incremental_acknowledgments')).fetchone())[0]==1


async def test_ack_is_erased_on_epoch_change_and_account_deletion_not_ordinary_update(db):
    body=request([sample()]); await db.send(body); await db.send(body,1)
    async with db.transaction() as conn:
        await conn.execute("UPDATE companion_private.snapshots SET body=jsonb_set(body,'{state,revision}','2')")
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_incremental_acknowledgments')).fetchone())[0]==1
        await conn.execute("UPDATE companion_private.snapshots SET body=jsonb_set(body,'{state,data_epoch}',%s)",(Jsonb('epoch-two'),))
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_incremental_acknowledgments')).fetchone())[0]==0
    with pytest.raises(DomainError) as error: await db.send(body)
    assert error.value.code=='epoch_mismatch'
    async with db.transaction(1) as conn:
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_incremental_acknowledgments')).fetchone())[0]==1
        await conn.execute('DELETE FROM companion_private.pattern_batches WHERE account_id=%s',(db.accounts[1],))
        await conn.execute('DELETE FROM companion_private.receipts WHERE account_id=%s',(db.accounts[1],))
        await conn.execute('DELETE FROM companion_private.snapshots WHERE account_id=%s',(db.accounts[1],))
        await conn.execute('DELETE FROM companion_private.accounts WHERE account_id=%s',(db.accounts[1],))
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_incremental_acknowledgments')).fetchone())[0]==0


async def test_upgrade_backfills_minimal_current_epoch_ack_before_generic_pruning(db):
    body=request([sample()]); original=await db.send(body)
    async with db.transaction() as conn:
        await conn.execute('DELETE FROM companion_private.health_incremental_acknowledgments')
        await conn.execute("UPDATE companion_private.receipts SET response=response-'expired'")
    with psycopg.connect(db.dsn,autocommit=True) as conn:
        apply_migration(conn,db.role)
    async with db.transaction() as conn:
        await conn.execute('DELETE FROM companion_private.receipts')
    assert await db.send(body)==original
    assert len(await db.rows())==1


async def test_upgrade_refuses_unexpected_private_fields_in_legacy_receipt(db):
    await db.send(request([sample()]))
    async with db.transaction() as conn:
        await conn.execute('DELETE FROM companion_private.health_incremental_acknowledgments')
        await conn.execute("UPDATE companion_private.receipts SET response=response || %s",(Jsonb({'samples':['must-not-be-copied']}),))
    with psycopg.connect(db.dsn,autocommit=True) as conn:
        with pytest.raises(ValueError): apply_migration(conn,db.role)
    async with db.transaction() as conn:
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_incremental_acknowledgments')).fetchone())[0]==0


async def test_empty_terminal_page_allowed_after_maximum_initial_pages(db):
    stream=str(uuid4())
    async with db.transaction() as conn:
        await conn.execute('INSERT INTO companion_private.health_incremental_streams(account_id,epoch,hk_type,stream_id,next_sequence,bootstrapped) VALUES(%s,%s,%s,%s,500,false)',
            (db.accounts[0],EPOCH,'stepCount',stream))
    response=await db.send(request(kind='stepCount',stream=stream,sequence=500))
    assert response['bootstrapped']


async def test_malformed_raw_payload_returns_sanitized_contract_error(db):
    for body in [request([sample(timezone='not/a/timezone')]),request([sample(value=float('nan'))]),{**request(),'extra':'private-value'}]:
        with pytest.raises(DomainError) as error: await db.send(body)
        assert error.value.code=='invalid_request' and error.value.status==422
        assert 'private-value' not in error.value.message
    assert await db.rows()==[]


async def test_autocommit_call_is_rejected(db):
    async with await psycopg.AsyncConnection.connect(db.dsn+' user='+db.role,autocommit=True) as conn:
        with pytest.raises(RuntimeError,match='explicit transaction'):
            await stage_health_incremental(conn,db.accounts[0],request([sample()]),now=NOW)
    assert await db.rows()==[]


async def test_empty_initial_query_does_not_claim_read_permission(db):
    response=await db.send(request())
    assert response['upserted']==0 and await db.rows()==[]
    async with db.transaction() as conn:
        assert (await (await conn.execute("SELECT body->'state'->'sources'->0->>'read_access' FROM companion_private.snapshots")).fetchone())[0]=='unknown'


def test_isolated_schema_matches_strict_model_shape_and_bounds():
    from jsonschema import Draft202012Validator,FormatChecker
    path=Path(__file__).resolve().parents[2]/'contracts/health_incremental.schema.json'
    schema=json.loads(path.read_text())
    validator=Draft202012Validator({'$ref':'#/$defs/HealthIncrementalRequest','$defs':schema['$defs']},format_checker=FormatChecker())
    good=request([sample()]); validator.validate(good)
    for bad in [{**good,'account_id':str(uuid4())},request(deleted=['bad']),
                request([sample() for _ in range(51)],[str(uuid4()) for _ in range(50)]),
                request([sample()],kind='workout'),request(more=True)]:
        assert not validator.is_valid(bad)
    models=HealthIncrementalRequest.model_json_schema()
    assert schema['$defs']['IncrementalSample']==models['$defs']['IncrementalSample']
    assert schema['$defs']['HealthIncrementalRequest']['properties']==models['properties']
    assert schema['$defs']['HealthIncrementalRequest']['additionalProperties'] is False
    from companion.health_incremental import HealthIncrementalReceipt
    assert schema['$defs']['HealthIncrementalReceipt']==HealthIncrementalReceipt.model_json_schema()


@pytest.mark.parametrize('change',[
    {'synthetic':1}, {'value':float('inf')}, {'value':-1.0}, {'source_stream':'raw-device-id'},
    {'hk_uuid':'not-a-uuid'}, {'start_at':'2026-11-13T00:00:00'}, {'timezone':'not/a/timezone'},
    {'source_bundle':'bad/source'}, {'extra':'forbidden'},
])
def test_strict_sample_validation(change):
    with pytest.raises((ValidationError,ValueError,KeyError)):
        IncrementalSample.model_validate_json(json.dumps(sample(**change)))


def test_page_limits_and_contradictions():
    a=sample()
    for body in [request([a,a]),request(deleted=[a['hk_uuid'],a['hk_uuid']]),request(deleted=['broken']),
                 request([sample() for _ in range(101)]),request(more=True),
                 {**request(),'account_id':str(uuid4())}, request([a],kind='workout')]:
        with pytest.raises((ValidationError,ValueError)):
            HealthIncrementalRequest.model_validate_json(json.dumps(body))

async def test_unknown_device_step_stream_is_explicitly_excluded_not_summed(db):
    value=sample('stepCount',source_stream=None,excluded_reason='unsupported_step_source')
    result=await db.send(request([value],kind='stepCount'))
    assert result['excluded']==1 and result['upserted']==0
    assert await db.rows()==[]
