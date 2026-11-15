"""Real disposable PostgreSQL only; reuses local fixture, never reads DATABASE_URL.

No APNs/network provider sends. The existing fixture owns a fresh loopback cluster,
creates a unique database per test and always shuts it down. All runtime operations
use a restricted non-owner role with FORCE RLS (not a superuser SQLite stand-in).
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime,timezone
from companion.patterns import aggregate,findings
import json
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
import pytest

from test_postgres import local_pg_cluster, pg_dsn  # noqa: F401; local-only fixtures
from companion.auth import Principal, _VERIFIED
from companion.pattern_migrations import MIGRATION_V3
from companion.postgres import PostgresConfig, PostgresRuntime
from companion.report_notification_migrations import migrate, TABLES
from companion.report_notification_postgres import (
    NotificationError, PostgresReportNotifications, PostgresNotificationWorker,
    HTTP2APNsProvider,
)


class Cipher:
    def __init__(self):
        self.cipher = Fernet(Fernet.generate_key())  # disposable test-only encryption

    def seal(self, value):
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def open(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))


def principal(subject='one', session='session-one'):
    # Explicit test authentication substitute, never a public handler construction path.
    return Principal('https://verified-test.invalid', subject, session, _proof=_VERIFIED)


def snapshot(epoch='epoch-one'):
    return {'state': {'data_epoch': epoch, 'preferences': {'cloud_consent': True},
                      'sources': [{'id': 'healthkit', 'enabled': True, 'consent': 'granted'}]}, 'meta': {}}


def assessment(ids=('activity',)):
    dto=aggregate([],[],datetime.now(timezone.utc))
    return {'findings':[v.model_dump() for v in findings(list(ids),dto)],
            'areas':[{'area':a,'state':'concern' if a in ids else 'no_current_concern','summary':'Offline test assessment.'} for a in ('activity','sleep','food_service')]}

@dataclass
class Context:
    queue: PostgresReportNotifications
    runtime: PostgresRuntime
    dsn: str
    admin_dsn: str
    role: str
    user: Principal
    account: str

    async def enroll(self, user=None, **kwargs):
        user = user or self.user
        state = await self.queue.status(user)
        if 'installation_id' in kwargs and 'expected_consent_revision' not in kwargs:
            record = next(r for r in state['installations'] if r['id'] == kwargs['installation_id'])
            kwargs['expected_consent_revision'] = record['consent_revision']
        return await self.queue.register(user,request_id=kwargs.pop('request_id', str(uuid4())),
            opt_in=kwargs.pop('opt_in',True),
            token=kwargs.pop('token','ab12'),epoch=state['epoch'],revision=state['revision'],**kwargs)

    async def report(self, *, status='complete', meaningful=True, account=None, ids=('activity',)):
        account = account or self.account
        run = str(uuid4())
        async with self.queue.scoped(account) as c:
            await c.execute('''INSERT INTO companion_private.pattern_runs
                (account_id,run_id,request_id,input_digest,input_version,status,engine,evidence,is_demo,assessment)
                VALUES(%s,%s,%s,%s,'test','running','deterministic','{}',false,%s)''', (account,run,str(uuid4()),uuid4().hex * 2,Jsonb(assessment(ids if meaningful else ()))))
            if meaningful:
                for rank,area in enumerate(ids):await c.execute('INSERT INTO companion_private.pattern_insights(account_id,run_id,concern_id,rank) VALUES(%s,%s,%s,%s)',(account,run,area,rank))
            await c.execute('UPDATE companion_private.pattern_runs SET status=%s WHERE account_id=%s AND run_id=%s', (status,account,run))
        return run

    async def rows(self, table='report_notification_outbox'):
        assert table in TABLES
        async with self.queue.scoped(self.account) as c:
            return await (await c.execute(sql.SQL('SELECT * FROM companion_private.{}').format(sql.Identifier(table)))).fetchall()


@pytest.fixture
async def context(pg_dsn):
    role = 'rn_' + uuid4().hex
    with psycopg.connect(pg_dsn,autocommit=True) as c:
        c.execute(MIGRATION_V3)
        c.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
        c.execute(sql.SQL('GRANT USAGE ON SCHEMA companion_private TO {}').format(sql.Identifier(role)))
        for table in ('accounts','snapshots','pattern_runs','pattern_insights','pattern_revisions','schema_version'):
            c.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON companion_private.{} TO {}').format(sql.Identifier(table),sql.Identifier(role)))
        migrate(c,role)
    dsn = make_conninfo(pg_dsn,user=role)
    runtime = PostgresRuntime(PostgresConfig(dsn,local_test=True))
    queue = PostgresReportNotifications(runtime,Cipher(),topic='test.bundle',environment='sandbox')
    user = principal()
    account = await runtime.identity(user)
    async with queue.scoped(account) as c:
        await c.execute('INSERT INTO companion_private.snapshots(account_id,body) VALUES(%s,%s)', (account,Jsonb(snapshot())))
    try:
        await queue.check()
        yield Context(queue,runtime,dsn,pg_dsn,role,user,account)
    finally:
        await runtime.close()


async def test_migration_idempotence_rls_and_no_global_version_change(context):
    t = context
    with psycopg.connect(t.admin_dsn,autocommit=True) as c:
        migrate(c,t.role)
        assert c.execute('SELECT version FROM companion_private.schema_version').fetchone() == (3,)
        rows = c.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname=ANY(%s)", (list(TABLES),)).fetchall()
        assert len(rows) == 5 and all(all(row) for row in rows)
        for table in TABLES:
            for role in ('anon','authenticated'):
                assert not c.execute("SELECT has_table_privilege(%s,%s,'SELECT')", (role,'companion_private.' + table)).fetchone()[0]
        with pytest.raises(ValueError):
            migrate(c,'postgres')
    await t.enroll()
    with psycopg.connect(t.dsn,autocommit=True) as c:
        for table in TABLES:
            assert c.execute(sql.SQL('SELECT count(*) FROM companion_private.{}').format(sql.Identifier(table))).fetchone() == (0,)


async def test_verified_identity_only_and_cross_account_rls(context):
    t = context
    with pytest.raises(NotificationError,match='verified_principal_required'):
        await t.queue.status(SimpleNamespace(issuer='x',subject='y'))
    await t.enroll()
    await t.report()
    assert await t.queue.reconcile(t.account) == 1
    other = await t.runtime.identity(principal('two','session-two'))
    async with t.queue.scoped(other) as c:
        assert await (await c.execute('SELECT * FROM companion_private.report_notification_outbox')).fetchall() == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await c.execute('INSERT INTO companion_private.report_notification_scopes(account_id,epoch) VALUES(%s,%s)', (t.account,'wrong'))


@pytest.mark.parametrize('status,meaningful,expected', [
    ('running',True,0), ('unavailable',True,1), ('complete',False,0), ('complete',True,1),
])
async def test_terminal_meaningful_only_and_commit_gap_reconciliation(context,status,meaningful,expected):
    t = context
    await t.enroll()
    await t.report(status=status,meaningful=meaningful)
    # No workflow hook was called. The committed event survives until a later worker.
    assert await t.rows() == []
    assert await t.queue.reconcile(t.account) == expected
    assert await t.queue.reconcile(t.account) == 0
    if expected:
        job = await t.queue.claim(t.account)
        assert job and job.due > 0
        assert (await t.queue.authorize(job)).token == 'ab12'


async def test_transaction_rollback_never_creates_event(context):
    t = context
    await t.enroll()
    with pytest.raises(RuntimeError):
        async with t.queue.scoped(t.account) as c:
            await c.execute('''INSERT INTO companion_private.pattern_runs
                (account_id,run_id,request_id,input_digest,input_version,status,engine,evidence,is_demo)
                VALUES(%s,%s,%s,%s,'test','complete','deterministic','{}',false)''',
                (t.account,str(uuid4()),str(uuid4()),'a'*64))
            raise RuntimeError('rollback')
    assert await t.rows('report_notification_events') == []
    assert await t.queue.reconcile(t.account) == 0


async def test_new_installation_no_historical_alert_and_cached_run_not_realerted(context):
    t = context
    await t.report()
    first = await t.enroll()
    assert await t.queue.reconcile(t.account) == 0
    run = await t.report()
    assert await t.queue.reconcile(t.account) == 1
    job = await t.queue.claim(t.account)
    assert await t.queue.finish(job,'accepted')
    # Completed assessment rows are immutable; cached read/reconcile is the replay.
    rotated = await t.enroll(installation_id=first['installation_id'],expected_token_version=first['token_version'],token='cd34')
    assert rotated['installation_id'] == first['installation_id']
    assert rotated['token_version'] == first['token_version'] + 1
    assert await t.queue.reconcile(t.account) == 0
    assert await t.queue.claim(t.account) is None
    await t.report()
    assert await t.queue.reconcile(t.account) == 0  # Same concern-set across a new run is not another alert.
    assert await t.queue.claim(t.account) is None


async def test_per_installation_idempotent_registration_and_reconciler(context):
    t = context
    request = str(uuid4())
    first = await t.enroll(request_id=request)
    assert await t.enroll(request_id=request) == first
    await t.enroll(token='cd34')
    await t.report()
    results = await asyncio.gather(*(t.queue.reconcile(t.account) for _ in range(4)))
    assert sum(results) == 2
    jobs = await asyncio.gather(*(t.queue.claim(t.account) for _ in range(4)))
    claimed = [j for j in jobs if j]
    assert len(claimed) == 2 and len({j.id for j in claimed}) == 2


async def test_utc_due_and_backoff_not_before_available(context):
    t = context
    await t.enroll()
    await t.report()
    await t.queue.reconcile(t.account)
    async with t.queue.scoped(t.account) as c:
        await c.execute("UPDATE companion_private.report_notification_outbox SET due_at=clock_timestamp()+interval '1 hour'")
    assert await t.queue.claim(t.account) is None
    async with t.queue.scoped(t.account) as c:
        await c.execute("UPDATE companion_private.report_notification_outbox SET due_at=clock_timestamp()-interval '1 second'")
    job = await t.queue.claim(t.account)
    assert await t.queue.finish(job,'retry')
    assert await t.queue.claim(t.account) is None
    async with t.queue.scoped(t.account) as c:
        row = await (await c.execute('SELECT available_at-clock_timestamp(),due_at FROM companion_private.report_notification_outbox')).fetchone()
        assert 25 < row[0].total_seconds() <= 30
        assert row[1].utcoffset().total_seconds() == 0


async def test_crash_recovery_fences_old_lease_and_exhausts_budget(context):
    t = context
    await t.enroll()
    await t.report()
    await t.queue.reconcile(t.account)
    first = await t.queue.claim(t.account)
    assert first.attempts == 1
    latest = first
    for attempt in range(2,6):
        async with t.queue.scoped(t.account) as c:
            await c.execute("UPDATE companion_private.report_notification_outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE status='sending'")
        latest = await t.queue.claim(t.account)
        assert latest.attempts == attempt and latest.lease != first.lease
        assert not await t.queue.finish(first,'accepted')
    async with t.queue.scoped(t.account) as c:
        await c.execute("UPDATE companion_private.report_notification_outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE status='sending'")
    assert await t.queue.claim(t.account) is None
    async with t.queue.scoped(t.account) as c:
        assert (await (await c.execute('SELECT status FROM companion_private.report_notification_outbox')).fetchone())[0] == 'failed'


@pytest.mark.parametrize('mode',['revoke','logout','reset','source_consent'])
async def test_authoritative_invalidation_and_no_late_completion(context,mode):
    t = context
    registration = await t.enroll()
    old = await t.queue.status(t.user)
    await t.report()
    await t.queue.reconcile(t.account)
    job = await t.queue.claim(t.account)
    if mode == 'revoke':
        await t.queue.revoke(t.user,registration['installation_id'],expected_consent_revision=registration['consent_revision'])
    elif mode == 'logout':
        await t.queue.logout(t.user)
        with pytest.raises(NotificationError,match='session_revoked'):
            await t.enroll()
    else:
        doc = snapshot('epoch-two' if mode=='reset' else 'epoch-one')
        if mode == 'source_consent':
            doc['state']['sources'][0]['consent'] = 'denied'
        async with t.queue.scoped(t.account) as c:
            await c.execute('UPDATE companion_private.snapshots SET body=%s WHERE account_id=%s', (Jsonb(doc),t.account))
        state = await t.queue.status(t.user)
        assert state['revision'] > old['revision']
        with pytest.raises(NotificationError,match='stale_scope'):
            await t.queue.register(t.user,request_id=str(uuid4()),token='ab12',epoch=old['epoch'],revision=old['revision'])
    assert await t.queue.authorize(job) is None
    assert not await t.queue.finish(job,'accepted')
    assert await t.queue.claim(t.account) is None


async def test_revoke_regrant_between_polls_does_not_restore_old_work(context):
    t = context
    install = await t.enroll()
    await t.report()
    await t.queue.reconcile(t.account)
    async with t.queue.scoped(t.account) as c:
        doc = snapshot()
        doc['state']['preferences']['cloud_consent'] = False
        await c.execute('UPDATE companion_private.snapshots SET body=%s WHERE account_id=%s', (Jsonb(doc),t.account))
        await c.execute('UPDATE companion_private.snapshots SET body=%s WHERE account_id=%s', (Jsonb(snapshot()),t.account))
    assert not (await t.queue.status(t.user))['installations'][0]['enabled']
    await t.enroll(installation_id=install['installation_id'],expected_token_version=install['token_version'])
    assert await t.queue.reconcile(t.account) == 0
    assert await t.queue.claim(t.account) is None


async def test_stale_token_invalidation_does_not_revoke_rotated_token(context):
    t = context
    install = await t.enroll()
    await t.report()
    await t.queue.reconcile(t.account)
    job = await t.queue.claim(t.account)
    old = await t.queue.authorize(job)
    await t.enroll(installation_id=install['installation_id'],expected_token_version=install['token_version'],token='cd34')
    await t.queue.invalidate(old)
    assert (await t.queue.authorize(job)).token == 'cd34'
    assert (await t.queue.status(t.user))['installations'][0]['enabled']


async def test_content_withdrawal_blocks_delivery(context):
    t = context
    await t.enroll()
    run = await t.report()
    await t.queue.reconcile(t.account)
    job = await t.queue.claim(t.account)
    async with t.queue.scoped(t.account) as c:
        await c.execute("INSERT INTO companion_private.pattern_revisions(account_id,run_id,kind,concern_id,value) VALUES(%s,%s,'attribution','activity','not_for_me')", (t.account,run))
    assert await t.queue.authorize(job) is None


class FakeProvider:
    def __init__(self, outcome='accepted'):
        self.calls = []
        self.outcome = outcome

    async def send(self, admission):
        self.calls.append(admission)
        return self.outcome, False


async def test_worker_integration_disabled_provider_and_generic_payload(context):
    t = context
    await t.enroll()
    await t.report()
    worker = PostgresNotificationWorker(t.queue)
    assert await worker.tick() == 0
    async with t.queue.scoped(t.account) as c:
        assert await (await c.execute('SELECT status,attempts FROM companion_private.report_notification_outbox')).fetchone() == ('pending',0)
    provider = FakeProvider()
    worker.provider = provider
    assert await worker.tick() == 1
    assert await worker.tick() == 0
    admission = provider.calls[0]
    assert 'ab12' not in repr(admission)

    class HTTP:
        calls = []
        async def post(self,url,**kwargs):
            self.calls.append((url,kwargs))
            return SimpleNamespace(status_code=200)

    http = HTTP()
    result = await HTTP2APNsProvider(http,lambda:'fake-test-only').send(admission)
    assert result == ('accepted',False)
    payload = http.calls[0][1]['json']
    assert set(payload) == {'aps','report_notification_id'}
    assert t.account not in json.dumps(payload)
    assert admission.job.report not in json.dumps(payload)


async def test_stale_registration_and_revoke_cannot_cross_consent_admission(context):
    t = context
    install = await t.enroll()
    scope = await t.queue.status(t.user)
    await t.queue.revoke(t.user,install['installation_id'],expected_consent_revision=install['consent_revision'])
    with pytest.raises(NotificationError,match='stale_consent_revision'):
        await t.queue.register(t.user,request_id=str(uuid4()),token='cd34',
            epoch=scope['epoch'],revision=scope['revision'],installation_id=install['installation_id'],
            expected_token_version=install['token_version'],expected_consent_revision=install['consent_revision'])
    renewed = await t.enroll(installation_id=install['installation_id'],expected_token_version=install['token_version'])
    await t.queue.revoke(t.user,install['installation_id'],expected_consent_revision=install['consent_revision'])
    state = await t.queue.status(t.user)
    assert state['installations'][0]['enabled']
    assert state['installations'][0]['consent_revision'] == renewed['consent_revision']


async def test_source_metadata_update_does_not_revoke_consent(context):
    t = context
    await t.enroll()
    old = await t.queue.status(t.user)
    doc = snapshot()
    doc['state']['sources'][0]['last_synced_at'] = '2026-11-14T22:00:00Z'
    async with t.queue.scoped(t.account) as c:
        await c.execute('UPDATE companion_private.snapshots SET body=%s WHERE account_id=%s', (Jsonb(doc),t.account))
    new = await t.queue.status(t.user)
    assert old['revision'] == new['revision']
    assert new['installations'][0]['enabled']


async def test_rejected_old_token_retries_rotated_token_without_new_job(context):
    t = context
    install = await t.enroll()
    await t.report()
    await t.queue.reconcile(t.account)

    class RotateDuringSend:
        async def send(self, admission):
            await t.enroll(installation_id=install['installation_id'],expected_token_version=install['token_version'],token='cd34')
            return 'cancelled', True

    assert await PostgresNotificationWorker(t.queue,RotateDuringSend()).dispatch_one(t.account)
    state = await t.queue.status(t.user)
    assert state['installations'][0]['enabled']
    async with t.queue.scoped(t.account) as c:
        assert (await (await c.execute('SELECT status,attempts FROM companion_private.report_notification_outbox')).fetchone()) == ('pending',1)


async def test_provider_exception_retries_and_revoke_inflight_is_fenced(context):
    t = context
    install = await t.enroll()
    await t.report()
    await t.queue.reconcile(t.account)

    class FailingProvider:
        async def send(self, admission):
            raise TimeoutError('test-only private diagnostic must not be stored')

    assert await PostgresNotificationWorker(t.queue,FailingProvider()).dispatch_one(t.account)
    async with t.queue.scoped(t.account) as c:
        await c.execute("UPDATE companion_private.report_notification_outbox SET available_at=clock_timestamp()-interval '1 second'")

    class RevokeDuringSend:
        async def send(self, admission):
            await t.queue.revoke(t.user,install['installation_id'],expected_consent_revision=install['consent_revision'])
            return 'accepted', False

    assert await PostgresNotificationWorker(t.queue,RevokeDuringSend()).dispatch_one(t.account)
    async with t.queue.scoped(t.account) as c:
        assert (await (await c.execute('SELECT status FROM companion_private.report_notification_outbox')).fetchone())[0] == 'cancelled'


async def test_migration_preserves_workflow_version_four_and_checks_triggers(context):
    t = context
    with psycopg.connect(t.admin_dsn,autocommit=True) as c:
        # Workflow's version marker, changed only inside this disposable database.
        c.execute('ALTER TABLE companion_private.schema_version DROP CONSTRAINT schema_version_version_check')
        c.execute('UPDATE companion_private.schema_version SET version=4')
        migrate(c,t.role)
        assert c.execute('SELECT version FROM companion_private.schema_version').fetchone() == (4,)
        c.execute('ALTER TABLE companion_private.pattern_runs DISABLE TRIGGER report_notification_terminal')
    with pytest.raises(NotificationError,match='notification_migration_or_restricted_role_required'):
        await t.queue.check()


async def test_encrypted_tokens_and_reset_deletion_cascade(context):
    t = context
    await t.enroll()
    await t.report()
    await t.queue.reconcile(t.account)
    job = await t.queue.claim(t.account)
    async with t.queue.scoped(t.account) as c:
        cipher = (await (await c.execute('SELECT ciphertext FROM companion_private.report_notification_registrations')).fetchone())[0]
        assert 'ab12' not in cipher
        assert t.queue.cipher.open(cipher) == {'token':'ab12'}
        await c.execute('UPDATE companion_private.snapshots SET body=%s WHERE account_id=%s', (Jsonb(snapshot('epoch-two')),t.account))
        await c.execute('DELETE FROM companion_private.pattern_runs WHERE account_id=%s', (t.account,))
    assert await t.rows('report_notification_events') == []
    assert await t.rows() == []
    assert not await t.queue.finish(job,'accepted')


async def test_missing_migration_fails_closed_without_runtime_ddl(context):
    t = context
    with psycopg.connect(t.admin_dsn,autocommit=True) as c:
        c.execute('DROP TABLE companion_private.report_notification_outbox')
    with pytest.raises(NotificationError,match='notification_migration_or_restricted_role_required'):
        await t.queue.check()
    with psycopg.connect(t.admin_dsn,autocommit=True) as c:
        assert c.execute("SELECT to_regclass('companion_private.report_notification_outbox')").fetchone() == (None,)


async def test_concurrent_token_rotation_is_compare_and_set(context):
    t = context
    install = await t.enroll()
    args = {'installation_id':install['installation_id'],
            'expected_token_version':install['token_version'],
            'expected_consent_revision':install['consent_revision']}
    results = await asyncio.gather(t.enroll(token='cd34',**args),t.enroll(token='ef56',**args),return_exceptions=True)
    assert sum(isinstance(r,dict) for r in results) == 1
    assert sum(isinstance(r,NotificationError) and str(r)=='stale_token_version' for r in results) == 1


async def test_background_refresh_cannot_create_or_restore_notification_consent(context):
    t = context
    with pytest.raises(NotificationError,match='notification_opt_in_required'):
        await t.enroll(opt_in=False)
    install = await t.enroll()
    rotated = await t.enroll(installation_id=install['installation_id'],
        expected_token_version=install['token_version'],token='cd34',opt_in=False)
    await t.queue.revoke(t.user,rotated['installation_id'],expected_consent_revision=rotated['consent_revision'])
    with pytest.raises(NotificationError,match='notification_opt_in_required'):
        await t.enroll(installation_id=rotated['installation_id'],
            expected_token_version=rotated['token_version'],token='cd34',opt_in=False)


async def test_scheduler_outage_status_is_sanitized_and_stop_is_respected():
    stop = asyncio.Event()

    class Queue:
        async def check(self):
            pass
        async def accounts(self):
            stop.set()
            raise RuntimeError('private test connection details')

    worker = PostgresNotificationWorker(Queue())
    await worker.run(stop)
    assert worker.status() == {'delivery_configured':False,'error':'notification_worker_unavailable'}

async def test_provider_outage_does_not_flush_historical_or_superseded_report_alerts(context):
    t=context
    await t.enroll()
    old=await t.report();await t.queue.reconcile(t.account)
    # Existing pending work remains at zero attempts while disabled, then expires safely.
    worker=PostgresNotificationWorker(t.queue)
    assert await worker.tick()==0
    async with t.queue.scoped(t.account) as c:
        await c.execute("UPDATE companion_private.report_notification_events SET completed_at=clock_timestamp()-interval '25 hours' WHERE run_id=%s",(old,))
    assert await t.queue.claim(t.account) is None
    recent=await t.report();await t.queue.reconcile(t.account)
    newest=await t.report();await t.queue.reconcile(t.account)
    job=await t.queue.claim(t.account)
    assert job.report==newest and job.report!=recent
    assert await t.queue.finish(job,'accepted')
    assert await t.queue.claim(t.account) is None
