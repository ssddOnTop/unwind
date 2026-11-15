"""Current assessment projection and delivery races on restricted disposable PG."""
import asyncio
from copy import deepcopy
from uuid import uuid4
import psycopg
import pytest
from psycopg.types.json import Jsonb
from companion.pattern_store import notification_candidate
from companion.report_notification_postgres import PostgresNotificationWorker
from companion.report_notification_migrations import migrate
from test_report_notification_postgres import context,local_pg_cluster,pg_dsn,assessment,FakeProvider

async def correct(t,run,area,value):
    async with t.queue.scoped(t.account) as c:
        await c.execute("INSERT INTO companion_private.pattern_revisions(account_id,run_id,kind,concern_id,value) VALUES(%s,%s,'attribution',%s,%s)",(t.account,run,area,value))

async def projection(t):
    async with t.queue.scoped(t.account) as c:return await notification_candidate(c,t.account,None,None)

async def saved(t,run):
    async with t.queue.scoped(t.account) as c:
        return await (await c.execute('SELECT assessment,evidence,status FROM companion_private.pattern_runs WHERE account_id=%s AND run_id=%s',(t.account,run))).fetchone()

async def test_same_run_correction_changes_key_cancels_lease_and_preserves_append_only_history(context):
    t=context;await t.enroll();run=await t.report(ids=('activity','sleep'))
    before=await saved(t,run);first=await projection(t)
    assert await t.queue.reconcile(t.account)==1
    old=await t.queue.claim(t.account);assert old.actionability_key==first['actionability_key']
    await correct(t,run,'activity','not_for_me')
    assert await t.queue.authorize(old) is None
    assert not await t.queue.finish(old,'accepted')
    second=await projection(t)
    assert second['current_run_id']==run and second['actionability_key']!=first['actionability_key']
    assert second['actionable_area_ids']==['sleep']
    assert await t.queue.reconcile(t.account)==1
    new=await t.queue.claim(t.account);assert new.report==old.report and new.actionability_key!=old.actionability_key
    assert await t.queue.authorize(new)
    assert await t.queue.finish(new,'accepted')
    assert await saved(t,run)==before
    async with t.queue.scoped(t.account) as c:
        assert (await (await c.execute('SELECT count(*) FROM companion_private.pattern_revisions')).fetchone())[0]==1
        with pytest.raises(psycopg.errors.RaiseException):
            async with c.transaction():await c.execute("UPDATE companion_private.pattern_runs SET evidence='{}' WHERE run_id=%s",(run,))
    # A restoration cannot resend an uncertain prior key; nor can it resend the accepted sleep key.
    await correct(t,run,'activity','for_me');assert await t.queue.reconcile(t.account)==0
    await correct(t,run,'activity','not_for_me');assert await t.queue.reconcile(t.account)==0

async def test_same_concerns_new_run_retargets_only_unattempted_then_deduplicates_forever(context):
    t=context;await t.enroll();first=await t.report()
    assert await t.queue.reconcile(t.account)==1
    async with t.queue.scoped(t.account) as c:
        identity=(await (await c.execute('SELECT notification_id FROM companion_private.report_notification_outbox')).fetchone())[0]
    second=await t.report();assert first!=second
    assert await t.queue.reconcile(t.account)==1
    job=await t.queue.claim(t.account);assert job.report==second and job.id==str(identity)
    assert await t.queue.authorize(job);assert await t.queue.finish(job,'accepted')
    await t.report();assert await t.queue.reconcile(t.account)==0;assert await t.queue.claim(t.account) is None
    # Resolution does not erase an accepted actionability tombstone.
    await t.report(meaningful=False);await t.queue.reconcile(t.account)
    await t.report();assert await t.queue.reconcile(t.account)==0

@pytest.mark.parametrize('status,meaningful',[('running',True),('complete',False),('unavailable',False)])
async def test_nonactionable_or_inflight_new_current_supersedes_old_concerns(context,status,meaningful):
    t=context;await t.enroll();old=await t.report();await t.queue.reconcile(t.account)
    job=await t.queue.claim(t.account)
    new=await t.report(status=status,meaningful=meaningful)
    assert await t.queue.reconcile(t.account)==0
    assert await t.queue.authorize(job) is None
    assert await t.queue.claim(t.account) is None
    async with t.queue.scoped(t.account) as c:
        assert (await (await c.execute('SELECT count(*) FROM companion_private.pattern_runs')).fetchone())[0]==2
        current=await notification_candidate(c,t.account,old,job.actionability_key)
        assert current['current_run_id']==new and not current['eligible']

async def test_concurrent_reconcile_claim_and_stale_authorize_cannot_cancel_current_job(context):
    t=context;await t.enroll();await t.report();assert sum(await asyncio.gather(*(t.queue.reconcile(t.account) for _ in range(8))))==1
    claims=await asyncio.gather(*(t.queue.claim(t.account) for _ in range(4)))
    old=next(j for j in claims if j);assert sum(j is not None for j in claims)==1
    newrun=await t.report(ids=('sleep',));assert await t.queue.reconcile(t.account)==1
    assert await t.queue.authorize(old) is None
    current=await t.queue.claim(t.account);assert current and current.report==newrun
    assert await t.queue.authorize(current)

async def test_correction_during_send_fences_completion_and_generic_payload_has_no_key(context):
    t=context;await t.enroll();run=await t.report();await t.queue.reconcile(t.account)
    entered=asyncio.Event();release=asyncio.Event()
    class Provider:
        calls=[]
        async def send(self,admission):
            self.calls.append(admission);entered.set();await release.wait();return 'accepted',False
    provider=Provider();worker=PostgresNotificationWorker(t.queue,provider)
    task=asyncio.create_task(worker.dispatch_one(t.account));await asyncio.wait_for(entered.wait(),3)
    await correct(t,run,'activity','not_for_me');release.set();assert await task
    assert await t.queue.reconcile(t.account)==0
    async with t.queue.scoped(t.account) as c:
        assert (await (await c.execute('SELECT status,lease_token FROM companion_private.report_notification_outbox')).fetchone())==('cancelled',None)
    from companion.report_notifications import apns_payload
    payload=str(apns_payload(provider.calls[0].job))
    assert run not in payload and provider.calls[0].job.actionability_key not in payload
    # An already handed-off generic alert cannot be recalled, but no repeat is allowed.
    await correct(t,run,'activity','for_me');assert await t.queue.reconcile(t.account)==0

async def test_history_corrections_notes_and_repeated_migration_do_not_realert(context):
    t=context;await t.enroll();historical=await t.report(ids=('activity',));current=await t.report(ids=('sleep',))
    await t.queue.reconcile(t.account)
    await correct(t,historical,'activity','not_for_me')
    async with t.queue.scoped(t.account) as c:
        await c.execute("INSERT INTO companion_private.pattern_revisions(account_id,run_id,kind,value) VALUES(%s,%s,'notes','Private text never sent')",(t.account,current))
    with psycopg.connect(t.admin_dsn,autocommit=True) as c:migrate(c,t.role)
    assert await t.queue.reconcile(t.account)==0
    job=await t.queue.claim(t.account);assert job.report==current and await t.queue.authorize(job)
    assert await t.queue.finish(job,'accepted')
    with psycopg.connect(t.admin_dsn,autocommit=True) as c:migrate(c,t.role)
    assert await t.queue.reconcile(t.account)==0

async def test_disabled_provider_consumes_no_attempts_and_actionability_is_installation_scoped(context):
    t=context;first=await t.enroll();second=await t.enroll();await t.report()
    disabled=PostgresNotificationWorker(t.queue)
    assert await disabled.tick()==0
    async with t.queue.scoped(t.account) as c:
        rows=await (await c.execute('SELECT actionability_key,attempts FROM companion_private.report_notification_outbox')).fetchall()
        assert len(rows)==2 and len({r[0] for r in rows})==1 and all(r[1]==0 for r in rows)
    provider=FakeProvider();worker=PostgresNotificationWorker(t.queue,provider)
    assert await worker.dispatch_one(t.account);assert await worker.dispatch_one(t.account)
    assert len(provider.calls)==2
    await t.report();assert await t.queue.reconcile(t.account)==0

async def test_upgrade_backfills_prior_accepted_keys_without_realert_or_history_rewrite(context):
    t=context;registration=await t.enroll();first=await t.report(ids=('activity','sleep'));second=await t.report(ids=('activity','sleep'))
    # Disposable approximation of the reviewed predecessor schema, not a production downgrade.
    with psycopg.connect(t.admin_dsn,autocommit=True) as c:
        c.execute('DROP INDEX companion_private.report_notification_actionability')
        c.execute('ALTER TABLE companion_private.report_notification_outbox DROP COLUMN actionability_key')
        c.execute('ALTER TABLE companion_private.report_notification_outbox ADD UNIQUE(account_id,epoch,run_id,installation_id)')
    async with t.queue.scoped(t.account) as c:
        for run in (first,second):
            await c.execute("INSERT INTO companion_private.report_notification_outbox(account_id,epoch,run_id,installation_id,scope_revision,consent_revision,status,attempts) VALUES(%s,'epoch-one',%s,%s,1,1,'accepted',1)",(t.account,run,registration['installation_id']))
    before=await saved(t,first),await saved(t,second)
    with psycopg.connect(t.admin_dsn,autocommit=True) as c:migrate(c,t.role)
    await t.queue.check()
    assert (await saved(t,first),await saved(t,second))==before
    latest=await t.report(ids=('activity','sleep'))
    current=await projection(t)
    async with t.queue.scoped(t.account) as c:
        rows=await (await c.execute('SELECT actionability_key,status FROM companion_private.report_notification_outbox')).fetchall()
        assert len(rows)==2 and sum(r[0]==current['actionability_key'] for r in rows)==1
        assert all(r[1]=='accepted' for r in rows)
    assert await t.queue.reconcile(t.account)==0 and await t.queue.claim(t.account) is None

from test_patterns import configured
from companion.patterns import ClarifyRequest

async def test_worker_candidate_matches_real_coordinator_current_view_and_corrections(configured):
    _,_,_,_,coordinator,_=configured
    first=await coordinator.analyze(str(uuid4()))
    for area in (None,'activity','sleep','food_service'):
        if area:await coordinator.clarify(ClarifyRequest(schema_version='1',run_id=first['run_id'],concern_id=area,attribution='not_for_me'))
        view=await coordinator.get()
        method=await coordinator.notification_candidate(view['run_id'],view['actionability_key'])
        async with coordinator.transaction() as c:
            worker=await notification_candidate(c,coordinator.store.account_id,view['run_id'],view['actionability_key'])
        assert worker==method
        assert worker['actionability_key']==view['actionability_key']
        assert worker['actionable_area_ids']==view['actionable_area_ids']
        assert worker['eligible']==bool(view['actionable_area_ids'])

async def test_reconcile_projection_lock_serializes_correction_and_cancels_after_commit(context,monkeypatch):
    import companion.report_notification_postgres as module
    t=context;await t.enroll();run=await t.report()
    entered=asyncio.Event();release=asyncio.Event();original=module.notification_candidate
    async def paused(*args):
        result=await original(*args)
        if not entered.is_set():entered.set();await release.wait()
        return result
    with monkeypatch.context() as patch:
        patch.setattr(module,'notification_candidate',paused)
        reconcile=asyncio.create_task(t.queue.reconcile(t.account));await asyncio.wait_for(entered.wait(),3)
        correction=asyncio.create_task(correct(t,run,'activity','not_for_me'))
        await asyncio.sleep(.02)
        assert not correction.done(), 'Correction commit must serialize with the authoritative projection read'
        release.set();assert await reconcile==1;await correction
    assert await t.queue.claim(t.account) is None
    assert await t.queue.reconcile(t.account)==0

from test_report_notification_http import http_app,eligible,status,register_body,report
from test_postgres_http import signing_key

async def test_authenticated_reset_erases_key_tombstones_and_requires_new_consent(http_app):
    h,_,provider=http_app
    async with h.client() as client:
        state=await eligible(client,h);notification=h.app.state.report_notifications
        account=(await h.handle('a')).account_id
        body=register_body(await status(client,h));registration=await client.post('/report-notifications/v1/register',headers=h.headers(),json=body)
        assert registration.status_code==200
        installation=registration.json()['installation_id']
        await report(notification,account);assert await notification.worker.tick()==1
        assert len(provider.calls)==1
        reset=await client.request('DELETE','/data',headers=h.headers(),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'confirm':'delete_all'})
        assert reset.status_code==200
        async with notification.queue.scoped(account) as c:
            assert (await (await c.execute('SELECT count(*) FROM companion_private.report_notification_outbox')).fetchone())[0]==0
        assert await notification.worker.tick()==0
        state=await eligible(client,h)
        replacement=register_body(await status(client,h),request_id=body['request_id'])
        renewed=await client.post('/report-notifications/v1/register',headers=h.headers(),json=replacement)
        assert renewed.status_code==200 and renewed.json()['installation_id']==installation
        await report(notification,account);assert await notification.worker.tick()==1
        assert len(provider.calls)==2
