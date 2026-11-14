"""Disposable PostgreSQL and fake model only; no configured service or provider."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest

from companion.patterns import Aggregate, BriefRequest, ClarifyRequest, area_results
from companion.pattern_store import PatternCoordinator
from companion.extensions.postgres import PostgresPersistenceExtension
from test_patterns import configured, NOW
from test_postgres import local_pg_cluster, pg_dsn, document


async def saved(c, run):
    async with c.transaction() as conn:
        return await (await conn.execute('SELECT evidence,assessment,brief_override,status,engine FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s',(c.store.account_id,run))).fetchone()


async def test_improved_missing_and_recurrent_results_are_unique_immutable_and_cached(configured):
    _,_,_,_,c,model=configured
    first=await c.analyze(str(uuid4()))
    snapshot=await saved(c,first['run_id'])
    assert len(first['areas'])==len({a['area'] for a in first['areas']})==3
    assert first['is_current'] and len(first['actionable_area_ids'])==3
    async with c.transaction() as conn:
        # Fully recorded improved steps/sleep; food-service frequency disappears.
        await conn.execute("UPDATE companion_private.health_samples SET value=9000 WHERE hk_type='stepCount'")
        await conn.execute("UPDATE companion_private.pattern_transactions SET category='other'")
        await conn.execute("UPDATE companion_private.health_samples SET deleted=true WHERE hk_type='sleepAnalysis'")
    improved=await c.analyze(str(uuid4()))
    states={a['area']:a['state'] for a in improved['areas']}
    assert states=={'activity':'no_current_concern','sleep':'insufficient_data','food_service':'no_current_concern'}
    assert improved['findings']==[] and improved['actionable_area_ids']==[] and improved['actionability_key'] is None
    assert improved['run_id']!=first['run_id'] and (await c.get())['run_id']==improved['run_id']
    assert await saved(c,first['run_id'])==snapshot
    async with c.transaction() as conn:
        old=await (await conn.execute('SELECT run_id,status,engine,analyzed_at,evidence,is_demo FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s',(c.store.account_id,first['run_id']))).fetchone()
        oldview=await c._view(conn,old)
    assert not oldview['is_current'] and oldview['actionable_area_ids']==[] and oldview['current_run_id']==improved['run_id']
    assert not (await c.notification_candidate(first['run_id'], first['actionability_key']))['eligible']
    # Restore original aggregate using the same deterministic input generator.
    from companion.pattern_operator import generate
    _,samples,transactions=generate(NOW)
    async with c.transaction() as conn:
        for sample in samples:
            await conn.execute('UPDATE companion_private.health_samples SET value=%s,deleted=false WHERE account_id=%s AND sample_id=%s',(sample['value'],c.store.account_id,sample['sample_id']))
        for tx in transactions:
            await conn.execute('UPDATE companion_private.pattern_transactions SET category=%s WHERE account_id=%s AND transaction_id=%s',(tx['category'],c.store.account_id,tx['transaction_id']))
    recurrent=await c.analyze(str(uuid4()))
    assert recurrent['run_id'] not in {first['run_id'],improved['run_id']}
    assert recurrent['areas']==first['areas'] and recurrent['is_current']
    assert len(model.calls)==1  # Both no-concern and cached recurrence cost zero calls.
    assert await saved(c,first['run_id'])==snapshot
    assert (await c.notification_candidate(recurrent['run_id'],recurrent['actionability_key']))['eligible']
    assert not (await c.notification_candidate(first['run_id'],first['actionability_key']))['eligible']


async def test_notes_and_clarifications_append_and_cannot_rewrite_snapshots(configured):
    _,_,_,_,c,model=configured
    first=await c.analyze(str(uuid4()));snapshot=await saved(c,first['run_id'])
    revised=await c.save_brief(BriefRequest(schema_version='1',run_id=first['run_id'],expected_brief=first['doctor_brief'],text='My corrected note.'))
    corrected=await c.clarify(ClarifyRequest(schema_version='1',run_id=first['run_id'],concern_id='food_service',attribution='not_for_me'))
    assert corrected['revision']>revised['revision']>0
    assert 'food_service' not in corrected['actionable_area_ids']
    assert not (await c.notification_candidate(first['run_id'],first['actionability_key']))['eligible']
    assert await saved(c,first['run_id'])==snapshot
    async with c.transaction() as conn:
        events=await (await conn.execute('SELECT kind,value FROM companion_private.pattern_revisions ORDER BY revision')).fetchall()
        assert events==[('notes','My corrected note.'),('attribution','not_for_me')]
    for query in ["UPDATE companion_private.pattern_runs SET brief_override='overwrite'", "UPDATE companion_private.pattern_insights SET attribution='for_me'", "UPDATE companion_private.pattern_revisions SET value='overwrite'"]:
        with pytest.raises(psycopg.errors.RaiseException):
            async with c.transaction() as conn:await conn.execute(query)
    assert len(model.calls)==1


async def test_history_over_190_days_survives_raw_expiry_but_reset_erases(configured):
    _,a,_,store,c,_=configured
    first=await c.analyze(str(uuid4()))
    old=str(uuid4())
    async with c.transaction() as conn:
        await conn.execute("INSERT INTO companion_private.pattern_runs(account_id,run_id,request_id,input_digest,input_version,status,engine,evidence,is_demo,source_label,assessment,analyzed_at) SELECT account_id,%s,%s,input_digest,input_version,status,engine,evidence,is_demo,source_label,assessment,now()-interval '400 days' FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s",(old,str(uuid4()),a,first['run_id']))
        await conn.execute("INSERT INTO companion_private.pattern_revisions(account_id,run_id,kind,value) VALUES(%s,%s,'notes','Old saved note')",(a,old))
        await conn.execute("UPDATE companion_private.health_samples SET start_at=start_at-interval '500 days',end_at=end_at-interval '500 days'")
        await conn.execute("UPDATE companion_private.pattern_transactions SET event_at=event_at-interval '500 days'")
        await conn.execute("UPDATE companion_private.pattern_batches SET start_at=start_at-interval '500 days',end_at=end_at-interval '500 days'")
    original=await saved(c,old)
    async with c.transaction() as conn:await store._retain_patterns()
    assert await saved(c,old)==original
    empty=await c.analyze(str(uuid4()))
    assert {a['state'] for a in empty['areas']}=={'insufficient_data'}
    assert empty['findings']==[] and empty['actionable_area_ids']==[]
    assert (await c.get())['run_id']==empty['run_id']
    async with c.transaction() as conn:
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.health_samples')).fetchone())[0]==0
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.pattern_runs')).fetchone())[0]==3
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.pattern_revisions')).fetchone())[0]==1
    await store.handle('commit',{'document':document('erased-epoch'),'erase_receipts':True})
    async with c.transaction() as conn:
        for table in ['pattern_runs','pattern_revisions','pattern_requests','pattern_insights']:
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.'+table)).fetchone())[0]==0


async def test_revision_rls_prevents_cross_account_read_append_and_reset(configured):
    runtime,_,b,_,c,_=configured
    first=await c.analyze(str(uuid4()))
    await c.clarify(ClarifyRequest(schema_version='1',run_id=first['run_id'],concern_id='sleep',attribution='unsure'))
    from types import SimpleNamespace
    import asyncio
    store=PostgresPersistenceExtension(runtime,b)
    other=PatternCoordinator(SimpleNamespace(lock=asyncio.Lock(),clock=lambda:NOW),store)
    try:
        async with other.transaction() as conn:
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.pattern_revisions')).fetchone())[0]==0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with other.transaction() as conn:
                await conn.execute("INSERT INTO companion_private.pattern_revisions(account_id,run_id,kind,value) VALUES(%s,%s,'notes','cross account')",(c.store.account_id,first['run_id']))
        async with other.transaction() as conn:
            await conn.execute('DELETE FROM companion_private.pattern_runs')
        assert await saved(c,first['run_id']) is not None
    finally:await store.handle('close',{})


def test_missing_coverage_is_not_a_clean_bill_of_health():
    from companion.patterns import aggregate
    missing=aggregate([],[],NOW)
    assert len(area_results(missing))==3
    assert all(a.state=='insufficient_data' for a in area_results(missing))


def test_current_ui_never_appends_legacy_history():
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    ledger=(root/'fe/App/LocalCompanionApp.swift').read_text().split('struct LedgerView:')[1].split('private struct InferenceTimestamp')[0]
    assert 'inference_history' not in ledger
    ui=(root/'fe/App/BackendExperienceViews.swift').read_text()
    assert 'ForEach(value.areas, id: \\.area)' in ui and 'if value.is_current' in ui


async def test_existing_completed_assessment_backfill_is_idempotent(pg_dsn):
    from types import SimpleNamespace
    import asyncio
    from psycopg.types.json import Jsonb
    from companion.assessment_migrations import ASSESSMENT_MIGRATION
    from companion.pattern_migrations import MIGRATION_V3
    from companion.pattern_operator import generate
    from companion.patterns import aggregate
    from companion.postgres import PostgresRuntime,PostgresConfig
    runtime=PostgresRuntime(PostgresConfig(pg_dsn,local_test=True))
    await runtime.check()
    account=await runtime.identity(SimpleNamespace(issuer='https://issuer.invalid',subject='legacy-assessment'))
    run=str(uuid4())
    _,samples,transactions=generate(NOW)
    evidence=aggregate(samples,transactions,NOW).model_dump()
    async with await psycopg.AsyncConnection.connect(pg_dsn,autocommit=True) as conn:
        await conn.execute(MIGRATION_V3.replace(ASSESSMENT_MIGRATION,''))
        await conn.execute("INSERT INTO companion_private.pattern_runs(account_id,run_id,request_id,input_digest,input_version,status,engine,evidence,is_demo,brief_override,analyzed_at) VALUES(%s,%s,%s,%s,'legacy','complete','deterministic',%s,true,'Original note',now()-interval '400 days')",(account,run,str(uuid4()),'a'*64,Jsonb(evidence)))
        await conn.execute("INSERT INTO companion_private.pattern_insights(account_id,run_id,concern_id,rank,attribution) VALUES(%s,%s,'food_service',0,'not_for_me')",(account,run))
        await conn.execute(MIGRATION_V3)
        first=await (await conn.execute('SELECT evidence,assessment,brief_override FROM companion_private.pattern_runs')).fetchone()
        await conn.execute(MIGRATION_V3)
        assert await (await conn.execute('SELECT evidence,assessment,brief_override FROM companion_private.pattern_runs')).fetchone()==first
        assert first[0]==evidence and first[2]=='Original note'
        assert len(first[1]['areas'])==3
        assert first[1]['findings'][0]['attribution']=='not_for_me'
    store=PostgresPersistenceExtension(runtime,account)
    coordinator=PatternCoordinator(SimpleNamespace(lock=asyncio.Lock(),clock=lambda:NOW),store)
    try:
        view=await coordinator.get()
        assert view['findings']==[] and view['actionable_area_ids']==[]
        assert 'Original note' in view['doctor_brief']
    finally:
        await store.handle('close',{})
        await runtime.close()


def test_sparse_comparison_never_promotes_missing_coverage_to_a_warning():
    from companion.pattern_operator import generate
    from companion.patterns import aggregate,concerns
    _,samples,transactions=generate(NOW)
    dto=aggregate(samples,transactions,NOW)
    dto.recent.missing_steps=True
    dto.recent.missing_sleep=True
    dto.recent.missing_transactions=True
    assert concerns(dto)==[]
    assert all(area.state=='insufficient_data' for area in area_results(dto))
