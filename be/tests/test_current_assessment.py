"""Disposable PostgreSQL and fake model only; no configured service or provider."""
from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
import pytest

from companion.patterns import Aggregate, BriefRequest, ClarifyRequest, area_results
from companion.pattern_store import PatternCoordinator
from companion.extensions.postgres import PostgresPersistenceExtension
from test_patterns import configured, NOW
from test_postgres import local_pg_cluster, pg_dsn, document
from scripted_models import review_model


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


@pytest.mark.parametrize('remaining_active', [True, False])
async def test_named_dataset_cleanup_preserves_all_history_and_supersedes_from_remaining_evidence(configured, monkeypatch, remaining_active):
    import json
    from types import SimpleNamespace
    from companion import pattern_operator as operator
    from companion.patterns import aggregate
    runtime,account,_,store,c,model=configured
    _,_,transactions=operator.generate(NOW)
    unrelated=str(uuid4())
    async with c.transaction() as conn:
        await conn.execute('UPDATE companion_private.pattern_batches SET active=false WHERE account_id=%s',(account,))
        await conn.execute("INSERT INTO companion_private.pattern_batches(account_id,batch_id,dataset,provenance,start_at,end_at) VALUES(%s,%s,'unrelated-bank','providerSandbox',%s,%s)",(account,unrelated,NOW-timedelta(days=190),NOW))
        for tx in transactions:
            await conn.execute('INSERT INTO companion_private.pattern_transactions(account_id,batch_id,transaction_id,event_at,timezone,amount,currency,category) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(account,unrelated,tx['transaction_id'],tx['event_at'],tx['timezone'],tx['amount'],tx['currency'],tx['category']))
    calls=[]
    class ExactFake:
        async def ainvoke(self, messages, **kwargs):
            calls.append(True)
            allowed=json.loads(messages[0].content.rsplit(': ',1)[1])
            return SimpleNamespace(content=json.dumps({'concern_ids':allowed}))
    c.model_factory=lambda:review_model(ExactFake())
    earlier=await c.analyze(str(uuid4()))
    assert earlier['status']=='complete' and earlier['actionable_area_ids']==['food_service']
    earlier=await c.clarify(ClarifyRequest(schema_version='1',run_id=earlier['run_id'],concern_id='food_service',attribution='for_me'))
    await c.save_brief(BriefRequest(schema_version='1',run_id=earlier['run_id'],expected_brief=earlier['doctor_brief'],text='Unrelated assessment note'))
    async with c.transaction() as conn:
        await conn.execute('UPDATE companion_private.pattern_batches SET active=true WHERE account_id=%s',(account,))
    latest=await c.analyze(str(uuid4()))
    assert latest['status']=='complete' and set(latest['actionable_area_ids'])=={'activity','sleep','food_service'}
    latest=await c.clarify(ClarifyRequest(schema_version='1',run_id=latest['run_id'],concern_id='sleep',attribution='unsure'))
    await c.save_brief(BriefRequest(schema_version='1',run_id=latest['run_id'],expected_brief=latest['doctor_brief'],text='Combined assessment note'))
    old_ids=[earlier['run_id'],latest['run_id']]
    tables=('pattern_runs','pattern_insights','pattern_requests','pattern_revisions')
    async def history(conn):
        return {table:await (await conn.execute('SELECT to_jsonb(t) FROM companion_private.'+table+' t WHERE account_id=%s AND run_id=ANY(%s::uuid[]) ORDER BY to_jsonb(t)::text',(account,old_ids))).fetchall() for table in tables}
    async with c.transaction() as conn:
        await conn.execute('UPDATE companion_private.pattern_batches SET active=%s WHERE account_id=%s AND batch_id=%s',(remaining_active,account,unrelated))
        before=await history(conn)
    await store.handle('close',{})  # Operator requires the runtime advisory lock released.
    async def fail_projection(*args):raise RuntimeError('Synthetic projection failure')
    async with runtime.pool.connection() as conn:
        with monkeypatch.context() as patch:
            patch.setattr(operator,'append_cleanup_assessment',fail_projection)
            with pytest.raises(RuntimeError,match='Synthetic projection failure'):
                await operator.load(conn,account_id=account,issuer='https://issuer.invalid',subject='private-subject',now=NOW,remove=True)
        async with conn.transaction():
            await conn.execute("SELECT set_config('companion.account_id',%s,true)",(account,))
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.pattern_batches WHERE dataset=%s',(operator.DATASET,))).fetchone())[0]==1
            assert await history(conn)==before
        await operator.load(conn,account_id=account,issuer='https://issuer.invalid',subject='private-subject',now=NOW,remove=True)
        await operator.load(conn,account_id=account,issuer='https://issuer.invalid',subject='private-subject',now=NOW,remove=True)  # no-op repeat
    current=await c.get()
    assert current['run_id'] not in old_ids and current['is_current'] and current['engine']=='deterministic'
    assert current['evidence']==aggregate([],transactions if remaining_active else [],NOW).model_dump()
    assert current['actionable_area_ids']==(['food_service'] if remaining_active else [])
    assert {a['state'] for a in current['areas'] if a['area'] in {'activity','sleep'}}=={'insufficient_data'}
    assert not (await c.notification_candidate(latest['run_id'],latest['actionability_key']))['eligible']
    async with c.transaction() as conn:
        assert await history(conn)==before  # Every prior row, including bindings/events, byte-equivalent JSON.
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.pattern_runs')).fetchone())[0]==3
        assert (await (await conn.execute('SELECT dataset FROM companion_private.pattern_batches')).fetchall())==[('unrelated-bank',)]
        assert (await (await conn.execute('SELECT count(*) FROM companion_private.pattern_transactions')).fetchone())[0]==len(transactions)
        original_request=await (await conn.execute('SELECT request_id FROM companion_private.pattern_requests WHERE run_id=%s',(old_ids[0],))).fetchone()
    replay=await c.analyze(str(original_request[0]))
    assert replay['run_id']==old_ids[0] and not replay['is_current'] and replay['actionable_area_ids']==[]
    assert len(calls)==2  # Cleanup and old-request replay never invoke the fake model.
    await store.handle('commit',{'document':document('explicit-reset'),'erase_receipts':True})
    async with c.transaction() as conn:
        for table in tables:
            assert (await (await conn.execute('SELECT count(*) FROM companion_private.'+table)).fetchone())[0]==0


@pytest.mark.parametrize('version', [3,4])
@pytest.mark.parametrize('command', ['assessment','pattern'])
def test_explicit_operator_upgrade_preserves_v3_v4_data_marker_and_restricted_grants(pg_dsn, monkeypatch, capsys, version, command):
    import sys
    import json
    from types import SimpleNamespace
    from psycopg import sql
    from psycopg.types.json import Jsonb
    from companion import assessment_migrations as assessment, pattern_migrations as pattern, postgres
    from companion.workflow_migrations import MIGRATION_V4
    from companion.patterns import aggregate
    account,run,request=str(uuid4()),str(uuid4()),str(uuid4())
    role='assessment_'+uuid4().hex
    with psycopg.connect(pg_dsn,autocommit=True) as conn:
        conn.execute(pattern.MIGRATION_V3.replace(assessment.ASSESSMENT_MIGRATION,''))
        if version==4:conn.execute(MIGRATION_V4)
        conn.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA companion_private TO {}').format(sql.Identifier(role)))
        conn.execute("INSERT INTO companion_private.accounts(account_id,issuer,subject) VALUES(%s,'https://issuer.invalid','upgrade-test')",(account,))
        conn.execute("INSERT INTO companion_private.snapshots(account_id,body) VALUES(%s,'{\"keep\":true}')",(account,))
        conn.execute("INSERT INTO companion_private.pattern_runs(account_id,run_id,request_id,input_digest,input_version,status,engine,evidence,is_demo,brief_override) VALUES(%s,%s,%s,%s,'legacy','complete','deterministic',%s,false,'Preserved note')",(account,run,request,'b'*64,Jsonb(aggregate([],[],NOW).model_dump())))
        conn.execute('INSERT INTO companion_private.pattern_requests(account_id,run_id,request_id) VALUES(%s,%s,%s)',(account,run,request))
        if version==4:
            conn.execute("INSERT INTO companion_private.pattern_jobs(account_id,epoch,job_id,request_id,utc_date,origin,digest,available_at,created_at,updated_at) VALUES(%s,'epoch',%s,%s,current_date,'operator',%s,now(),now(),now())",(account,str(uuid4()),str(uuid4()),'c'*64))
        marker=conn.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='companion_private.schema_version'::regclass ORDER BY conname").fetchall()
        original=conn.execute('SELECT evidence,brief_override,status,request_id FROM companion_private.pattern_runs').fetchall()
        jobs=conn.execute('SELECT to_jsonb(j) FROM companion_private.pattern_jobs j').fetchall() if version==4 else None
        with pytest.raises(ValueError):assessment.upgrade_assessments(conn,'postgres')
        assert conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema='companion_private' AND table_name='pattern_runs' AND column_name='assessment'").fetchone()[0]==0
    # Exercise the actual explicit command functions; only the test configuration
    # admits the disposable loopback database without production TLS requirements.
    monkeypatch.setenv('DATABASE_URL',pg_dsn)
    monkeypatch.setattr(postgres,'PostgresConfig',lambda dsn:SimpleNamespace(dsn=dsn))
    module,flag=(assessment,'--confirm-current-assessment-schema') if command=='assessment' else (pattern,'--confirm-private-pattern-schema')
    monkeypatch.setattr(sys,'argv',['operator',flag,'--runtime-role',role])
    module.main();assert json.loads(capsys.readouterr().out)['migration']=='completed'
    with psycopg.connect(pg_dsn,autocommit=True) as conn:
        conn.execute("INSERT INTO companion_private.pattern_revisions(account_id,run_id,kind,value) VALUES(%s,%s,'notes','New immutable note')",(account,run))
        first=conn.execute('SELECT to_jsonb(r) FROM companion_private.pattern_runs r').fetchall()
        revisions=conn.execute('SELECT to_jsonb(r) FROM companion_private.pattern_revisions r').fetchall()
    module.main()
    with psycopg.connect(pg_dsn,autocommit=True) as conn:
        assert conn.execute('SELECT version FROM companion_private.schema_version').fetchall()==[(version,)]
        assert conn.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='companion_private.schema_version'::regclass ORDER BY conname").fetchall()==marker
        assert conn.execute('SELECT evidence,brief_override,status,request_id FROM companion_private.pattern_runs').fetchall()==original
        assert conn.execute('SELECT to_jsonb(r) FROM companion_private.pattern_runs r').fetchall()==first
        assert conn.execute('SELECT to_jsonb(r) FROM companion_private.pattern_revisions r').fetchall()==revisions
        assert conn.execute('SELECT run_id,request_id FROM companion_private.pattern_requests').fetchall()==[(UUID(run),UUID(request))]
        assert conn.execute('SELECT body FROM companion_private.snapshots').fetchall()==[({'keep':True},)]
        if version==4:assert conn.execute('SELECT to_jsonb(j) FROM companion_private.pattern_jobs j').fetchall()==jobs
        for table in assessment.ASSESSMENT_TABLES:
            assert conn.execute('SELECT has_table_privilege(%s,%s,\'SELECT,INSERT,UPDATE,DELETE\')',(role,'companion_private.'+table)).fetchone()[0]
        for table in ('snapshots','pattern_batches','health_samples') + (('pattern_jobs',) if version==4 else ()):
            assert not conn.execute('SELECT has_table_privilege(%s,%s,\'SELECT\')',(role,'companion_private.'+table)).fetchone()[0]
    with psycopg.connect(pg_dsn+' user='+role,autocommit=True) as conn:
        conn.execute("SELECT set_config('companion.account_id',%s,false)",(account,))
        assert conn.execute('SELECT count(*) FROM companion_private.pattern_revisions').fetchone()[0]==1
        conn.execute("SELECT set_config('companion.account_id',%s,false)",(str(uuid4()),))
        assert conn.execute('SELECT count(*) FROM companion_private.pattern_runs').fetchone()[0]==0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO companion_private.pattern_revisions(account_id,run_id,kind,value) VALUES(%s,%s,'notes','wrong tenant')",(account,run))
