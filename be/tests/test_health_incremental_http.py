"""Actual authenticated route, isolated restricted PostgreSQL; no Health/provider I/O."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4
import psycopg,pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from companion.auth_http import create_authenticated_app
from companion.bootstrap import UnavailableSynthesis
from companion.pattern_migrations import MIGRATION_V3
from companion.postgres import PostgresConfig,PostgresRuntime
from companion.health_incremental_migrations import apply_migration,TABLES
from companion.health_incremental_service import HealthIncrementalStatus
from test_postgres import local_pg_cluster,pg_dsn
from test_postgres_http import signing_key
from test_auth_http import Harness,NOW
from test_pattern_bridges import consent,health_pages,send
from test_health_incremental import sample,request

@pytest.fixture
async def app(tmp_path,pg_dsn,signing_key):
    role='health_http_'+uuid4().hex
    with psycopg.connect(pg_dsn,autocommit=True) as c:
        c.execute(MIGRATION_V3)
        c.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
        c.execute(sql.SQL('GRANT USAGE ON SCHEMA companion_private TO {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA companion_private TO {}').format(sql.Identifier(role)))
        apply_migration(c,role);apply_migration(c,role)
    runtime=PostgresRuntime(PostgresConfig(make_conninfo(pg_dsn,user=role),local_test=True))
    h=Harness(tmp_path/'private',signing_key)
    h.app=create_authenticated_app(tmp_path/'private',verifier=h.verifier,clock=lambda:NOW,housekeeping_interval=3600,
        postgres_runtime=runtime,experience_profile=True,health_incremental_enabled=True,health_retention_worker_enabled=False,
        replacements_factory=lambda path:{'synthesis':UnavailableSynthesis(path/'checkpoints.sqlite')})
    return h,runtime

async def status(client,h,who='a'):
    r=await client.get('/health/v1/incremental/status',headers=h.headers(who))
    assert r.status_code==200,r.text
    HealthIncrementalStatus.model_validate(r.json());return r.json()

async def post(client,h,body,who='a'):
    return await client.post('/health/v1/incremental',headers=h.headers(who),json=body)

async def test_authenticated_receipt_route_retention_replay_conflict_and_reset(app):
    h,runtime=app
    async with h.client() as client:
        assert (await client.get('/health/v1/incremental/status')).status_code==401
        assert (await client.post('/health/v1/incremental',json={})).status_code==401
        state=await consent(client,h);s=await status(client,h)
        assert s['enabled'] and s['schema_ready'] and s['baseline_supported'] and not s['snapshot_allowed']
        body=request([sample()],epoch=state['data_epoch'])
        first=await post(client,h,body);assert first.status_code==200,first.text
        assert first.json()['expired']==0 and 'state' not in first.json()
        assert (await post(client,h,body)).json()==first.json()
        assert (await post(client,h,{**body,'has_more':True})).status_code==409
        assert (await post(client,h,{**body,'account_id':str(uuid4())})).status_code==422
        await consent(client,h,'b')
        assert all(v['stream_id'] is None for v in (await status(client,h,'b'))['streams'])
        async with runtime.pool.connection() as c:
            assert (await (await c.execute('SELECT count(*) FROM companion_private.health_incremental_objects')).fetchone())[0]==0
        handle=await h.handle('a');service=handle.service.app.state.service
        async with service.lock:await service.commit(deepcopy(service.document),erase=True)
        await service.housekeep()
        assert (await post(client,h,body)).json()==first.json()
        result=await client.request('DELETE','/data',headers=h.headers(),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':body['data_epoch'],'confirm':'delete_all'})
        assert result.status_code==200,result.text
        assert (await post(client,h,body)).status_code==409
        async with service.pattern_coordinator.transaction() as c:
            for table in TABLES:assert (await (await c.execute(sql.SQL('SELECT count(*) FROM companion_private.{}').format(sql.Identifier(table)))).fetchone())[0]==0

async def test_expiry_disabled_collection_retains_ack_and_raw_data_pruned(app):
    h,runtime=app
    async with h.client() as client:
        state=await consent(client,h);body=request([sample()],epoch=state['data_epoch'])
        first=await post(client,h,body);assert first.status_code==200,first.text
        handle=await h.handle('a');service=handle.service.app.state.service
        prefs=deepcopy(state['preferences']);prefs['enabled_source_ids'].remove('healthkit')
        result=await client.put('/preferences',headers=h.headers(),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'preferences':prefs})
        assert result.status_code==200
        assert (await post(client,h,body)).status_code==409
        maintenance=h.app.state.incremental_health;maintenance.enabled=False;maintenance.clock=lambda:NOW+timedelta(days=191)
        await maintenance.retain(handle.account_id)
        async with service.pattern_coordinator.transaction() as c:
            assert (await (await c.execute('SELECT count(*) FROM companion_private.health_incremental_objects WHERE raw IS NOT NULL')).fetchone())[0]==0
            assert (await (await c.execute('SELECT count(*) FROM companion_private.health_incremental_acknowledgments')).fetchone())[0]==1
        assert not (await status(client,h))['snapshot_allowed']

async def test_snapshot_cutover_preserves_helper_records_and_rejects_second_stream(app):
    h,runtime=app
    async with h.client() as client:
        state=await consent(client,h)
        for page in health_pages(state['data_epoch']):assert (await send(client,h,page)).status_code==200
        handle=await h.handle('a');service=handle.service.app.state.service
        async with service.pattern_coordinator.transaction() as c:
            before=await (await c.execute('SELECT batch_id,sample_id,deleted FROM companion_private.health_samples ORDER BY batch_id,sample_id')).fetchall()
        assert (await status(client,h))['baseline_supported']
        body=request([sample()],epoch=state['data_epoch']);r=await post(client,h,body);assert r.status_code==200,r.text
        async with service.pattern_coordinator.transaction() as c:
            after=await (await c.execute('SELECT batch_id,sample_id,deleted FROM companion_private.health_samples ORDER BY batch_id,sample_id')).fetchall()
        assert set(before).issubset(set(after))
        assert (await post(client,h,request([sample()],epoch=state['data_epoch']))).status_code==409
        # Complete-window imports can no longer erase UUID deltas after cutover.
        blocked=await send(client,h,health_pages(state['data_epoch'])[0])
        assert blocked.status_code in {409,503}

async def test_disabled_or_missing_schema_has_no_implicit_activation(app):
    h,runtime=app
    async with h.client() as client:
        state=await consent(client,h);h.app.state.incremental_health.enabled=False
        view=await status(client,h);assert not view['enabled'] and view['schema_ready']
        assert (await post(client,h,request([sample()],epoch=state['data_epoch']))).status_code==409

async def test_legacy_real_steps_report_unsupported_without_reset_or_replacement(app):
    h,runtime=app
    async with h.client() as client:
        state=await consent(client,h)
        pages=health_pages(state['data_epoch'])
        for page in pages:
            for row in page['samples']:
                if row['hk_type']=='stepCount':
                    row.update(provenance='realHealthKit',source_bundle='org.example.watch',dataset_id=None,recipe=None)
            response=await send(client,h,page);assert response.status_code==200,response.text
        view=await status(client,h)
        assert not view['baseline_supported'] and 'UUID baseline' in view['message']
        body=request([sample('stepCount')],kind='stepCount',epoch=state['data_epoch'])
        rejected=await post(client,h,body);assert rejected.status_code==409,rejected.text
        assert (await client.get('/state',headers=h.headers())).json()['data_epoch']==state['data_epoch']
        service=(await h.handle('a')).service.app.state.service
        async with service.pattern_coordinator.transaction() as c:
            assert (await (await c.execute('SELECT count(*) FROM companion_private.health_incremental_streams')).fetchone())[0]==0
            assert (await (await c.execute('SELECT count(*) FROM companion_private.health_samples WHERE NOT deleted')).fetchone())[0]>0
