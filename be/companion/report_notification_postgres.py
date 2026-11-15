"""PostgreSQL-only report notification runtime; no SQLite fallback or implicit DDL.

Authenticated handlers call status/register/revoke/logout with a verified Principal.
The worker reconciles committed terminal events and dispatches immediately when due.
All account transactions use the existing companion.account_id RLS convention.
No report details enter the queue or APNs payload. No provider is enabled by default.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
from typing import Protocol
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from .auth import Principal
from .pattern_store import notification_candidate
from .report_notifications import APNsTransport, Job, MAX_ATTEMPTS, LEASE_SECONDS


class NotificationError(ValueError):
    """Safe handler-facing error code (never token, identity or SQL text)."""


class TokenCipher(Protocol):
    """Protected authenticated encryption, e.g. existing PrivateVault.seal/open.
    The production caller owns key durability and rotation; never use plaintext.
    """
    def seal(self, value: dict) -> str: ...
    def open(self, ciphertext: str) -> dict: ...


def permitted(document):
    state = document.get('state', {})
    return bool(state.get('preferences', {}).get('cloud_consent') and any(
        s.get('id') in {'healthkit', 'plaid'} and s.get('enabled') and s.get('consent') == 'granted'
        for s in state.get('sources', [])))


def session_key(principal: Principal) -> str:
    return hashlib.sha256((principal.issuer + '\0' + principal.session_id).encode()).hexdigest()


@dataclass(frozen=True)
class Admission:
    job: Job
    token: str = field(repr=False)
    token_version: int
    topic: str
    environment: str


# SQL gates registration/session/freshness. The shared notification_candidate
# authority additionally binds every enqueue, claim and send to the current run/key.
VALID = """
 o.epoch=s.epoch AND o.scope_revision=s.revision
 AND r.epoch=s.epoch AND r.scope_revision=s.revision AND r.enabled
 AND o.consent_revision=r.consent_revision AND NOT ns.revoked
 AND o.actionability_key IS NOT NULL
 AND e.completed_at>=clock_timestamp()-interval '24 hours'

"""
JOINS = """
 JOIN companion_private.report_notification_events e ON e.account_id=o.account_id AND e.epoch=o.epoch AND e.run_id=o.run_id
 JOIN companion_private.report_notification_scopes s ON s.account_id=o.account_id
 JOIN companion_private.report_notification_registrations r
   ON r.account_id=o.account_id AND r.installation_id=o.installation_id
 JOIN companion_private.report_notification_sessions ns
   ON ns.account_id=r.account_id AND ns.session_key=r.session_key
 JOIN companion_private.pattern_runs p ON p.account_id=o.account_id AND p.run_id=o.run_id
"""


class PostgresReportNotifications:
    def __init__(self, runtime, cipher: TokenCipher, *, topic: str, environment: str):
        if not topic or environment not in {'sandbox', 'production'}:
            raise NotificationError('invalid_provider_configuration')
        self.runtime, self.cipher = runtime, cipher
        self.topic, self.environment = topic, environment
        self.cursor = UUID(int=0)

    async def check(self):
        """Fail closed until explicit migration; never create tables or fall back."""
        await self.runtime.open()
        async with self.runtime.pool.connection() as c:
            row = await (await c.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='companion_private' AND c.relname IN ('report_notification_scopes','report_notification_sessions','report_notification_registrations','report_notification_events','report_notification_outbox') AND c.relrowsecurity AND c.relforcerowsecurity")).fetchone()
            role = await (await c.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user')).fetchone()
            triggers = await (await c.execute("SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='companion_private' AND NOT t.tgisinternal AND t.tgenabled='O' AND ((c.relname='pattern_runs' AND t.tgname='report_notification_terminal') OR (c.relname='snapshots' AND t.tgname='report_notification_invalidate') OR (c.relname='pattern_revisions' AND t.tgname='report_notification_correction'))")).fetchone()
            columns=await (await c.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema='companion_private' AND table_name='report_notification_outbox' AND column_name='actionability_key'")).fetchone()
            index=await (await c.execute("SELECT count(*) FROM pg_index i WHERE i.indexrelid=to_regclass('companion_private.report_notification_actionability') AND i.indisunique AND i.indisvalid")).fetchone()
            if row[0] != 5 or any(role) or triggers[0] != 3 or columns[0]!=1 or index[0]!=1:
                raise NotificationError('notification_migration_or_restricted_role_required')

    async def _identity(self, principal):
        if not isinstance(principal, Principal):
            raise NotificationError('verified_principal_required')
        # Account is derived from the verifier result, never a body/path account ID.
        return await self.runtime.identity(principal)

    @asynccontextmanager
    async def scoped(self, account):
        await self.runtime.open()
        async with self.runtime.pool.connection() as c:
            async with c.transaction():
                await c.execute("SELECT set_config('companion.account_id',%s,true)", (str(UUID(str(account))),))
                await c.execute("SET LOCAL TIME ZONE 'UTC'")
                yield c

    async def _state(self, c, account):
        # Lock order: snapshot, scope, registration, outbox. Matches snapshot trigger.
        row = await (await c.execute('SELECT body FROM companion_private.snapshots WHERE account_id=%s FOR SHARE', (account,))).fetchone()
        if not row or not row[0].get('state', {}).get('data_epoch'):
            raise NotificationError('account_snapshot_required')
        document = row[0]
        epoch = document['state']['data_epoch']
        await c.execute('INSERT INTO companion_private.report_notification_scopes(account_id,epoch) VALUES(%s,%s) ON CONFLICT DO NOTHING', (account, epoch))
        scope = await (await c.execute('SELECT epoch,revision FROM companion_private.report_notification_scopes WHERE account_id=%s FOR UPDATE', (account,))).fetchone()
        if scope[0] != epoch:
            raise NotificationError('notification_epoch_inconsistent')
        return document, scope[0], scope[1]

    async def status(self, principal: Principal):
        account = await self._identity(principal)
        async with self.scoped(account) as c:
            doc, epoch, revision = await self._state(c, account)
            rows = await (await c.execute('SELECT installation_id,enabled,token_version,consent_revision FROM companion_private.report_notification_registrations WHERE account_id=%s AND session_key=%s ORDER BY installation_id', (account, session_key(principal)))).fetchall()
            session=await (await c.execute('SELECT revoked FROM companion_private.report_notification_sessions WHERE account_id=%s AND session_key=%s',(account,session_key(principal)))).fetchone()
            outcomes=await (await c.execute('SELECT o.status,count(*) FROM companion_private.report_notification_outbox o JOIN companion_private.report_notification_registrations r ON r.account_id=o.account_id AND r.installation_id=o.installation_id WHERE o.account_id=%s AND r.session_key=%s GROUP BY o.status',(account,session_key(principal)))).fetchall()
            return {'outcomes':dict(outcomes),'epoch': epoch, 'revision': revision, 'eligible': permitted(doc),'session_revoked':bool(session and session[0]),
                    'installations': [{'id': str(r[0]), 'enabled': r[1], 'token_version': r[2], 'consent_revision': r[3]} for r in rows]}

    async def register(self, principal: Principal, *, request_id: str, token: str,
                       epoch: str, revision: int, installation_id: str | None = None,
                       expected_token_version: int = 0,
                       expected_consent_revision: int | None = None, opt_in: bool = False):
        """Explicit opt-in / token refresh. Topic/environment are server configuration.

        Initial request_id supplies idempotency, not identity; server derives the
        installation UUID. Persist returned installation ID across token rotations.
        A refresh preserves opted_at and consent_revision, so it cannot re-alert.
        Re-opt-in after revoke resets opted_at and increments consent_revision.
        Initial admission/re-admission requires opt_in=True from an explicit user
        consent action. Background token refresh must leave it False.
        """
        account = await self._identity(principal)
        request_id = str(UUID(request_id))
        installation = str(UUID(installation_id)) if installation_id else str(uuid5(NAMESPACE_URL, 'report-installation:' + account + ':' + request_id))
        if not isinstance(token, str) or not 1 <= len(token) <= 4096 or len(token) % 2 or any(x not in '0123456789abcdefABCDEF' for x in token):
            raise NotificationError('invalid_device_token')
        token = token.lower()
        key = session_key(principal)
        async with self.scoped(account) as c:
            doc, current_epoch, current_revision = await self._state(c, account)
            if (epoch, revision) != (current_epoch, current_revision):
                raise NotificationError('stale_scope')
            if not permitted(doc):
                raise NotificationError('consent_required')
            await c.execute('INSERT INTO companion_private.report_notification_sessions(account_id,session_key) VALUES(%s,%s) ON CONFLICT DO NOTHING', (account, key))
            revoked = await (await c.execute('SELECT revoked FROM companion_private.report_notification_sessions WHERE account_id=%s AND session_key=%s', (account, key))).fetchone()
            if revoked[0]:
                raise NotificationError('session_revoked')
            old = await (await c.execute('SELECT session_key,enabled,token_version,ciphertext,epoch,scope_revision,consent_revision,topic,environment FROM companion_private.report_notification_registrations WHERE account_id=%s AND installation_id=%s FOR UPDATE', (account, installation))).fetchone()
            if old:
                if old[0] != key and old[1]:
                    raise NotificationError('installation_bound_to_other_session')
                same_scope = old[1] and (old[4],old[5],old[7],old[8]) == (epoch,revision,self.topic,self.environment)
                same_token = same_scope and self.cipher.open(old[3]).get('token') == token
                if expected_consent_revision is not None and expected_consent_revision != old[6]:
                    raise NotificationError('stale_consent_revision')
                if same_token:
                    return {'installation_id': installation, 'token_version': old[2], 'consent_revision': old[6]}
                if expected_consent_revision != old[6]:
                    raise NotificationError('stale_consent_revision')
                if old[2] != expected_token_version:
                    raise NotificationError('stale_token_version')
                if not same_scope and opt_in is not True:
                    raise NotificationError('notification_opt_in_required')
                ciphertext = self.cipher.seal({'token': token})
                row = await (await c.execute('''UPDATE companion_private.report_notification_registrations
                    SET session_key=%s,epoch=%s,scope_revision=%s,enabled=true,ciphertext=%s,
                    topic=%s,environment=%s,token_version=token_version+1,
                    consent_revision=consent_revision+%s,
                    opted_at=CASE WHEN %s THEN opted_at ELSE clock_timestamp() END
                    WHERE account_id=%s AND installation_id=%s RETURNING token_version,consent_revision''',
                    (key,epoch,revision,ciphertext,self.topic,self.environment,0 if same_scope else 1,same_scope,account,installation))).fetchone()
            else:
                if installation_id is not None or expected_token_version != 0:
                    raise NotificationError('unknown_installation')
                if opt_in is not True:
                    raise NotificationError('notification_opt_in_required')
                row = await (await c.execute('''INSERT INTO companion_private.report_notification_registrations
                    (account_id,installation_id,session_key,epoch,scope_revision,ciphertext,topic,environment)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING token_version,consent_revision''',
                    (account,installation,key,epoch,revision,self.cipher.seal({'token':token}),self.topic,self.environment))).fetchone()
            return {'installation_id': installation, 'token_version': row[0], 'consent_revision': row[1]}

    async def revoke(self, principal: Principal, installation_id: str, *, expected_consent_revision: int):
        account = await self._identity(principal)
        installation = str(UUID(installation_id))
        async with self.scoped(account) as c:
            await self._state(c, account)
            # A late logout/revoke from an old session cannot disable a new admission.
            row = await (await c.execute('''UPDATE companion_private.report_notification_registrations
                SET enabled=false,ciphertext='',consent_revision=consent_revision+1
                WHERE account_id=%s AND installation_id=%s AND session_key=%s AND enabled AND consent_revision=%s
                RETURNING installation_id''', (account,installation,session_key(principal),expected_consent_revision))).fetchone()
            if row:
                await c.execute("UPDATE companion_private.report_notification_outbox SET status='cancelled',lease_token=NULL,lease_until=NULL WHERE account_id=%s AND installation_id=%s AND status IN ('pending','sending')", (account,installation))

    async def revoke_initial(self,principal: Principal,request_id: str):
        """A durable tombstone fences an initial token upload whose response was lost.

        A later explicit re-opt-in increments consent revision; stale initial
        cancellation cannot disable that new admission.
        """
        account=await self._identity(principal);key=session_key(principal)
        installation=str(uuid5(NAMESPACE_URL,'report-installation:'+account+':'+str(UUID(request_id))))
        async with self.scoped(account) as c:
            _,epoch,revision=await self._state(c,account)
            await c.execute('INSERT INTO companion_private.report_notification_sessions(account_id,session_key) VALUES(%s,%s) ON CONFLICT DO NOTHING',(account,key))
            await c.execute("""INSERT INTO companion_private.report_notification_registrations
                (account_id,installation_id,session_key,epoch,scope_revision,consent_revision,ciphertext,topic,environment,enabled)
                VALUES(%s,%s,%s,%s,%s,2,'',%s,%s,false) ON CONFLICT DO NOTHING""",(account,installation,key,epoch,revision,self.topic,self.environment))
            row=await c.execute("UPDATE companion_private.report_notification_registrations SET enabled=false,ciphertext='',consent_revision=consent_revision+1 WHERE account_id=%s AND installation_id=%s AND session_key=%s AND consent_revision=1",(account,installation,key))
            if row.rowcount:await c.execute("UPDATE companion_private.report_notification_outbox SET status='cancelled',lease_token=NULL,lease_until=NULL WHERE account_id=%s AND installation_id=%s AND status IN ('pending','sending')",(account,installation))

    async def logout(self, principal: Principal):
        account = await self._identity(principal)
        key = session_key(principal)
        async with self.scoped(account) as c:
            await self._state(c, account)
            await c.execute('INSERT INTO companion_private.report_notification_sessions(account_id,session_key,revoked) VALUES(%s,%s,true) ON CONFLICT(account_id,session_key) DO UPDATE SET revoked=true', (account,key))
            await c.execute("UPDATE companion_private.report_notification_registrations SET enabled=false,ciphertext='',consent_revision=consent_revision+1 WHERE account_id=%s AND session_key=%s", (account,key))
            await c.execute("UPDATE companion_private.report_notification_outbox o SET status='cancelled',lease_token=NULL,lease_until=NULL FROM companion_private.report_notification_registrations r WHERE o.account_id=%s AND r.account_id=o.account_id AND r.installation_id=o.installation_id AND r.session_key=%s AND o.status IN ('pending','sending')", (account,key))

    async def _candidate(self,c,account):
        value=await notification_candidate(c,account,None,None)
        return await notification_candidate(c,account,value['current_run_id'],value['actionability_key'])

    async def _cancel_obsolete(self,c,account,current):
        await c.execute("UPDATE companion_private.report_notification_outbox SET status='cancelled',lease_token=NULL,lease_until=NULL WHERE account_id=%s AND status IN ('pending','sending') AND (%s=false OR run_id IS DISTINCT FROM %s::uuid OR actionability_key IS DISTINCT FROM %s)",
                        (account,current['eligible'],current['current_run_id'],current['actionability_key']))

    async def reconcile(self, account: str) -> int:
        """Reserve one overall current concern-set, never an historical insight."""
        async with self.scoped(account) as c:
            doc, epoch, revision = await self._state(c, account)
            current=await self._candidate(c,account)
            if not permitted(doc):current['eligible']=False
            await self._cancel_obsolete(c,account,current)
            if not current['eligible']:return 0
            result=await c.execute('''INSERT INTO companion_private.report_notification_outbox
                (account_id,epoch,run_id,installation_id,scope_revision,consent_revision,actionability_key)
                SELECT e.account_id,e.epoch,e.run_id,r.installation_id,%s,r.consent_revision,%s
                FROM companion_private.report_notification_events e
                JOIN companion_private.report_notification_registrations r ON r.account_id=e.account_id
                JOIN companion_private.report_notification_sessions ns ON ns.account_id=r.account_id AND ns.session_key=r.session_key
                WHERE e.account_id=%s AND e.epoch=%s AND e.run_id=%s AND r.epoch=e.epoch
                  AND r.scope_revision=%s AND r.enabled AND NOT ns.revoked
                  AND r.opted_at<=e.completed_at AND e.completed_at>=clock_timestamp()-interval '24 hours'
                ON CONFLICT(account_id,epoch,installation_id,actionability_key) WHERE actionability_key IS NOT NULL
                DO UPDATE SET run_id=excluded.run_id,scope_revision=excluded.scope_revision,consent_revision=excluded.consent_revision,
                  status='pending',due_at=clock_timestamp(),available_at=clock_timestamp(),lease_token=NULL,lease_until=NULL
                WHERE companion_private.report_notification_outbox.attempts=0
                  AND companion_private.report_notification_outbox.status IN ('pending','cancelled')
                  AND (companion_private.report_notification_outbox.run_id IS DISTINCT FROM excluded.run_id
                       OR companion_private.report_notification_outbox.status='cancelled')''',
                (revision,current['actionability_key'],account,epoch,current['current_run_id'],revision))
            return result.rowcount

    async def claim(self, account: str) -> Job | None:
        async with self.scoped(account) as c:
            doc, _, _ = await self._state(c, account)
            if not permitted(doc):
                return None
            await c.execute("UPDATE companion_private.report_notification_outbox SET status=CASE WHEN attempts>=5 THEN 'failed' ELSE 'pending' END,lease_token=NULL,lease_until=NULL WHERE account_id=%s AND status='sending' AND lease_until<=clock_timestamp()", (account,))
            # Revoked content/registration jobs never sit indefinitely pending.
            await c.execute("UPDATE companion_private.report_notification_outbox x SET status='cancelled',lease_token=NULL,lease_until=NULL WHERE x.account_id=%s AND x.status IN ('pending','sending') AND NOT EXISTS(SELECT 1 FROM companion_private.report_notification_outbox o " + JOINS + " WHERE o.account_id=x.account_id AND o.notification_id=x.notification_id AND " + VALID + ')', (account,))
            current=await self._candidate(c,account)
            await self._cancel_obsolete(c,account,current)
            if not current['eligible']:return None
            lease = str(uuid4())
            row = await (await c.execute('''WITH candidate AS (
                SELECT o.account_id,o.notification_id FROM companion_private.report_notification_outbox o ''' + JOINS + '''
                WHERE o.account_id=%s AND o.status='pending' AND o.attempts<5
                  AND o.due_at<=clock_timestamp() AND o.available_at<=clock_timestamp() AND ''' + VALID + '''
                ORDER BY o.available_at,o.notification_id FOR UPDATE OF o SKIP LOCKED LIMIT 1)
                UPDATE companion_private.report_notification_outbox o SET status='sending',attempts=o.attempts+1,
                    lease_token=%s,lease_until=clock_timestamp()+make_interval(secs=>%s)
                FROM candidate x WHERE o.account_id=x.account_id AND o.notification_id=x.notification_id AND o.status='pending'
                RETURNING o.notification_id,o.account_id,o.epoch,o.run_id,o.installation_id,o.scope_revision,o.due_at,o.attempts,o.lease_token,o.actionability_key''',
                (account,lease,LEASE_SECONDS))).fetchone()
            if not row:
                return None
            return Job(str(row[0]),str(row[1]),row[2],str(row[3]),str(row[4]),row[5],row[6].timestamp(),row[7],str(row[8]),row[9])

    async def authorize(self, job: Job) -> Admission | None:
        """Resolve encrypted token only after a fresh authoritative admission read."""
        async with self.scoped(job.account) as c:
            doc, _, _ = await self._state(c, job.account)
            if not permitted(doc):
                return None
            current=await self._candidate(c,job.account)
            await self._cancel_obsolete(c,job.account,current)
            verified=await notification_candidate(c,job.account,job.report,job.actionability_key)
            if not verified['eligible']:return None
            row = await (await c.execute('SELECT r.ciphertext,r.token_version,r.topic,r.environment FROM companion_private.report_notification_outbox o ' + JOINS + " WHERE o.account_id=%s AND o.notification_id=%s AND o.lease_token=%s AND o.run_id=%s AND o.actionability_key=%s AND o.status='sending' AND o.lease_until>clock_timestamp() AND " + VALID,
                (job.account,job.id,job.lease,job.report,job.actionability_key))).fetchone()
            if not row:
                return None
            return Admission(job,self.cipher.open(row[0])['token'],row[1],row[2],row[3])

    async def finish(self, job: Job, outcome: str):
        if outcome not in {'accepted','retry','failed','cancelled'}:
            raise NotificationError('invalid_transport_outcome')
        status = 'pending' if outcome == 'retry' and job.attempts < MAX_ATTEMPTS else 'failed' if outcome == 'retry' else outcome
        delay = min(3600,30 * 2 ** (job.attempts - 1)) if status == 'pending' else 0
        async with self.scoped(job.account) as c:
            # Epoch/revocation triggers clear leases atomically. CAS fences stale completions.
            result = await c.execute('''UPDATE companion_private.report_notification_outbox SET
                status=%s,available_at=clock_timestamp()+make_interval(secs=>%s),lease_token=NULL,lease_until=NULL
                WHERE account_id=%s AND notification_id=%s AND lease_token=%s
                  AND status='sending' AND lease_until>clock_timestamp()''', (status,delay,job.account,job.id,job.lease))
            return result.rowcount == 1

    async def invalidate(self, admission: Admission):
        """APNs token rejection cannot revoke a newer token rotation/admission."""
        job = admission.job
        async with self.scoped(job.account) as c:
            await self._state(c, job.account)
            changed = await c.execute("UPDATE companion_private.report_notification_registrations SET enabled=false,ciphertext='',consent_revision=consent_revision+1 WHERE account_id=%s AND installation_id=%s AND token_version=%s AND enabled", (job.account,job.registration,admission.token_version))
            if changed.rowcount:
                await c.execute("UPDATE companion_private.report_notification_outbox SET status='cancelled',lease_token=NULL,lease_until=NULL WHERE account_id=%s AND installation_id=%s AND status IN ('pending','sending')", (job.account,job.registration))
            return changed.rowcount == 1

    async def accounts(self):
        await self.runtime.open()
        async with self.runtime.pool.connection() as c:
            rows = await (await c.execute('SELECT account_id FROM companion_private.accounts WHERE account_id>%s ORDER BY account_id LIMIT 100', (self.cursor,))).fetchall()
        self.cursor = rows[-1][0] if len(rows) == 100 else UUID(int=0)
        return [str(r[0]) for r in rows]


class Provider(Protocol):
    async def send(self, admission: Admission) -> tuple[str, bool]: ...


class HTTP2APNsProvider:
    """Inert until constructed by explicit operator-configured wiring.

    Pass httpx.AsyncClient(http2=True, trust_env=False) and a protected JWT supplier.
    Existing httpx==0.28.1 must become httpx[http2]==0.28.1 in the coordinated lock
    update (h2 plus hpack/hyperframe). No signing key or DSN is loaded here.
    """
    def __init__(self, client, bearer):
        self.client, self.bearer = client, bearer

    async def send(self, admission: Admission) -> tuple[str, bool]:
        invalid = []
        transport = APNsTransport(self.client,admission.topic,admission.environment=='sandbox',
                                  lambda _: admission.token,self.bearer,invalid.append)
        return await transport.send(admission.job), bool(invalid)


class PostgresNotificationWorker:
    def __init__(self, queue: PostgresReportNotifications, provider: Provider | None = None):
        self.queue, self.provider = queue, provider
        self.last_error: str | None = None

    def status(self):
        return {'delivery_configured': self.provider is not None, 'error': self.last_error}

    async def dispatch_one(self, account: str) -> bool:
        if self.provider is None:
            return False  # disabled is not successful delivery; no lease/attempt consumed
        job = await self.queue.claim(account)
        if job is None:
            return False
        try:
            admission = await self.queue.authorize(job)
            if admission is None:
                outcome = 'cancelled'
            else:
                async with asyncio.timeout(15):  # shorter than lease; provider timeout is 10s
                    outcome, invalid = await self.provider.send(admission)
                if invalid and not await self.queue.invalidate(admission):
                    outcome = 'retry'  # old rejected token was rotated; retain logical job
        except Exception:
            outcome = 'retry'  # intentionally no token/private error logging
        await self.queue.finish(job,outcome)
        return True

    async def tick(self) -> int:
        count = 0
        for account in await self.queue.accounts():
            try:
                await self.queue.reconcile(account)
                count += int(await self.dispatch_one(account))
            except NotificationError as error:
                if str(error) != 'account_snapshot_required':
                    raise
        return count

    async def run(self, stop: asyncio.Event, poll_seconds: float = 2):
        """Parent starts/stops this task in its lifespan; no autonomous startup."""
        if poll_seconds <= 0:
            raise ValueError('Positive worker poll interval required')
        await self.queue.check()
        failures = 0
        while not stop.is_set():
            try:
                await self.tick()
                failures = 0
                self.last_error = None
            except Exception:
                # Keep the backend scheduler alive during transient database outages;
                # status exposes only a safe code, never SQL/connection/token details.
                failures = min(failures + 1, 6)
                self.last_error = 'notification_worker_unavailable'
            delay = min(60, poll_seconds * 2 ** failures)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
