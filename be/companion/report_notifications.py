"""Opt-in report-ready outbox; no imports/startup hooks perform I/O.

SQLiteQueue is a durable, single-host reference implementation, NOT a migration of
companion's PostgreSQL store. Production uses report_notification_postgres in the same database as reports,
with transactional terminal events and committed-report reconciliation. Never call enqueue from an HTTP request's claims.
Authority must read persisted report/account/registration state, not client DTOs.

APNs acceptance is not delivery. Lease recovery can resend after an ambiguous
network result; stable collapse IDs mitigate but cannot guarantee exactly once.
Cancellation fences unsent work, not requests already handed to APNs.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import sqlite3
from typing import Callable, Protocol
from uuid import uuid4


TITLE = "Your companion"
BODY = "A report is ready. Open the app to review it."
MAX_ATTEMPTS = 5
LEASE_SECONDS = 60


def utc_seconds(value: datetime) -> float:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An explicit timezone is required")
    return value.astimezone(timezone.utc).timestamp()


@dataclass(frozen=True)
class ReadyReport:
    account: str
    epoch: str
    report: str
    registration: str  # opaque immutable registration-version ID, never a token
    revision: int  # backend monotonic notification-consent/session revision
    due_at: datetime  # backend policy, never derived by the native app


@dataclass(frozen=True)
class Job:
    id: str
    account: str
    epoch: str
    report: str
    registration: str
    revision: int
    due: float
    attempts: int
    lease: str
    actionability_key: str | None = None


class Authority(Protocol):
    def eligible(self, report: ReadyReport) -> bool:
        """Verify terminal persisted report, current epoch, active login,
        notification opt-in AND relevant source/cloud consent, active registration.
        Fail closed; logout/revoke/reset must change the authoritative revision.
        """
        ...


class Queue(Protocol):
    def claim(self, now: datetime) -> Job | None: ...
    def current(self, job: Job, now: datetime) -> bool: ...
    def finish(self, job: Job, outcome: str, now: datetime) -> bool: ...


class SQLiteQueue:
    """Explicitly opened testable durable store; caller controls path/permissions.

    One connection per worker; transactions serialize claims across connections.
    Store on a protected local durable volume, never a shared network filesystem.
    Contains opaque identifiers only, no report content, APNs keys or device tokens.
    set_scope must be called on logout, reset, revocation and new admission with a
    strictly increasing revision. Same-revision conflicting updates are rejected.
    """

    def __init__(self, path: str):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS notification_scopes (
            account TEXT PRIMARY KEY, epoch TEXT NOT NULL,
            revision INTEGER NOT NULL, enabled INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS report_notifications (
            id TEXT PRIMARY KEY, account TEXT NOT NULL, epoch TEXT NOT NULL,
            report TEXT NOT NULL, registration TEXT NOT NULL, revision INTEGER NOT NULL,
            due REAL NOT NULL, available REAL NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, lease TEXT, lease_until REAL,
            UNIQUE(account, epoch, report, registration));
          CREATE INDEX IF NOT EXISTS report_notification_due
            ON report_notifications(status, available);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def set_scope(self, account: str, epoch: str, revision: int, enabled: bool):
        if revision < 0:
            raise ValueError("Invalid revision")
        with self.transaction():
            old = self.db.execute("SELECT * FROM notification_scopes WHERE account=?", (account,)).fetchone()
            if old and revision <= old['revision']:
                if (epoch, revision, int(enabled)) == (old['epoch'], old['revision'], old['enabled']):
                    return
                raise ValueError("Stale or conflicting scope revision")
            self.db.execute("INSERT OR REPLACE INTO notification_scopes VALUES(?,?,?,?)",
                            (account, epoch, revision, int(enabled)))
            self.db.execute("UPDATE report_notifications SET status='cancelled',lease=NULL,lease_until=NULL WHERE account=? AND status IN ('pending','sending')", (account,))

    def _scope(self, account, epoch, revision):
        return bool(self.db.execute("SELECT 1 FROM notification_scopes WHERE account=? AND epoch=? AND revision=? AND enabled=1", (account, epoch, revision)).fetchone())

    def enqueue(self, report: ReadyReport, authority: Authority) -> str | None:
        due = utc_seconds(report.due_at)
        with self.transaction():
            if not self._scope(report.account, report.epoch, report.revision) or not authority.eligible(report):
                return None
            identity = (report.account, report.epoch, report.report, report.registration)
            existing = self.db.execute("SELECT id FROM report_notifications WHERE account=? AND epoch=? AND report=? AND registration=?", identity).fetchone()
            if existing:
                return existing['id']  # immutable first schedule, including cancelled tombstones
            identifier = str(uuid4())
            self.db.execute("INSERT INTO report_notifications(id,account,epoch,report,registration,revision,due,available,status) VALUES(?,?,?,?,?,?,?,?,'pending')",
                            (identifier, *identity, report.revision, due, due))
            return identifier

    def claim(self, now: datetime) -> Job | None:
        stamp = utc_seconds(now)
        with self.transaction():
            self.db.execute("UPDATE report_notifications SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'pending' END,lease=NULL,lease_until=NULL WHERE status='sending' AND lease_until<=?", (MAX_ATTEMPTS, stamp))
            self.db.execute("UPDATE report_notifications SET status='cancelled' WHERE status='pending' AND NOT EXISTS (SELECT 1 FROM notification_scopes s WHERE s.account=report_notifications.account AND s.epoch=report_notifications.epoch AND s.revision=report_notifications.revision AND s.enabled=1)")
            row = self.db.execute("SELECT * FROM report_notifications WHERE status='pending' AND due<=? AND available<=? ORDER BY available,id LIMIT 1", (stamp, stamp)).fetchone()
            if not row:
                return None
            lease = str(uuid4())
            self.db.execute("UPDATE report_notifications SET status='sending',attempts=attempts+1,lease=?,lease_until=? WHERE id=?", (lease, stamp + LEASE_SECONDS, row['id']))
            return Job(*(row[k] for k in ('id','account','epoch','report','registration','revision','due')), row['attempts'] + 1, lease)

    def current(self, job: Job, now: datetime) -> bool:
        return self._scope(job.account, job.epoch, job.revision) and bool(self.db.execute("SELECT 1 FROM report_notifications WHERE id=? AND lease=? AND status='sending' AND lease_until>?", (job.id, job.lease, utc_seconds(now))).fetchone())

    def finish(self, job: Job, outcome: str, now: datetime) -> bool:
        if outcome not in {'accepted', 'retry', 'failed', 'cancelled'}:
            raise ValueError("Unknown transport outcome")
        stamp = utc_seconds(now)
        with self.transaction():
            if not self.current(job, now):
                return False  # late workers cannot overwrite cancellation/new claims
            status = 'pending' if outcome == 'retry' and job.attempts < MAX_ATTEMPTS else 'failed' if outcome == 'retry' else outcome
            available = stamp + min(3600, 30 * 2 ** (job.attempts - 1)) if status == 'pending' else stamp
            self.db.execute("UPDATE report_notifications SET status=?,available=?,lease=NULL,lease_until=NULL WHERE id=? AND lease=?", (status, available, job.id, job.lease))
            return True


class Transport(Protocol):
    async def send(self, job: Job) -> str:
        """Return accepted/retry/failed; never call acceptance 'delivered'."""
        ...


class Dispatcher:
    def __init__(self, queue: Queue, authority: Authority, transport: Transport,
                 clock: Callable[[], datetime]):
        self.queue, self.authority, self.transport, self.clock = queue, authority, transport, clock

    async def tick(self) -> bool:
        job = self.queue.claim(self.clock())
        if job is None:
            return False
        try:
            ready = ReadyReport(job.account, job.epoch, job.report, job.registration,
                                job.revision, datetime.fromtimestamp(job.due, timezone.utc))
            if not self.authority.eligible(ready) or not self.queue.current(job, self.clock()):
                self.queue.finish(job, 'cancelled', self.clock())
                return True
            outcome = await self.transport.send(job)
        except Exception:
            # No private error messages/tokens are retained; crash/timeout is ambiguous.
            outcome = 'retry'
        self.queue.finish(job, outcome, self.clock())
        return True


def apns_payload(job: Job) -> dict:
    return {'aps': {'alert': {'title': TITLE, 'body': BODY}},
            'report_notification_id': job.id}  # no account/report/health/financial data


class APNsTransport:
    """Real HTTP/2 APNs boundary, inert until explicitly injected and called.

    client: async HTTP/2 TLS client (e.g. httpx.AsyncClient(http2=True)); HTTP/2
    dependencies, connection lifecycle and redacted logging are caller-owned.
    resolve(job): secure current token lookup + authoritative admission recheck;
    return None if revoked. It MUST bind account/epoch/revision/registration.
    bearer(): short-lived provider JWT from protected signer, not a frontend token.
    invalidate(job): compare-and-disable only that immutable registration version.
    The 10-second HTTP timeout is intentionally shorter than the queue lease.
    """
    def __init__(self, client, topic: str, sandbox: bool,
                 resolve: Callable[[Job], str | None], bearer: Callable[[], str],
                 invalidate: Callable[[Job], None]):
        if not topic:
            raise ValueError('APNs topic required')
        self.client, self.topic = client, topic
        self.host = 'https://api.sandbox.push.apple.com' if sandbox else 'https://api.push.apple.com'
        self.resolve, self.bearer, self.invalidate = resolve, bearer, invalidate

    async def send(self, job: Job) -> str:
        token = self.resolve(job)
        if token is None:
            return 'cancelled'
        if not token or any(c not in '0123456789abcdefABCDEF' for c in token):
            return 'failed'
        headers = {'authorization': 'bearer ' + self.bearer(), 'apns-topic': self.topic,
                   'apns-push-type': 'alert', 'apns-priority': '5',
                   'apns-expiration': '0', 'apns-id': job.id,
                   'apns-collapse-id': hashlib.sha256(job.id.encode()).hexdigest()}
        response = await self.client.post(self.host + '/3/device/' + token,
                                          headers=headers, json=apns_payload(job), timeout=10)
        if response.status_code == 200:
            return 'accepted'
        try:
            reason = response.json().get('reason')
        except (ValueError, AttributeError):
            reason = None
        if response.status_code == 410 or (response.status_code == 400 and reason in {'BadDeviceToken','DeviceTokenNotForTopic'}):
            self.invalidate(job)
            return 'cancelled'
        if response.status_code == 429 or response.status_code >= 500:
            return 'retry'
        return 'failed'  # auth/topic/configuration failures require operator correction
